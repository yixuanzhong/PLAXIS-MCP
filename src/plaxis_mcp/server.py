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

from mcp.server.mcpserver import MCPServer
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
    ProfileError,
    WindowsCredentialStore,
    credentials_main,
    discover_role_ports,
    load_profile,
    profile_path_for_role,
    profiles_main,
    set_profile_port,
    setup_main,
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
# Everything PLAXIS can actually receive as one value. plxscripting builds
# every call as a command line, where a mapping has no representation at all
# and a sequence becomes a parenthesised group -- so the dict and list arms of
# the old JsonValue alias were never values PLAXIS could take, and admitting
# them only moved the failure to PLAXIS's parser. To point a property at
# another object, pass that object's `path` string to a tool that takes
# references (WP-B/WP-H).
ScalarValue: TypeAlias = StrictStr | StrictInt | StrictFloat | StrictBool | None


@dataclass(frozen=True)
class ServerConfig:
    """Pinned connection and runtime settings for one role."""

    role: PlaxisRole
    host: str = "127.0.0.1"
    port: int | None = None
    password: str = field(default="", repr=False)
    # See EndpointProfile.connect_timeout_seconds: PLAXIS is routinely busy for
    # several seconds after new_project, and 5 s was below that (WP-I).
    connect_timeout: float = 15.0
    request_timeout: float = 240.0
    worker_python: str | None = None
    runtime_profile: str = "unconfigured"
    installation_root: str | None = None
    # Where this config was loaded from, so a corrected port can be written
    # back. None for an environment-built config, which has no file to repin.
    profile_path: str | None = None

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
            "15",
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
    profile_path: Path | str | None = None,
) -> ServerConfig:
    """Build an immutable endpoint config from a generated, secret-free profile."""

    if not profile.worker_python.is_file():
        raise ServerConfigurationError("The selected PLAXIS bundled interpreter was not found.")
    try:
        password = (credential_store or WindowsCredentialStore()).read(profile.credential_target)
    except CredentialStoreUnavailable as exc:
        raise ServerConfigurationError(
            "The stored PLAXIS password could not be read. Run `plaxis-mcp credentials "
            "set --target %s` in a terminal to store it; it prompts with hidden input "
            "and never takes the password on the command line." % profile.credential_target
        ) from exc
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
        profile_path=None if profile_path is None else str(profile_path),
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
            "serve uses only its generated profile and Windows Credential Manager; "
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


def _names_this_role(server_full_name: Any, role: str) -> bool:
    """Require the peer to name itself as this role's PLAXIS scripting server.

    Stricter than the post-connect endpoint check on purpose. That check
    validates a port the operator chose and so tolerates a build that omits the
    role word; this one is choosing between ports, and an unnamed candidate is
    not evidence -- it is the thing being decided.
    """

    if not isinstance(server_full_name, str):
        return False
    name = server_full_name.lower()
    return "plaxis" in name and role in name


def _contradicts_this_role(server_full_name: Any, role: str) -> bool:
    """True only when a peer's own name rules out this role's PLAXIS.

    Deliberately permissive, matching the worker's post-connect check: a build
    that omits the role word is not proof of a mismatch, and a port that cannot
    be identified at all is not evidence of anything. Only an explicit
    contradiction may stop a connect the operator configured.
    """

    if not isinstance(server_full_name, str) or not server_full_name.strip():
        return False
    name = server_full_name.lower()
    if "plaxis" not in name:
        return True
    opposite = "output" if role == "input" else "input"
    return opposite in name and role not in name


