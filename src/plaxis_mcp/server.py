"""Role-pinned, stdio-only MCP host for an isolated PLAXIS worker.

This module intentionally imports no PLAXIS SDK package. The only process
that imports plxscripting is plaxis_mcp.worker_runtime, started with the
Python executable supplied by the selected PLAXIS installation.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from enum import Enum
from contextlib import asynccontextmanager
import functools
import inspect
import json
import os
from pathlib import Path
import sys
from typing import Annotated, Any, Callable, Literal, Mapping, TypeAlias

from mcp.server.fastmcp import FastMCP
from mcp import types as mcp_types
from mcp.types import ToolAnnotations
from pydantic import ConfigDict, Field, StrictBool, StrictFloat, StrictInt, StrictStr

from . import __version__
from .bridge import (
    BridgeError,
    BridgeSession,
    BridgeSessionConfig,
    WorkerOperationError,
    WorkerProtocolError,
    WorkerStartupError,
    WorkerTimeoutError,
)
from .deployment import (
    CredentialStore,
    CredentialStoreUnavailable,
    EndpointProfile,
    WindowsCredentialStore,
    credentials_main,
    load_profile,
    profiles_main,
)


class ServerConfigurationError(RuntimeError):
    """Raised for an unsafe or incomplete local PLAXIS MCP configuration."""


class PlaxisRole(str, Enum):
    """The immutable PLAXIS scripting role hosted by one MCP process."""

    INPUT = "input"
    OUTPUT = "output"


_DEFAULT_PORTS = {
    PlaxisRole.INPUT: 10000,
    PlaxisRole.OUTPUT: 10001,
}
_LOOPBACK_HOSTS = {"127.0.0.1", "::1"}
JsonValue: TypeAlias = (
    StrictStr
    | StrictInt
    | StrictFloat
    | StrictBool
    | None
    | list[Any]
    | dict[str, Any]
)


@dataclass(frozen=True)
class ServerConfig:
    """Pinned connection and runtime settings for one role."""

    role: PlaxisRole
    host: str = "127.0.0.1"
    port: int | None = None
    password: str = field(default="", repr=False)
    connect_timeout: float = 5.0
    request_timeout: float = 240.0
    worker_python: str | None = None
    runtime_profile: str = "unconfigured"
    installation_root: str | None = None

    def __post_init__(self) -> None:
        host = self.host.strip()
        if host.lower() not in _LOOPBACK_HOSTS:
            raise ServerConfigurationError(
                "PLAXIS scripting endpoints must be loopback addresses "
                "(127.0.0.1 or ::1)."
            )
        if self.port is None:
            object.__setattr__(self, "port", _DEFAULT_PORTS[self.role])
        if isinstance(self.port, bool) or not isinstance(self.port, int) or not 1 <= self.port <= 65535:
            raise ServerConfigurationError("PLAXIS port must be an integer from 1 through 65535.")
        if self.connect_timeout <= 0 or self.request_timeout <= 0:
            raise ServerConfigurationError("PLAXIS timeouts must be positive.")
        object.__setattr__(self, "host", host)

    def connection_kwargs(self) -> dict[str, Any]:
        """Legacy direct-session compatibility only; never expose these in MCP."""

        return {
            "host": self.host,
            "port": self.port,
            "password": self.password,
            "timeout": self.connect_timeout,
            "request_timeout": self.request_timeout,
        }


def _value_from_env(
    environment: Mapping[str, str],
    specific_name: str,
    generic_names: tuple[str, ...],
    default: str,
) -> str:
    if specific_name in environment:
        return environment[specific_name]
    for generic_name in generic_names:
        if generic_name in environment:
            return environment[generic_name]
    return default


def _parse_positive_float(value: str, name: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ServerConfigurationError("%s must be a positive number." % name) from exc
    if parsed <= 0:
        raise ServerConfigurationError("%s must be a positive number." % name)
    return parsed


def _parse_port(value: str, name: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ServerConfigurationError("%s must be an integer port." % name) from exc
    if not 1 <= parsed <= 65535:
        raise ServerConfigurationError("%s must be an integer from 1 through 65535." % name)
    return parsed


def load_server_config(environment: Mapping[str, str] | None = None) -> ServerConfig:
    """Load a role-pinned configuration with one-release generic fallbacks."""

    environment = dict(os.environ if environment is None else environment)
    raw_role = environment.get("PLAXIS_ROLE", "").strip().lower()
    try:
        role = PlaxisRole(raw_role)
    except ValueError as exc:
        raise ServerConfigurationError(
            "PLAXIS_ROLE must be explicitly set to either 'input' or 'output'."
        ) from exc

    prefix = "PLAXIS_%s" % role.value.upper()
    default_port = str(_DEFAULT_PORTS[role])
    host = _value_from_env(environment, prefix + "_HOST", ("PLAXIS_HOST",), "127.0.0.1")
    port = _parse_port(
        _value_from_env(environment, prefix + "_PORT", ("PLAXIS_PORT",), default_port),
        prefix + "_PORT",
    )
    password = _value_from_env(environment, prefix + "_PASSWORD", ("PLAXIS_PASSWORD",), "")
    connect_timeout = _parse_positive_float(
        _value_from_env(
            environment,
            prefix + "_CONNECT_TIMEOUT",
            ("PLAXIS_CONNECT_TIMEOUT", "PLAXIS_TIMEOUT"),
            "5",
        ),
        prefix + "_CONNECT_TIMEOUT",
    )
    request_timeout = _parse_positive_float(
        _value_from_env(
            environment,
            prefix + "_REQUEST_TIMEOUT",
            ("PLAXIS_REQUEST_TIMEOUT",),
            "240",
        ),
        prefix + "_REQUEST_TIMEOUT",
    )
    worker_python = _value_from_env(
        environment,
        prefix + "_BUNDLE_PYTHON",
        ("PLAXIS_BUNDLE_PYTHON",),
        "",
    ) or None
    runtime_profile = _value_from_env(
        environment,
        prefix + "_RUNTIME_PROFILE",
        ("PLAXIS_RUNTIME_PROFILE",),
        "unconfigured",
    )
    return ServerConfig(
        role=role,
        host=host,
        port=port,
        password=password,
        connect_timeout=connect_timeout,
        request_timeout=request_timeout,
        worker_python=worker_python,
        runtime_profile=runtime_profile,
    )


def _new_bridge_session(config: ServerConfig) -> BridgeSession:
    if not config.worker_python:
        raise ServerConfigurationError(
            "PLAXIS_BUNDLE_PYTHON (or the role-specific BUNDLE_PYTHON setting) "
            "must point to the selected PLAXIS bundled interpreter."
        )
    return BridgeSession(
        BridgeSessionConfig(
            worker_python=config.worker_python,
            role=config.role.value,
            host=config.host,
            port=config.port,
            password=config.password,
            connect_timeout=config.connect_timeout,
            request_timeout=config.request_timeout,
            runtime_profile=config.runtime_profile,
            installation_root=config.installation_root,
        )
    )


def server_config_from_profile(
    profile: EndpointProfile,
    *,
    credential_store: CredentialStore | None = None,
) -> ServerConfig:
    """Build an immutable endpoint config from a generated, secret-free profile."""

    if not profile.worker_python.is_file():
        raise ServerConfigurationError("The selected PLAXIS bundled interpreter was not found.")
    try:
        password = (credential_store or WindowsCredentialStore()).read(profile.credential_target)
    except CredentialStoreUnavailable as exc:
        raise ServerConfigurationError("The stored PLAXIS password could not be read.") from exc
    return ServerConfig(
        role=PlaxisRole(profile.role),
        host=profile.host,
        port=profile.port,
        password=password,
        connect_timeout=profile.connect_timeout_seconds,
        request_timeout=profile.request_timeout_seconds,
        worker_python=str(profile.worker_python),
        runtime_profile=profile.runtime_profile,
        installation_root=str(profile.installation_root),
    )


_PROFILE_OVERRIDE_ENVIRONMENT = (
    "PLAXIS_ROLE",
    "PLAXIS_HOST",
    "PLAXIS_PORT",
    "PLAXIS_PASSWORD",
    "PLAXIS_TIMEOUT",
    "PLAXIS_CONNECT_TIMEOUT",
    "PLAXIS_REQUEST_TIMEOUT",
    "PLAXIS_BUNDLE_PYTHON",
    "PLAXIS_RUNTIME_PROFILE",
    "PLAXIS_INPUT_HOST",
    "PLAXIS_INPUT_PORT",
    "PLAXIS_INPUT_PASSWORD",
    "PLAXIS_INPUT_CONNECT_TIMEOUT",
    "PLAXIS_INPUT_REQUEST_TIMEOUT",
    "PLAXIS_INPUT_BUNDLE_PYTHON",
    "PLAXIS_INPUT_RUNTIME_PROFILE",
    "PLAXIS_OUTPUT_HOST",
    "PLAXIS_OUTPUT_PORT",
    "PLAXIS_OUTPUT_PASSWORD",
    "PLAXIS_OUTPUT_CONNECT_TIMEOUT",
    "PLAXIS_OUTPUT_REQUEST_TIMEOUT",
    "PLAXIS_OUTPUT_BUNDLE_PYTHON",
    "PLAXIS_OUTPUT_RUNTIME_PROFILE",
)


def _reject_profile_environment_overrides(environment: Mapping[str, str]) -> None:
    if any(name in environment for name in _PROFILE_OVERRIDE_ENVIRONMENT):
        raise ServerConfigurationError(
            "serve --config uses only its generated profile and Windows Credential Manager; "
            "PLAXIS endpoint environment overrides are not allowed."
        )


def _construct_session(
    config: ServerConfig,
    session_factory: Callable[..., Any] | None,
) -> Any:
    if session_factory is None:
        return _new_bridge_session(config)
    try:
        signature = inspect.signature(session_factory)
        signature.bind(config)
    except TypeError:
        return session_factory()
    except (ValueError, AttributeError):
        return session_factory(config)
    return session_factory(config)


def _connect_session(session: Any, config: ServerConfig) -> dict[str, Any]:
    """Connect a bridge zero-argument session or a temporary legacy fake."""

    if isinstance(session, BridgeSession) or callable(getattr(session, "invoke", None)):
        return session.connect()
    return session.connect(**config.connection_kwargs())


def _invoke(session: Any, operation: str, **params: Any) -> Any:
    bridge_invoke = getattr(session, "invoke", None)
    if callable(bridge_invoke):
        return bridge_invoke(operation, **params)
    method = getattr(session, operation)
    return method(**params)


def _normalized_status(session: Any, config: ServerConfig) -> dict[str, Any]:
    raw_status = session.status()
    if not isinstance(raw_status, Mapping):
        raw_status = {}
    handshake = getattr(session, "handshake", None)
    if not isinstance(handshake, Mapping):
        handshake = {}
    return {
        "role": config.role.value,
        "state": raw_status.get("state", "disconnected"),
        "connected": bool(raw_status.get("connected", False)),
        "host": config.host,
        "port": config.port,
        "server_full_name": raw_status.get("server_full_name"),
        "product_version": raw_status.get("product_version") or handshake.get("product_version"),
        "runtime_profile": (
            config.runtime_profile
            if config.runtime_profile not in {"auto", "unconfigured"}
            else handshake.get("runtime_profile", config.runtime_profile)
        ),
        "worker_python_version": raw_status.get("python_version") or handshake.get("python_version"),
        "current_operation": raw_status.get("current_operation"),
        "last_error": raw_status.get("last_error"),
        "last_transition_at": raw_status.get("last_transition_at"),
    }


_READ = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)
_CONNECT = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=True,
)
_DISCONNECT = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)
_SET_PROPERTY = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=True,
    idempotentHint=True,
    openWorldHint=False,
)
_CALL_METHOD = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=True,
    idempotentHint=False,
    openWorldHint=True,
)
_PROJECT_MUTATOR = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=True,
    idempotentHint=False,
    openWorldHint=False,
)
_CLOSE_SAVE = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=True,
    idempotentHint=True,
    openWorldHint=False,
)


def _set_version(server: FastMCP) -> None:
    # The Python SDK does not expose a public constructor argument for the
    # protocol server-info version in this release.
    server._mcp_server.version = __version__


def _lock_input_schemas(server: FastMCP) -> None:
    """Make the SDK-generated argument models reject unknown properties.

    FastMCP 1.26 deliberately uses permissive Pydantic argument models. The
    deployed contract is stricter: every advertised tool schema is closed, so
    old and new clients receive the same invalid-argument behavior.
    """

    for tool in server._tool_manager._tools.values():
        tool.parameters["additionalProperties"] = False
        model = tool.fn_metadata.arg_model
        model.model_config = ConfigDict(**dict(model.model_config), extra="forbid")
        model.model_rebuild(force=True)


def _safe_error_state(session: Any, config: ServerConfig) -> str:
    try:
        return str(_normalized_status(session, config)["state"])
    except Exception:
        return "uncertain"


def _bridge_error_result(
    error: BridgeError,
    session: Any,
    config: ServerConfig,
) -> mcp_types.CallToolResult:
    """Convert a private bridge failure into a portable MCP tool error."""

    if isinstance(error, WorkerOperationError):
        code = error.code
        retryable = error.retryable
        state = error.state or _safe_error_state(session, config)
    elif isinstance(error, WorkerTimeoutError):
        code, retryable, state = "TIMEOUT", False, "uncertain"
    elif isinstance(error, WorkerStartupError):
        code, retryable, state = "CONNECTION_FAILED", True, _safe_error_state(session, config)
    elif isinstance(error, WorkerProtocolError):
        # Corruption is not cancellation. Reporting it as CANCELLED_OUTCOME_UNKNOWN
        # tells an agent a human intended the interruption, and would make a real
        # client cancellation indistinguishable from a malformed worker record.
        # The uncertain state is what carries the safety meaning here.
        code, retryable, state = "PROTOCOL_ERROR", False, "uncertain"
    else:
        code, retryable, state = "PLAXIS_ERROR", False, _safe_error_state(session, config)
    payload = {
        "code": code,
        "message": str(error),
        "retryable": retryable,
        "state": state,
    }
    return mcp_types.CallToolResult(
        content=[mcp_types.TextContent(type="text", text=json.dumps(payload, separators=(",", ":")))],
        structuredContent=payload,
        isError=True,
    )


def _install_bridge_error_handlers(
    server: FastMCP,
    session: Any,
    config: ServerConfig,
) -> None:
    """Preserve generated schemas while returning MCP isError for bridge faults."""

    for tool in server._tool_manager._tools.values():
        original = tool.fn

        @functools.wraps(original)
        def guarded(*args: Any, __original: Callable[..., Any] = original, **kwargs: Any) -> Any:
            try:
                return __original(*args, **kwargs)
            except BridgeError as error:
                return _bridge_error_result(error, session, config)

        tool.fn = guarded


def _worker_lifespan(session: Any) -> Callable[[FastMCP], Any]:
    """Ensure stdin EOF and server shutdown cannot orphan a worker process."""

    @asynccontextmanager
    async def lifespan(_: FastMCP):
        try:
            yield
        finally:
            close = getattr(session, "close", None)
            if callable(close):
                close()

    return lifespan


def create_server(
    config: ServerConfig | None = None,
    *,
    session_factory: Callable[..., Any] | None = None,
) -> FastMCP:
    """Create a role-specific FastMCP server without importing PLAXIS SDKs."""

    config = config or load_server_config()
    session = _construct_session(config, session_factory)
    server = FastMCP(
        "plaxis-mcp",
        instructions=(
            "Connect only to the pinned local PLAXIS %s endpoint. "
            "Require client approval before mutation tools."
        ) % config.role.value,
        json_response=True,
        lifespan=_worker_lifespan(session),
    )
    _set_version(server)
    # Intentionally private, testable attachment. It does not contain a
    # serializable MCP resource and should never be surfaced to clients.
    server._plaxis_config = config
    server._plaxis_session = session

    @server.resource("plaxis://%s/status" % config.role.value)
    def role_status_resource() -> dict[str, Any]:
        """Return the state of this role-pinned PLAXIS worker."""

        return _normalized_status(session, config)

    @server.resource("plaxis://status")
    def compatibility_status_resource() -> dict[str, Any]:
        """Deprecated alias for clients that cannot select a role URI yet."""

        return _normalized_status(session, config)

    @server.tool(annotations=_CONNECT, structured_output=True)
    def connect() -> dict[str, Any]:
        """Connect to the pinned local PLAXIS endpoint."""

        return _normalized_status_after(_connect_session(session, config), session, config)

    @server.tool(annotations=_DISCONNECT, structured_output=True)
    def disconnect() -> dict[str, Any]:
        """Disconnect the current PLAXIS worker session."""

        return _normalized_status_after(session.disconnect(), session, config)

    @server.tool(annotations=_READ, structured_output=True)
    def connection_status() -> dict[str, Any]:
        """Get state for the pinned PLAXIS role."""

        return _normalized_status(session, config)

    @server.tool(annotations=_READ, structured_output=True)
    def list_members(path: StrictStr = "") -> dict[str, Any]:
        """List available attributes on a PLAXIS object path."""

        return _invoke(session, "list_members", path=path)

    @server.tool(annotations=_READ, structured_output=True)
    def inspect(path: StrictStr = "") -> dict[str, Any]:
        """Inspect a PLAXIS object or property."""

        return _invoke(session, "inspect", path=path)

    @server.tool(annotations=_READ, structured_output=True)
    def project_info() -> dict[str, Any]:
        """Return common project metadata and units."""

        return _invoke(session, "project_info")

    @server.tool(annotations=_READ, structured_output=True)
    def list_phases() -> dict[str, Any]:
        """Return a compact model-phase summary."""

        return _invoke(session, "list_phases")

    @server.tool(annotations=_READ, structured_output=True)
    def list_materials() -> dict[str, Any]:
        """Return a compact material summary."""

        return _invoke(session, "list_materials")

    if config.role is PlaxisRole.INPUT:

        @server.tool(annotations=_SET_PROPERTY, structured_output=True)
        def set_property(
            path: Annotated[StrictStr, Field(min_length=1)],
            value: JsonValue,
        ) -> dict[str, Any]:
            """Set a writable PLAXIS property using a dotted path."""

            return _invoke(session, "set_property", path=path, value=value)

        @server.tool(annotations=_CALL_METHOD, structured_output=True)
        def call_method(
            method: Annotated[StrictStr, Field(min_length=1)],
            path: StrictStr = "",
            args: list[JsonValue] | None = None,
        ) -> dict[str, Any]:
            """Call an explicitly named PLAXIS method with literal positional arguments.

            Every argument must be a literal value (number, string, boolean, or an
            array/object of literals) -- there is no way to pass a reference to
            another PLAXIS object or phase through this tool. A string argument is
            sent to PLAXIS as a quoted text literal, not as a reference, so it
            cannot stand in for an object or phase parameter even when it names
            one. Use this only for methods whose PLAXIS command reference shows
            purely literal parameters (numbers, text, booleans).
            """

            return _invoke(session, "call_method", path=path, method=method, args=args or [])

        @server.tool(annotations=_PROJECT_MUTATOR, structured_output=True)
        def new_project() -> dict[str, Any]:
            """Create a new PLAXIS project."""

            return _invoke(session, "new_project")

        @server.tool(annotations=_PROJECT_MUTATOR, structured_output=True)
        def open_project(filename: Annotated[StrictStr, Field(min_length=1)]) -> dict[str, Any]:
            """Open an approved PLAXIS project path."""

            return _invoke(session, "open_project", filename=filename)

        @server.tool(annotations=_CLOSE_SAVE, structured_output=True)
        def close_project() -> dict[str, Any]:
            """Close the active PLAXIS project."""

            return _invoke(session, "close_project")

        @server.tool(annotations=_PROJECT_MUTATOR, structured_output=True)
        def recover_project() -> dict[str, Any]:
            """Recover the current PLAXIS project."""

            return _invoke(session, "recover_project")

        @server.tool(annotations=_CLOSE_SAVE, structured_output=True)
        def save_project(
            filename: Annotated[StrictStr | None, Field(min_length=1)] = None,
        ) -> dict[str, Any]:
            """Save the active project, optionally to an approved new path."""

            return _invoke(session, "save_project", filename=filename)

    else:

        @server.tool(annotations=_READ, structured_output=True)
        def list_result_types(path: StrictStr = "ResultTypes") -> dict[str, Any]:
            """List available Output result-type groups or members."""

            return _invoke(session, "list_result_types", path=path)

        @server.tool(annotations=_READ, structured_output=True)
        def get_results(
            phase: StrictInt | StrictStr,
            result_type_path: Annotated[StrictStr, Field(min_length=1)],
            fem_type: Literal["node", "stresspoint", "element", "materialpoint"] = "node",
            smoothing: StrictBool | None = None,
            offset: Annotated[StrictInt, Field(ge=0)] = 0,
            limit: Annotated[StrictInt, Field(ge=1, le=5000)] = 200,
        ) -> dict[str, Any]:
            """Fetch one lossless page of PLAXIS Output results.

            smoothing enables PLAXIS's result-smoothing option where supported;
            omit it to use PLAXIS's own default for the result type.
            """

            return _invoke(
                session,
                "get_results",
                phase=phase,
                result_type_path=result_type_path,
                fem_type=fem_type,
                smoothing=smoothing,
                offset=offset,
                limit=limit,
            )

    _lock_input_schemas(server)
    _install_bridge_error_handlers(server, session, config)
    return server


def _normalized_status_after(result: Any, session: Any, config: ServerConfig) -> dict[str, Any]:
    # Worker connect/disconnect results carry extra attestation fields, while
    # the MCP contract deliberately remains stable and secret-free.
    if isinstance(result, Mapping):
        normalized = _normalized_status(session, config)
        normalized["state"] = result.get("state", normalized["state"])
        normalized["connected"] = bool(result.get("connected", normalized["connected"]))
        # connect reports whether it just cleared a session uncertainty, so an
        # agent learns that an earlier operation's outcome was never confirmed.
        # Only connect sets this, so disconnect's shape is unchanged.
        if "recovered_from_uncertain" in result:
            normalized["recovered_from_uncertain"] = bool(result["recovered_from_uncertain"])
        return normalized
    return _normalized_status(session, config)


mcp: FastMCP | None = None


def _run_stdio(config: ServerConfig) -> int:
    """Run one role and always close its worker when stdin or the client exits."""

    global mcp
    mcp = create_server(config)
    try:
        mcp.run(transport="stdio")
    finally:
        close = getattr(mcp._plaxis_session, "close", None)
        if callable(close):
            close()
    return 0


def main(argv: list[str] | None = None) -> int:
    """Run stdio MCP, package profiles, or the hidden-input credential utility."""

    transport = os.getenv("PLAXIS_MCP_TRANSPORT", "stdio")
    if transport != "stdio":
        raise ServerConfigurationError("PLAXIS-MCP supports the stdio transport only.")
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments:
        config = load_server_config()
        if not config.worker_python:
            raise ServerConfigurationError(
                "PLAXIS_BUNDLE_PYTHON is required before starting a source-mode PLAXIS-MCP host."
            )
        return _run_stdio(config)

    # These two own their own argument parsers. Dispatch before argparse sees
    # them: a subparser using nargs=REMAINDER does not reliably capture leading
    # option-like tokens, so "plaxis-mcp profiles --output-dir X" failed with
    # "unrecognized arguments: --output-dir" instead of delegating.
    if arguments[0] == "credentials":
        return credentials_main(arguments[1:])
    if arguments[0] == "profiles":
        return profiles_main(arguments[1:])

    parser = argparse.ArgumentParser(prog="plaxis-mcp")
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="Start a role-pinned stdio MCP server.")
    serve.add_argument("--config", required=True, type=Path, help="Generated input.toml or output.toml")
    # Registered for --help discoverability only; both are dispatched above.
    commands.add_parser("credentials", help="Store or remove a PLAXIS role password.")
    commands.add_parser("profiles", help="Discover PLAXIS and generate role profiles.")
    namespace = parser.parse_args(arguments)

    _reject_profile_environment_overrides(os.environ)
    profile = load_profile(namespace.config)
    return _run_stdio(server_config_from_profile(profile))


if __name__ == "__main__":
    raise SystemExit(main())
