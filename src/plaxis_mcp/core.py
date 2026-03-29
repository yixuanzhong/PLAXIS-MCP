from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from typing import Any, Callable


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 10000
DEFAULT_TIMEOUT = 5.0


class PlaxisConnectionError(RuntimeError):
    """Raised when the MCP server cannot connect to PLAXIS."""


def _maybe_add_plaxis_scripting_path() -> None:
    extra_path = os.getenv("PLAXIS_SCRIPTING_PATH")
    if extra_path and extra_path not in sys.path:
        sys.path.insert(0, extra_path)


def load_new_server() -> Callable[..., Any]:
    """Import and return the upstream plxscripting connection factory."""
    _maybe_add_plaxis_scripting_path()
    try:
        from plxscripting.easy import new_server
    except ImportError as exc:
        raise PlaxisConnectionError(
            "Unable to import 'plxscripting'. Install project dependencies or set "
            "PLAXIS_SCRIPTING_PATH to the PLAXIS scripting folder."
        ) from exc
    return new_server


def parse_path(path: str) -> list[Any]:
    """Parse a dotted PLAXIS path with optional list indexes."""
    if not path:
        return []

    tokens: list[Any] = []
    current = []
    i = 0
    while i < len(path):
        char = path[i]
        if char == ".":
            if current:
                tokens.append("".join(current))
                current = []
            i += 1
            continue
        if char == "[":
            if current:
                tokens.append("".join(current))
                current = []
            end = path.find("]", i)
            if end == -1:
                raise ValueError(f"Unclosed index in path: {path}")
            raw_index = path[i + 1 : end].strip()
            if not raw_index.isdigit():
                raise ValueError(f"Only numeric indexes are supported in path: {path}")
            tokens.append(int(raw_index))
            i = end + 1
            continue
        current.append(char)
        i += 1

    if current:
        tokens.append("".join(current))
    return tokens


def resolve_path(root: Any, path: str) -> Any:
    """Resolve a PLAXIS-style path from a root object."""
    value = root
    for token in parse_path(path):
        if isinstance(token, int):
            value = value[token]
        else:
            value = getattr(value, token)
    return value


def _read_member_value(obj: Any, attr_name: str) -> Any:
    try:
        member = getattr(obj, attr_name)
    except Exception:
        return None

    if hasattr(member, "value"):
        try:
            return member.value
        except Exception:
            return None
    return member


def _safe_dir(obj: Any) -> list[str]:
    names = []
    for name in dir(obj):
        if name.startswith("_"):
            continue
        names.append(name)
    return names


def serialize_value(value: Any, *, depth: int = 0, max_depth: int = 4, max_items: int = 25) -> Any:
    """Convert PLAXIS proxy values into JSON-friendly structures."""
    if depth >= max_depth:
        return repr(value)

    if value is None or isinstance(value, (str, int, float, bool)):
        return value

    if isinstance(value, dict):
        items = list(value.items())[:max_items]
        return {str(k): serialize_value(v, depth=depth + 1, max_depth=max_depth) for k, v in items}

    if isinstance(value, (list, tuple, set)):
        items = list(value)[:max_items]
        return [serialize_value(item, depth=depth + 1, max_depth=max_depth) for item in items]

    if hasattr(value, "value"):
        try:
            return {
                "kind": "property",
                "type": value.__class__.__name__,
                "value": serialize_value(value.value, depth=depth + 1, max_depth=max_depth),
            }
        except Exception:
            pass

    is_proxy_like = hasattr(value, "_guid") or hasattr(value, "get_cmd_line_repr")
    if is_proxy_like:
        result = {
            "kind": "object",
            "type": value.__class__.__name__,
            "repr": repr(value),
            "guid": getattr(value, "_guid", None),
            "type_name": _read_member_value(value, "TypeName"),
            "name": _read_member_value(value, "Name"),
            "identification": _read_member_value(value, "Identification"),
        }
        members = _safe_dir(value)
        if members:
            result["members"] = members[:max_items]
            if len(members) > max_items:
                result["member_count"] = len(members)
        return result

    if callable(value):
        return {"kind": "callable", "repr": repr(value)}

    return {"kind": "repr", "repr": repr(value), "type": value.__class__.__name__}


@dataclass
class SessionConfig:
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    password: str = ""
    timeout: float = DEFAULT_TIMEOUT
    request_timeout: float | None = None