def _refuse_unidentified_endpoint(session: Any, config: ServerConfig) -> None:
    """Withhold the password from a pinned port that is not this role's PLAXIS.

    The scripting handshake sends the password before the peer identifies
    itself, so the post-connect endpoint check can only report a disclosure
    that already happened -- it is how a service answering as
    "Qualys-Correlation" came to be handed a PLAXIS password. Identity is
    served unauthenticated, so asking first costs one round trip and turns that
    disclosure into a refusal.

    Silence is not a contradiction: a PLAXIS that is merely busy cannot answer
    a probe either, and treating that as a mismatch would defeat the connect
    retry that exists because a failed connect once had an agent inventing
    results. An unidentifiable port falls through to the normal connect.
    """

    identify = getattr(session, "identify", None)
    if not callable(identify):
        return
    try:
        answer = identify(config.port)
    except BridgeError:
        return
    if not isinstance(answer, Mapping):
        return
    name = answer.get("server_full_name")
    if not _contradicts_this_role(name, config.role.value):
        return
    error = WorkerOperationError(
        "WRONG_ENDPOINT",
        "The service on %s:%s identified itself as %s, which is not the PLAXIS %s "
        "scripting server this profile is pinned to. No password was sent to it."
        % (config.host, config.port, name, config.role.value),
        state="disconnected",
    )
    # Records that this refusal happened before the handshake, so a correction
    # built on it does not tell the operator to rotate an undisclosed password.
    error.password_sent = False
    raise error


def _scripting_ports_among(session: Any, ports: list[int], role: str) -> list[int]:
    """Keep only the ports that identify as this role's scripting server."""

    identify = getattr(session, "identify", None)
    if not callable(identify):
        return []
    identified = []
    for port in ports:
        try:
            answer = identify(port)
        except BridgeError:
            continue
        if not isinstance(answer, Mapping):
            continue
        if _names_this_role(answer.get("server_full_name"), role):
            identified.append(port)
    return identified


def _correct_pinned_port(
    session: Any,
    config: ServerConfig,
    error: BridgeError,
) -> dict[str, Any] | None:
    """Repin this role to the port its own PLAXIS is actually listening on.

    A stale pin is not merely a failed connect. The scripting handshake sends
    the password before the peer identifies itself, so every attempt against a
    port some other local service has taken discloses the password again -- the
    disclosure ``_endpoint_identity_error`` records, where an endpoint answering
    as "Qualys-Correlation" was accepted as a healthy PLAXIS connection.
    Correcting the pin ends that repetition.

    The candidate comes from the OS process table and never from probing: it is
    a port owned by a running PLAXIS executable of this role, under this
    profile's own installation root. Nothing is connected to in order to find
    it, so discovery itself can disclose nothing. A password still only ever
    reaches a process that was identified first, and the worker's own endpoint
    check still runs afterwards.
    """

    if not isinstance(error, WorkerOperationError) or error.retryable:
        return None
    # A busy or slow PLAXIS is retryable and keeps its port; only a dead port
    # or a peer that is not this role's PLAXIS is a pin worth moving.
    if error.code not in ("CONNECTION_FAILED", "WRONG_ENDPOINT"):
        return None
    if not config.installation_root:
        return None
    candidates = [
        port
        for port in discover_role_ports(config.installation_root, config.role.value)
        if port != config.port
    ]
    if not candidates:
        return None
    # Owning the process is not enough. Measured on PLAXIS 2D V22, one Input
    # process held both 10003 ("PLAXIS 2D Input/22.2.0.1078") and 10141
    # ("sgcWebSockets 4.0.2"); only the second step tells them apart.
    candidates = _scripting_ports_among(session, candidates, config.role.value)
    # One identified scripting endpoint, or the operator decides. Guessing
    # between two would be guessing which one receives the password.
    if len(candidates) != 1:
        return None

    port = candidates[0]
    previous_port = config.port
    session_config = getattr(session, "config", None)
    if session_config is None:
        return None
    session_config.port = port
    # ServerConfig is frozen against accidental mutation, but a corrected pin
    # has to be visible everywhere the config is read -- connection_status
    # reports config.port, and reporting the abandoned port would be a lie.
    object.__setattr__(config, "port", port)

    correction: dict[str, Any] = {
        "previous_port": previous_port,
        "port": port,
        "profile_updated": False,
        # Only a handshake that actually reached the wrong peer discloses
        # anything. A refusal raised before the password was sent must not send
        # the operator off to rotate a password nobody received.
        "credential_exposed": (
            error.code == "WRONG_ENDPOINT" and getattr(error, "password_sent", True)
        ),
    }
    if config.profile_path:
        try:
            set_profile_port(config.profile_path, port)
            correction["profile_updated"] = True
        except (OSError, ProfileError):
            # An uncorrected file is not a failed connect: the session is on
            # the right port either way, and the next start re-detects.
            correction["profile_updated"] = False
    correction["detail"] = _describe_port_correction(correction, config)
    return correction


def _describe_port_correction(correction: Mapping[str, Any], config: ServerConfig) -> str:
    """State what moved, what it means, and what the operator still has to do."""

    lines = [
        "The pinned PLAXIS %s port %s was wrong; this installation's %s is listening "
        "on %s, so the connection was repinned to it."
        % (
            config.role.value,
            correction["previous_port"],
            config.role.value,
            correction["port"],
        )
    ]
    if correction["profile_updated"]:
        lines.append("The profile now records port %s." % correction["port"])
    else:
        lines.append(
            "The profile could not be updated, so this correction lasts for this "
            "session only. Make it permanent with `plaxis-mcp setup --%s-port %s "
            "--skip-credentials`." % (config.role.value, correction["port"])
        )
    if correction["credential_exposed"]:
        lines.append(
            "A different service was answering on port %s and the scripting handshake "
            "had already sent the password to it. Treat this endpoint's password as "
            "exposed and rotate it in PLAXIS, then store the new one with "
            "`plaxis-mcp credentials set`." % correction["previous_port"]
        )
    return " ".join(lines)


def _connect_session(session: Any, config: ServerConfig) -> dict[str, Any]:
    """Connect a bridge zero-argument session or a temporary legacy fake."""

    if isinstance(session, BridgeSession) or callable(getattr(session, "invoke", None)):
        try:
            _refuse_unidentified_endpoint(session, config)
            return session.connect()
        except BridgeError as error:
            correction = _correct_pinned_port(session, config, error)
            if correction is None:
                raise
            try:
                result = session.connect()
            except BridgeError as retry_error:
                # The pin moved even though the connect went on to fail for an
                # unrelated reason. Staying silent would leave the operator
                # with a changed profile and an error that never mentions it.
                retry_error.args = (
                    "%s %s" % (str(retry_error), correction["detail"]),
                )
                raise
            if isinstance(result, Mapping):
                result = dict(result)
                result["port_correction"] = correction
            return result
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
# Switches which existing phase/mode is current; does not create, destroy, or
# mutate model content, and repeating the same switch is a no-op.
_MODE_SWITCH = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)
# activate/deactivate/set_phase_property mutate staged-construction state.
# Repeating the same call reaches the same end state (idempotent, like
# set_property), but every mutator here still defaults to destructiveHint so
# no client treats it as safe to auto-approve.
_STAGE_MUTATOR = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=True,
    idempotentHint=True,
    openWorldHint=False,
)
# view_results changes no model content -- it hands a calculated phase to the
# separate PLAXIS Output application. Not read-only (it drives another
# process), not destructive, and asking twice for the same phase is a no-op.
_VIEW_RESULTS = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=True,
)