class PlaxisSession:
    """Manage a single live PLAXIS remote scripting session."""

    def __init__(self) -> None:
        self._server: Any | None = None
        self._global: Any | None = None
        self._config = SessionConfig()

    def connect(
        self,
        host: str | None = None,
        port: int | None = None,
        password: str | None = None,
        timeout: float | None = None,
        request_timeout: float | None = None,
    ) -> dict[str, Any]:
        new_server = load_new_server()
        config = SessionConfig(
            host=host or os.getenv("PLAXIS_HOST", self._config.host),
            port=port or int(os.getenv("PLAXIS_PORT", str(self._config.port))),
            password=password if password is not None else os.getenv("PLAXIS_PASSWORD", self._config.password),
            timeout=timeout or float(os.getenv("PLAXIS_TIMEOUT", str(self._config.timeout))),
            request_timeout=request_timeout,
        )
        if config.request_timeout is None:
            raw_request_timeout = os.getenv("PLAXIS_REQUEST_TIMEOUT")
            if raw_request_timeout:
                config.request_timeout = float(raw_request_timeout)

        try:
            server, global_object = new_server(
                address=config.host,
                port=config.port,
                timeout=config.timeout,
                request_timeout=config.request_timeout,
                password=config.password,
            )
        except Exception as exc:
            raise PlaxisConnectionError(f"Unable to connect to PLAXIS: {exc}") from exc

        self._server = server
        self._global = global_object
        self._config = config
        return self.status()

    def disconnect(self) -> dict[str, Any]:
        self._server = None
        self._global = None
        return self.status()

    def status(self) -> dict[str, Any]:
        connected = self._server is not None and self._global is not None
        return {
            "connected": connected,
            "host": self._config.host,
            "port": self._config.port,
            "timeout": self._config.timeout,
            "request_timeout": self._config.request_timeout,
        }

    def require_global(self) -> Any:
        if self._global is None:
            raise PlaxisConnectionError("Not connected. Call connect first.")
        return self._global

    def require_server(self) -> Any:
        if self._server is None:
            raise PlaxisConnectionError("Not connected. Call connect first.")
        return self._server

    def inspect(self, path: str = "") -> dict[str, Any]:
        target = resolve_path(self.require_global(), path)
        return {"path": path, "value": serialize_value(target)}

    def list_members(self, path: str = "") -> dict[str, Any]:
        target = resolve_path(self.require_global(), path)
        return {
            "path": path,
            "members": _safe_dir(target),
            "summary": serialize_value(target),
        }

    def set_property(self, path: str, value: Any) -> dict[str, Any]:
        tokens = parse_path(path)
        if not tokens:
            raise ValueError("Property path must not be empty.")
        if isinstance(tokens[-1], int):
            raise ValueError("Property path must end in an attribute name, not an index.")

        target = self.require_global()
        for token in tokens[:-1]:
            target = target[token] if isinstance(token, int) else getattr(target, token)

        setattr(target, tokens[-1], value)
        updated = getattr(target, tokens[-1])
        return {
            "path": path,
            "value": serialize_value(updated),
        }

    def call_method(self, path: str, method: str, args: list[Any] | None = None) -> dict[str, Any]:
        target = resolve_path(self.require_global(), path)
        member = getattr(target, method)
        if not callable(member):
            raise ValueError(f"'{method}' on '{path}' is not callable.")
        result = member(*(args or []))
        return {
            "path": path,
            "method": method,
            "args": args or [],
            "result": serialize_value(result),
        }

    def new_project(self) -> dict[str, Any]:
        result = self.require_server().new()
        return {"action": "new_project", "result": serialize_value(result)}

    def open_project(self, filename: str) -> dict[str, Any]:
        result = self.require_server().open(filename)
        return {"action": "open_project", "filename": filename, "result": serialize_value(result)}

    def close_project(self) -> dict[str, Any]:
        result = self.require_server().close()
        return {"action": "close_project", "result": serialize_value(result)}

    def recover_project(self) -> dict[str, Any]:
        result = self.require_server().recover()
        return {"action": "recover_project", "result": serialize_value(result)}

    def save_project(self, filename: str | None = None) -> dict[str, Any]:
        global_object = self.require_global()
        method_name = "save" if filename is None else "saveas"
        method = getattr(global_object, method_name, None)
        if method is None or not callable(method):
            raise ValueError(
                f"PLAXIS global object does not expose a callable '{method_name}' command."
            )
        args = [] if filename is None else [filename]
        result = method(*args)
        payload = {"action": "save_project", "result": serialize_value(result)}
        if filename is not None:
            payload["filename"] = filename
        return payload

    def list_phases(self) -> dict[str, Any]:
        phases = resolve_path(self.require_global(), "Phases")
        items = []
        for index, phase in enumerate(phases):
            items.append(
                {
                    "index": index,
                    "name": _read_member_value(phase, "Name"),
                    "identification": _read_member_value(phase, "Identification"),
                    "phase_type": _read_member_value(phase, "TypeName"),
                }
            )
        return {"count": len(items), "phases": items}

    def list_materials(self) -> dict[str, Any]:
        global_object = self.require_global()
        materials = None
        for candidate in ("Materials", "SoilMat", "PlateMat", "BeamMat", "InterfaceMat"):
            try:
                materials = getattr(global_object, candidate)
                break
            except Exception:
                continue

        if materials is None:
            raise ValueError("No material collection was found on the connected PLAXIS model.")

        items = []
        for index, material in enumerate(materials):
            items.append(
                {
                    "index": index,
                    "name": _read_member_value(material, "Name"),
                    "identification": _read_member_value(material, "Identification"),
                    "material_type": _read_member_value(material, "TypeName"),
                }
            )
        return {"count": len(items), "materials": items}

    def project_info(self) -> dict[str, Any]:
        global_object = self.require_global()
        info = {
            "project_title": _read_member_value(global_object, "ProjectTitle"),
            "project_description": _read_member_value(global_object, "ProjectDescription"),
            "filename": _read_member_value(global_object, "Filename"),
            "unit_force": _read_member_value(global_object, "UnitForce"),
            "unit_length": _read_member_value(global_object, "UnitLength"),
            "unit_time": _read_member_value(global_object, "UnitTime"),
        }
        return info