def _lock_input_schemas(server: MCPServer) -> None:
    """Make the SDK-generated argument models reject unknown properties.

    MCPServer (mcp 2.0) deliberately uses permissive Pydantic argument models.
    The deployed contract is stricter: every advertised tool schema is closed,
    so old and new clients receive the same invalid-argument behavior.
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


def _credential_remediation(config: ServerConfig) -> str:
    """Name the only channel that can replace a rejected password."""

    try:
        profile = str(profile_path_for_role(config.role.value))
    except Exception:
        # A missing LOCALAPPDATA must not cost the caller the advice itself.
        profile = "%s.toml" % config.role.value
    return (
        "Run `plaxis-mcp credentials set --profile %s` in a terminal a person is typing "
        "at -- the prompt reads the console directly, so an agent's shell hangs instead "
        "of failing. It prompts with hidden input and stores the password in Windows "
        "Credential Manager. Never request the password in chat or as a tool argument."
        % profile
    )


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
    # PLAXIS's own words about the failure, and any structured evidence the
    # operation rebuilt, travel as their own fields (WP-A/WP-F). Present only
    # when there is something to say, so an unchanged failure keeps its
    # existing shape.
    plaxis_message = getattr(error, "plaxis_message", None)
    if plaxis_message:
        payload["plaxis_message"] = plaxis_message
        if getattr(error, "plaxis_message_truncated", False):
            payload["plaxis_message_truncated"] = True
    details = getattr(error, "details", None)
    if details:
        payload["details"] = details
    # A rejected password has no in-session remedy, and an agent left to invent
    # one offered to accept the password in chat -- disclosing it to the
    # transcript and the model provider, and uselessly, since connect takes no
    # password argument. Carry the one channel that works.
    if code == "AUTHENTICATION_FAILED":
        payload["remediation"] = _credential_remediation(config)
    return mcp_types.CallToolResult(
        content=[mcp_types.TextContent(type="text", text=json.dumps(payload, separators=(",", ":")))],
        structuredContent=payload,
        isError=True,
    )


def _install_bridge_error_handlers(
    server: MCPServer,
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


def _worker_lifespan(session: Any) -> Callable[[MCPServer], Any]:
    """Ensure stdin EOF and server shutdown cannot orphan a worker process."""

    @asynccontextmanager
    async def lifespan(_: MCPServer):
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
) -> MCPServer:
    """Create a role-specific MCPServer server without importing PLAXIS SDKs."""

    config = config or load_server_config()
    session = _construct_session(config, session_factory)
    server = MCPServer(
        "plaxis-mcp",
        # mcp 2.0 exposes the reported server-info version as a constructor
        # argument, so the old private poke into _mcp_server.version is gone.
        version=__version__,
        instructions=(
            "Connect only to the pinned local PLAXIS %s endpoint. "
            "Require client approval before mutation tools."
        ) % config.role.value,
        lifespan=_worker_lifespan(session),
    )
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

        @server.tool(annotations=_READ, structured_output=True)
        def list_objects(
            kind: Literal[
                "point", "line", "polygon", "soil", "soillayer", "borehole",
                "plate", "geogrid", "anchor", "embedded_beam",
            ],
            offset: Annotated[StrictInt, Field(ge=0)] = 0,
            limit: Annotated[StrictInt, Field(ge=1, le=2000)] = 200,
        ) -> dict[str, Any]:
            """List the model objects of one kind, with the path to pass back.

            Each object carries `path` (the exact string every reference
            parameter accepts), its identification, and a short geometry or
            material summary -- enough to pick the object you mean without
            probing members one at a time.
            """

            return _invoke(session, "list_objects", kind=kind, offset=offset, limit=limit)

        @server.tool(annotations=_READ, structured_output=True)
        def model_state() -> dict[str, Any]:
            """Report whether the model can be calculated, and what is stopping it.

            Read-only. Returns mesh status and element counts, the phase table
            with each phase's marked-for-calculation flag, and any features
            with no material assigned. `blocking` lists what would stop a
            calculate; `unknown` lists the checks that could not be evaluated
            on this endpoint, so an empty `blocking` is never mistaken for a
            clean bill of health.
            """

            return _invoke(session, "model_state")

        @server.tool(annotations=_SET_PROPERTY, structured_output=True)
        def set_property(
            path: Annotated[StrictStr, Field(min_length=1)],
            value: Annotated[
                ScalarValue,
                Field(
                    description=(
                        "A single scalar: number, string, boolean, or null. To point a "
                        "feature at a material use assign_material; to reference any other "
                        "object, pass its `path` string to a tool that takes references."
                    )
                ),
            ],
        ) -> dict[str, Any]:
            """Set a writable PLAXIS property using a dotted path.

            For a material reference such as Plate_1.Material use
            assign_material instead -- PLAXIS needs a real object there, and no
            string spelling of the material's name works through this tool.
            Setting Identification renames the object, which changes the `path`
            every later call must use.
            """

            return _invoke(session, "set_property", path=path, value=value)

        @server.tool(annotations=_CALL_METHOD, structured_output=True)
        def call_method(
            method: Annotated[StrictStr, Field(min_length=1)],
            path: StrictStr = "",
            args: list[ScalarValue] | None = None,
        ) -> dict[str, Any]:
            """Call an explicitly named PLAXIS method with literal positional arguments.

            Every argument must be a scalar literal (number, string, boolean,
            or null) -- there is no way to pass a reference to another PLAXIS
            object or phase through this tool. A string argument is sent to
            PLAXIS as a quoted text literal, not as a reference, so it cannot
            stand in for an object or phase parameter even when it names one.
            Use this only for methods whose PLAXIS command reference shows
            purely literal parameters; prefer list_objects, model_state, and
            the first-class tools over probing with dump.
            """

            return _invoke(session, "call_method", path=path, method=method, args=args or [])

        @server.tool(annotations=_PROJECT_MUTATOR, structured_output=True)
        def new_project() -> dict[str, Any]:
            """Create a new PLAXIS project."""

            return _invoke(session, "new_project")

        @server.tool(annotations=_PROJECT_MUTATOR, structured_output=True)
        def open_project(filename: Annotated[StrictStr, Field(min_length=1)]) -> dict[str, Any]:
            """Open a PLAXIS project.

            An absolute path is used as given; a relative path (a bare filename)
            resolves against the working directory, so "model.p2dx" opens the
            project in the directory the session was started in. The result
            reports the resolved absolute path.
            """

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
            """Save the active project, optionally to a new path.

            Omit filename to save the open project in place. An absolute
            filename is used as given; a relative one (a bare "model.p2dx")
            saves into the working directory the session was started in, not
            PLAXIS's own directory. The result reports the resolved absolute
            path.
            """

            return _invoke(session, "save_project", filename=filename)

        @server.tool(annotations=_PROJECT_MUTATOR, structured_output=True)
        def create_phase(
            reference_phase: Annotated[StrictStr, Field(min_length=1)],
            position: Literal["after", "before"] = "after",
        ) -> dict[str, Any]:
            """Create a new phase relative to an existing one.

            PLAXIS always has an InitialPhase; the first real phase is
            typically created with reference_phase="InitialPhase".
            """

            return _invoke(session, "create_phase", reference_phase=reference_phase, position=position)

        @server.tool(annotations=_MODE_SWITCH, structured_output=True)
        def set_current_phase(phase: Annotated[StrictStr, Field(min_length=1)]) -> dict[str, Any]:
            """Make an existing phase the current phase for further edits."""

            return _invoke(session, "set_current_phase", phase=phase)

        @server.tool(annotations=_STAGE_MUTATOR, structured_output=True)
        def activate(
            paths: Annotated[list[StrictStr], Field(min_length=1)],
            phases: Annotated[list[StrictStr], Field(min_length=1)],
        ) -> dict[str, Any]:
            """Activate one or more objects in one or more phases."""

            return _invoke(session, "activate", paths=paths, phases=phases)

        @server.tool(annotations=_STAGE_MUTATOR, structured_output=True)
        def deactivate(
            paths: Annotated[list[StrictStr], Field(min_length=1)],
            phases: Annotated[list[StrictStr], Field(min_length=1)],
        ) -> dict[str, Any]:
            """Deactivate one or more objects in one or more phases."""

            return _invoke(session, "deactivate", paths=paths, phases=phases)

        @server.tool(annotations=_PROJECT_MUTATOR, structured_output=True)
        def calculate(phases: list[Annotated[StrictStr, Field(min_length=1)]] | None = None) -> dict[str, Any]:
            """Calculate phases.

            Omit phases to calculate every phase currently marked for
            calculation; provide phases to calculate exactly those phases
            regardless of their marked-for-calculation state.
            """

            return _invoke(session, "calculate", phases=phases)

        @server.tool(annotations=_VIEW_RESULTS, structured_output=True)
        def view_results(phase: Annotated[StrictStr, Field(min_length=1)]) -> dict[str, Any]:
            """Open a calculated phase in the PLAXIS Output application.

            This is the only route from a calculated Input model to the Output
            endpoint's results: Output reads whatever its application has open,
            and the Output role deliberately has no open_project. Call this for
            a phase whose status is "calculated", then read results through the
            Output server.
            """

            return _invoke(session, "view_results", phase=phase)

        @server.tool(annotations=_STAGE_MUTATOR, structured_output=True)
        def set_phase_property(
            path: Annotated[StrictStr, Field(min_length=1)],
            property: Annotated[StrictStr, Field(min_length=1)],
            phase: Annotated[StrictStr, Field(min_length=1)],
            value: ScalarValue,
        ) -> dict[str, Any]:
            """Set a property of an object for one specific phase only.

            This is PLAXIS's phase-scoped setproperties form, distinct from
            set_property, which changes a property without phase scoping.
            """

            return _invoke(session, "set_phase_property", path=path, property=property, phase=phase, value=value)

        @server.tool(annotations=_MODE_SWITCH, structured_output=True)
        def set_mode(
            mode: Literal["soil", "structures", "mesh", "water", "stages"],
            force_new_intersection: StrictBool | None = None,
        ) -> dict[str, Any]:
            """Switch the PLAXIS Input working mode.

            force_new_intersection is only meaningful for mode "mesh",
            "water", or "stages"; it is rejected for "soil" or "structures".
            """

            return _invoke(session, "set_mode", mode=mode, force_new_intersection=force_new_intersection)

        @server.tool(annotations=_PROJECT_MUTATOR, structured_output=True)
        def generate_mesh(
            relative_element_size: Annotated[StrictFloat, Field(gt=0)] | None = None,
            enhanced_refinement: StrictBool | None = None,
        ) -> dict[str, Any]:
            """Generate the mesh, optionally with a relative element size factor.

            enhanced_refinement requires relative_element_size to also be given.
            """

            return _invoke(
                session, "generate_mesh",
                relative_element_size=relative_element_size, enhanced_refinement=enhanced_refinement,
            )

        @server.tool(annotations=_PROJECT_MUTATOR, structured_output=True)
        def create_point(
            coordinates: Annotated[list[list[StrictFloat]], Field(min_length=1)],
        ) -> dict[str, Any]:
            """Create one or more points at [x, y] coordinates."""

            return _invoke(session, "create_point", coordinates=coordinates)

        @server.tool(annotations=_PROJECT_MUTATOR, structured_output=True)
        def create_line(
            vertices: Annotated[list[list[StrictFloat] | StrictStr], Field(min_length=2)],
        ) -> dict[str, Any]:
            """Create a line, or a polyline through 3 or more vertices.

            Each vertex is either an [x, y] coordinate or the path of an
            existing point. Only the vertex-list form of the line command is
            implemented; the angle/relative/vector sub-command form is not.
            """

            return _invoke(session, "create_line", vertices=vertices)

        @server.tool(annotations=_PROJECT_MUTATOR, structured_output=True)
        def create_polygon(
            vertices: Annotated[list[list[StrictFloat] | StrictStr], Field(min_length=3)],
        ) -> dict[str, Any]:
            """Create a polygon from 3 or more vertices.

            Each vertex is either an [x, y] coordinate or the path of an
            existing point. Only the vertex-list form is implemented; the
            polycurve-based form is not.
            """

            return _invoke(session, "create_polygon", vertices=vertices)

        @server.tool(annotations=_PROJECT_MUTATOR, structured_output=True)
        def create_borehole(x: StrictFloat) -> dict[str, Any]:
            """Add a borehole at the given x-coordinate."""

            return _invoke(session, "create_borehole", x=x)

        @server.tool(annotations=_PROJECT_MUTATOR, structured_output=True)
        def create_soillayer(height: StrictFloat) -> dict[str, Any]:
            """Add a soil layer with the given height."""

            return _invoke(session, "create_soillayer", height=height)

        @server.tool(annotations=_PROJECT_MUTATOR, structured_output=True)
        def create_material(
            kind: Literal["soil", "plate", "geogrid", "anchor", "embedded_beam"],
            properties: dict[str, ScalarValue] | None = None,
        ) -> dict[str, Any]:
            """Create a material set.

            Omit properties to create a placeholder material that cannot be
            used to calculate until edited; provide a property/value map
            (matching PLAXIS's own property names, e.g. "SoilModel",
            "gammaUnsat") to create a usable material in one call. Property
            names are checked against the names PLAXIS 2D V22 exposes for that
            kind and are case-sensitive.

            "Identification" renames the material: after
            create_material(kind="soil", properties={"Identification": "SZ"}),
            the object's path is "SZ", and that is what every later call --
            assign_material included -- must use.
            """

            return _invoke(session, "create_material", kind=kind, properties=properties)

        @server.tool(annotations=_PROJECT_MUTATOR, structured_output=True)
        def assign_material(
            paths: Annotated[list[StrictStr], Field(min_length=1)],
            material: Annotated[StrictStr, Field(min_length=1)],
        ) -> dict[str, Any]:
            """Assign a material to one or more features."""

            return _invoke(session, "assign_material", paths=paths, material=material)

        @server.tool(annotations=_PROJECT_MUTATOR, structured_output=True)
        def create_structural_element(
            kind: Literal["plate", "geogrid", "embedded_beam", "n2n_anchor"],
            paths: Annotated[list[StrictStr], Field(min_length=1)],
        ) -> dict[str, Any]:
            """Add a structural element feature to one or more existing lines.

            Only the add-to-existing-line form is implemented; creating a new
            line inline, or element-specific extra overloads, are not. Create
            the line first with create_line.
            """

            return _invoke(session, "create_structural_element", kind=kind, paths=paths)

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

        @server.tool(annotations=_READ, structured_output=True)
        def get_single_result(
            phase: StrictInt | StrictStr,
            result_type_path: Annotated[StrictStr, Field(min_length=1)],
            point: Annotated[StrictStr, Field(min_length=1)] | None = None,
            coords: Annotated[list[StrictFloat], Field(min_length=2, max_length=2)] | None = None,
            smoothing: StrictBool | None = None,
        ) -> dict[str, Any]:
            """Fetch one result value at a named point or an (x, y) coordinate.

            Specify exactly one of point (an existing curve point's path) or
            coords ([x, y]). Only these two lookup forms are implemented;
            directional smoothing and updated-mesh-geometry coordinate lookup
            are not -- use get_results for those cases.
            """

            return _invoke(
                session, "get_single_result",
                phase=phase, result_type_path=result_type_path,
                point=point, coords=coords, smoothing=smoothing,
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
        # A connect that had to move the pin says so: the port in this result is
        # not the one the operator configured, and it may carry a disclosure.
        if "port_correction" in result:
            normalized["port_correction"] = result["port_correction"]
        return normalized
    return _normalized_status(session, config)


mcp: MCPServer | None = None


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

    arguments = list(sys.argv[1:] if argv is None else argv)
    # Reporting the version starts no server, so it stays available even where
    # the transport is misconfigured; the packaging preflight relies on it.
    if arguments and arguments[0] in ("--version", "-V"):
        print("plaxis-mcp %s" % __version__)
        return 0

    transport = os.getenv("PLAXIS_MCP_TRANSPORT", "stdio")
    if transport != "stdio":
        raise ServerConfigurationError("PLAXIS-MCP supports the stdio transport only.")
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
    if arguments[0] == "setup":
        return setup_main(arguments[1:])

    parser = argparse.ArgumentParser(prog="plaxis-mcp")
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="Start a role-pinned stdio MCP server.")
    profile_source = serve.add_mutually_exclusive_group(required=True)
    profile_source.add_argument("--config", type=Path, help="Generated input.toml or output.toml")
    profile_source.add_argument(
        "--role",
        choices=("input", "output"),
        help="Use this role's generated profile from the profiles directory",
    )
    serve.add_argument(
        "--profile-dir",
        type=Path,
        default=None,
        help="Profiles directory for --role (default: %%LOCALAPPDATA%%\\Caros\\PLAXIS-MCP\\profiles)",
    )
    # Registered for --help discoverability only; all three are dispatched above.
    commands.add_parser("credentials", help="Store or remove a PLAXIS role password.")
    commands.add_parser("profiles", help="Discover PLAXIS and generate role profiles.")
    commands.add_parser("setup", help="Generate both profiles and store both passwords.")
    namespace = parser.parse_args(arguments)
    if namespace.config is not None and namespace.profile_dir is not None:
        parser.error("--profile-dir applies to --role; --config already names a profile file.")

    _reject_profile_environment_overrides(os.environ)
    profile_path = (
        namespace.config
        if namespace.config is not None
        else profile_path_for_role(namespace.role, namespace.profile_dir)
    )
    if not Path(profile_path).is_file():
        raise ServerConfigurationError(
            "No PLAXIS-MCP profile at %s. Run `plaxis-mcp setup` once on this machine."
            % profile_path
        )
    return _run_stdio(
        server_config_from_profile(load_profile(profile_path), profile_path=profile_path)
    )


if __name__ == "__main__":
    raise SystemExit(main())
