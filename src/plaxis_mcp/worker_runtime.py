"""Python 3.7-compatible runtime used inside a PLAXIS Python bundle.

Do not add modern Python syntax here: this module is deliberately exercised
under PLAXIS V21's Python 3.7.4 as well as newer PLAXIS bundle interpreters.
"""

from __future__ import print_function

import datetime
import json
import math
import os
import sys


PROTOCOL_VERSION = 1
COMMON_OPERATIONS = (
    "connection_status", "list_members", "inspect", "project_info", "list_phases", "list_materials",
)
INPUT_OPERATIONS = COMMON_OPERATIONS + (
    "set_property", "call_method", "new_project", "open_project", "close_project", "recover_project", "save_project",
)
OUTPUT_OPERATIONS = COMMON_OPERATIONS + ("list_result_types", "get_results")
ROLE_OPERATION_ALLOWLISTS = {"input": INPUT_OPERATIONS, "output": OUTPUT_OPERATIONS}
ROLE_TOOL_ALLOWLISTS = {
    "input": ("connect", "disconnect") + INPUT_OPERATIONS,
    "output": ("connect", "disconnect") + OUTPUT_OPERATIONS,
}
TRANSPORT_OPERATIONS = ("handshake", "status", "connect", "disconnect", "call", "shutdown")
_SECRET_MARKERS = ("PASSWORD", "SECRET", "TOKEN", "CREDENTIAL", "API_KEY")


class WorkerError(Exception):
    """Safe operational failure sent to the host as a JSON error."""

    def __init__(self, code, message, retryable=False):
        super(WorkerError, self).__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


def parse_path(path):
    if not isinstance(path, str):
        raise WorkerError("INVALID_PATH", "PLAXIS object paths must be strings.")
    if not path:
        return []
    tokens, current, index = [], [], 0
    while index < len(path):
        character = path[index]
        if character == ".":
            if not current:
                raise WorkerError("INVALID_PATH", "PLAXIS paths cannot contain empty member names.")
            tokens.append(_public_member_name("".join(current), "path member"))
            current = []
            index += 1
            continue
        if character == "[":
            if current:
                tokens.append(_public_member_name("".join(current), "path member"))
                current = []
            end = path.find("]", index)
            raw_index = path[index + 1 : end].strip() if end != -1 else ""
            if not raw_index.isdigit():
                raise WorkerError("INVALID_PATH", "PLAXIS path indexes must be numeric and closed.")
            tokens.append(int(raw_index))
            index = end + 1
            continue
        current.append(character)
        index += 1
    if current:
        tokens.append(_public_member_name("".join(current), "path member"))
    return tokens


def _public_member_name(value, label):
    if not isinstance(value, str) or not value or value.startswith("_"):
        raise WorkerError("INVALID_PATH", "PLAXIS %s must be a public member name." % label)
    return value


def resolve_path(root, path):
    value = root
    for token in parse_path(path):
        try:
            value = value[token] if isinstance(token, int) else getattr(value, token)
        except Exception:
            raise WorkerError("INVALID_PATH", "PLAXIS object path could not be resolved.")
    return value


def read_member_value(obj, name):
    try:
        value = getattr(obj, name)
    except Exception:
        return None
    if hasattr(value, "value"):
        try:
            return value.value
        except Exception:
            return None
    return value


def safe_dir(obj):
    try:
        return [name for name in dir(obj) if isinstance(name, str) and not name.startswith("_")]
    except Exception:
        return []


def describe_value(value, depth=0, max_depth=4, max_items=100):
    """Describe vendor values without returning proxies, reprs, or tracebacks."""
    if depth >= max_depth:
        return {"kind": "truncated"}
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else {"kind": "non_finite_number"}
    if isinstance(value, dict):
        items = list(value.items())
        result = {str(key): describe_value(item, depth + 1, max_depth, max_items) for key, item in items[:max_items]}
        if len(items) > max_items:
            result["_truncated"] = True
        return result
    if isinstance(value, (list, tuple, set)):
        values = list(value)
        result = [describe_value(item, depth + 1, max_depth, max_items) for item in values[:max_items]]
        if len(values) > max_items:
            result.append({"kind": "truncated", "count": len(values)})
        return result
    if hasattr(value, "value"):
        try:
            return {"kind": "property", "value": describe_value(value.value, depth + 1, max_depth, max_items)}
        except Exception:
            pass
    if hasattr(value, "_guid") or hasattr(value, "get_cmd_line_repr"):
        result = {"kind": "object", "type": value.__class__.__name__}
        for source, target in (("_guid", "guid"), ("TypeName", "type_name"), ("Name", "name"), ("Identification", "identification")):
            member = read_member_value(value, source)
            if member is None or isinstance(member, (str, bool, int, float)):
                result[target] = member
        members = safe_dir(value)
        if members:
            # Never drop members silently: the caller must be able to tell a
            # complete member list from a clipped one.
            result["members"] = members[:max_items]
            result["member_count"] = len(members)
            if len(members) > max_items:
                result["members_truncated"] = True
        return result
    return {"kind": "unavailable", "type": value.__class__.__name__}


def resolve_phase(global_object, identifier):
    if identifier is None:
        raise WorkerError("INVALID_ARGUMENT", "A PLAXIS phase identifier is required.")
    try:
        phases = global_object.Phases
    except Exception:
        raise WorkerError("PLAXIS_ERROR", "The PLAXIS model does not expose phases.")
    if isinstance(identifier, int) and not isinstance(identifier, bool):
        try:
            return phases[identifier]
        except Exception:
            raise WorkerError("INVALID_ARGUMENT", "The requested PLAXIS phase was not found.")
    if isinstance(identifier, str) and identifier.isdigit():
        try:
            return phases[int(identifier)]
        except Exception:
            raise WorkerError("INVALID_ARGUMENT", "The requested PLAXIS phase was not found.")
    if isinstance(identifier, str) and identifier:
        try:
            return getattr(global_object, identifier)
        except Exception:
            pass
        for phase in phases:
            if read_member_value(phase, "Name") == identifier or read_member_value(phase, "Identification") == identifier:
                return phase
    raise WorkerError("INVALID_ARGUMENT", "The requested PLAXIS phase was not found.")


class CallDispatcher(object):
    """Explicit registry used by the worker's ``call`` operation.

    Handlers receive ``(server, global_object, params)``.  Keeping the
    registry explicit prevents an MCP client from selecting an arbitrary
    Python callable merely by sending a method name.
    """

    def __init__(self, handlers=None, allowed_operations=None):
        self._handlers = dict(handlers or {})
        self._allowed_operations = None
        self._allowed_order = ()
        self.set_allowed_operations(allowed_operations)

    def register(self, name, handler, replace=True):
        if not isinstance(name, str) or not name:
            raise ValueError("Worker handler names must be non-empty strings.")
        if not callable(handler):
            raise TypeError("Worker handlers must be callable.")
        if replace or name not in self._handlers:
            self._handlers[name] = handler

    def has_handler(self, name):
        return name in self._handlers

    def set_allowed_operations(self, allowed_operations):
        self._allowed_operations = None if allowed_operations is None else set(allowed_operations)
        self._allowed_order = () if allowed_operations is None else tuple(allowed_operations)

    def available_operations(self):
        if self._allowed_operations is None:
            return tuple(sorted(self._handlers))
        return tuple(name for name in self._allowed_order if name in self._handlers)

    def dispatch(self, method, params, server, global_object):
        if self._allowed_operations is not None and method not in self._allowed_operations:
            raise WorkerError("WRONG_ROLE", "The requested PLAXIS operation is not available for this endpoint role.")
        handler = self._handlers.get(method)
        if handler is None:
            raise WorkerError(
                "UNSUPPORTED_OPERATION",
                "The requested PLAXIS operation is not enabled by this worker.",
            )
        return handler(server, global_object, params)


class WorkerRuntime(object):
    """A single-threaded JSON-lines dispatcher with no PLAXIS host imports."""

    def __init__(self, role=None, new_server_factory=None, dispatcher=None):
        self.role = role or os.environ.get("PLAXIS_ROLE", "unknown")
        self._new_server_factory = new_server_factory
        self._dispatcher = dispatcher or CallDispatcher()
        self._server = None
        self._global_object = None
        self._config = {"host": None, "port": None, "timeout": None, "request_timeout": None}
        self._state = "disconnected"
        self._current_operation = None
        self._secret_values = set()
        self._shutdown_requested = False
        self._generation = 0
        self._last_error = None
        self._last_transition_at = self._timestamp()
        self._register_default_operations()
        self._dispatcher.set_allowed_operations(ROLE_OPERATION_ALLOWLISTS.get(self.role, ()))

    def serve(self, input_stream=None, output_stream=None):
        input_stream = input_stream or sys.stdin
        output_stream = output_stream or sys.stdout
        for raw_line in input_stream:
            response = self.handle_line(raw_line)
            self.write_response(output_stream, response)
            if self._shutdown_requested:
                break

    def handle_line(self, raw_line):
        try:
            request = json.loads(raw_line)
        except (TypeError, ValueError):
            return self._error_response(None, "INVALID_REQUEST", "Worker request was not valid JSON.")
        return self.handle_request(request)

    def handle_request(self, request):
        request_id = request.get("id") if isinstance(request, dict) else None
        try:
            self._validate_request(request)
            operation = request["operation"]
            params = request["params"]
            self._remember_secrets(params)
            self._current_operation = operation
            if operation == "handshake":
                result = self._handshake()
            elif operation == "status":
                result = self.status()
            elif operation == "connect":
                result = self.connect(params)
            elif operation == "disconnect":
                result = self.disconnect()
            elif operation == "call":
                result = self.call(params)
            elif operation == "shutdown":
                result = self.disconnect()
                self._shutdown_requested = True
            else:
                raise WorkerError("UNKNOWN_OPERATION", "Worker operation is not supported.")
            return self._success_response(request_id, self._json_value(result))
        except WorkerError as exc:
            return self._error_response(request_id, exc.code, exc.message, exc.retryable)
        except Exception:
            # Deliberately do not expose vendor exceptions, tracebacks, proxy
            # reprs, or potentially embedded credentials to the host.
            return self._error_response(
                request_id,
                "PLAXIS_ERROR",
                "The PLAXIS worker operation failed.",
            )
        finally:
            self._current_operation = None

    def write_response(self, output_stream, response):
        encoded = json.dumps(response, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        output_stream.write(encoded + "\n")
        output_stream.flush()

    def status(self):
        connected = self._server is not None and self._global_object is not None
        server_active = None
        server_full_name = None
        if self._server is not None:
            server_active = self._safe_attestation_value(read_member_value(self._server, "active"))
            server_full_name = self._safe_attestation_value(read_member_value(self._server, "server_full_name"))
            if server_active is False:
                self._transition("uncertain", "SERVER_INACTIVE")
                connected = False
        return {
            "role": self.role,
            "state": self._state,
            "connected": connected,
            "host": self._config["host"],
            "port": self._config["port"],
            "timeout": self._config["timeout"],
            "request_timeout": self._config["request_timeout"],
            "server_active": server_active,
            "server_full_name": server_full_name,
            "generation": self._generation,
            "last_error": self._last_error,
            "last_transition_at": self._last_transition_at,
            "current_operation": self._current_operation,
        }

    def connect(self, params):
        host = self._required_string(params, "host")
        password = self._required_string(params, "password", allow_empty=True)
        port = self._required_port(params)
        timeout = self._optional_number(params, "timeout")
        request_timeout = self._optional_number(params, "request_timeout")
        self._secret_values.add(password)

        if self._server is not None:
            self.disconnect()
        self._transition("connecting")
        try:
            factory = self._new_server_factory or self._load_new_server()
            server, global_object = factory(
                address=host,
                port=port,
                timeout=timeout,
                request_timeout=request_timeout,
                password=password,
            )
        except WorkerError:
            self._transition("disconnected", "CONNECTION_FAILED")
            raise
        except Exception:
            self._transition("disconnected", "CONNECTION_FAILED")
            raise WorkerError("CONNECTION_FAILED", "Unable to connect to the PLAXIS scripting server.")

        self._server = server
        self._global_object = global_object
        self._config = {
            "host": host,
            "port": port,
            "timeout": timeout,
            "request_timeout": request_timeout,
        }
        self._generation += 1
        self._transition("connected")
        result = self.status()
        if result["server_active"] is False:
            self._close_session(self._server)
            self._server = None
            self._global_object = None
            self._transition("disconnected", "SERVER_INACTIVE")
            raise WorkerError("CONNECTION_FAILED", "The PLAXIS scripting server is not active.")
        return result

    def disconnect(self):
        self._close_session(self._server)
        self._server = None
        self._global_object = None
        self._transition("disconnected")
        return self.status()

    def call(self, params):
        if self._server is None or self._global_object is None:
            raise WorkerError("NOT_CONNECTED", "Connect to PLAXIS before invoking an operation.")
        if not isinstance(params, dict):
            raise WorkerError("INVALID_ARGUMENT", "Worker call parameters must be an object.")
        method = self._required_string(params, "method")
        operation_params = params.get("params", {})
        if not isinstance(operation_params, dict):
            raise WorkerError("INVALID_ARGUMENT", "Worker call arguments must be an object.")
        mutating_operation = method in (
            "set_property", "call_method", "new_project", "open_project",
            "close_project", "recover_project", "save_project",
        )
        failure_code = None
        self._transition("busy")
        try:
            result = self._dispatcher.dispatch(method, operation_params, self._server, self._global_object)
            if mutating_operation:
                self._generation += 1
                if isinstance(result, dict):
                    result["generation"] = self._generation
        except WorkerError as exc:
            failure_code = exc.code
            raise
        except Exception:
            failure_code = "PLAXIS_ERROR"
            raise WorkerError("PLAXIS_ERROR", "The requested PLAXIS operation failed.")
        finally:
            if self._server is not None:
                self._transition("connected", failure_code)
        return result

    def _register_default_operations(self):
        handlers = {
            "connection_status": lambda server, global_object, params: self.status(),
            "list_members": lambda server, global_object, params: self._list_members(global_object, params),
            "inspect": lambda server, global_object, params: self._inspect(global_object, params),
            "project_info": lambda server, global_object, params: self._project_info(global_object),
            "list_phases": lambda server, global_object, params: self._list_phases(global_object),
            "list_materials": lambda server, global_object, params: self._list_materials(global_object),
            "set_property": lambda server, global_object, params: self._set_property(global_object, params),
            "call_method": lambda server, global_object, params: self._call_method(global_object, params),
            "new_project": lambda server, global_object, params: self._new_project(server),
            "open_project": lambda server, global_object, params: self._open_project(server, params),
            "close_project": lambda server, global_object, params: self._close_project(server),
            "recover_project": lambda server, global_object, params: self._recover_project(server),
            "save_project": lambda server, global_object, params: self._save_project(global_object, params),
            "list_result_types": lambda server, global_object, params: self._list_result_types(global_object, params),
            "get_results": lambda server, global_object, params: self._get_results(global_object, params),
        }
        for name, handler in handlers.items():
            self._dispatcher.register(name, handler, replace=False)

    def _inspect(self, global_object, params):
        path = self._optional_string(params, "path", "")
        return {"path": path, "value": describe_value(resolve_path(global_object, path))}

    def _list_members(self, global_object, params):
        path = self._optional_string(params, "path", "")
        target = resolve_path(global_object, path)
        return {"path": path, "members": safe_dir(target), "summary": describe_value(target)}

    def _set_property(self, global_object, params):
        path = self._required_string(params, "path")
        tokens = parse_path(path)
        if not tokens or isinstance(tokens[-1], int):
            raise WorkerError("INVALID_PATH", "Property paths must end with an attribute name.")
        target = global_object
        for token in tokens[:-1]:
            try:
                target = target[token] if isinstance(token, int) else getattr(target, token)
            except Exception:
                raise WorkerError("INVALID_PATH", "PLAXIS property path could not be resolved.")
        try:
            setattr(target, tokens[-1], params.get("value"))
            value = getattr(target, tokens[-1])
        except Exception:
            raise WorkerError("PLAXIS_ERROR", "PLAXIS rejected the requested property update.")
        return {"path": path, "value": describe_value(value)}

    def _call_method(self, global_object, params):
        path = self._optional_string(params, "path", "")
        method = self._required_string(params, "method")
        _public_member_name(method, "method")
        args = params.get("args", [])
        if not isinstance(args, list):
            raise WorkerError("INVALID_ARGUMENT", "PLAXIS method arguments must be an array.")
        target = resolve_path(global_object, path)
        try:
            callable_member = getattr(target, method)
        except Exception:
            raise WorkerError("INVALID_PATH", "PLAXIS method path could not be resolved.")
        if not callable(callable_member):
            raise WorkerError("INVALID_ARGUMENT", "The requested PLAXIS member is not callable.")
        try:
            result = callable_member(*args)
        except Exception:
            raise WorkerError("PLAXIS_ERROR", "The requested PLAXIS method failed.")
        return {"path": path, "method": method, "args": args, "result": describe_value(result)}

    @staticmethod
    def _new_project(server):
        try:
            result = server.new()
        except Exception:
            raise WorkerError("PLAXIS_ERROR", "PLAXIS could not create a new project.")
        return {"action": "new_project", "result": describe_value(result)}

    def _open_project(self, server, params):
        filename = self._required_string(params, "filename")
        try:
            result = server.open(filename)
        except Exception:
            raise WorkerError("PLAXIS_ERROR", "PLAXIS could not open the requested project.")
        return {"action": "open_project", "filename": filename, "result": describe_value(result)}

    @staticmethod
    def _close_project(server):
        try:
            result = server.close()
        except Exception:
            raise WorkerError("PLAXIS_ERROR", "PLAXIS could not close the active project.")
        return {"action": "close_project", "result": describe_value(result)}

    @staticmethod
    def _recover_project(server):
        try:
            result = server.recover()
        except Exception:
            raise WorkerError("PLAXIS_ERROR", "PLAXIS could not recover the active project.")
        return {"action": "recover_project", "result": describe_value(result)}

    def _save_project(self, global_object, params):
        filename = params.get("filename")
        if filename is not None and (not isinstance(filename, str) or not filename):
            raise WorkerError("INVALID_ARGUMENT", "PLAXIS save filename must be a non-empty string when supplied.")
        name = "save" if filename is None else "saveas"
        try:
            method = getattr(global_object, name)
            result = method() if filename is None else method(filename)
        except Exception:
            raise WorkerError("PLAXIS_ERROR", "PLAXIS could not save the active project.")
        payload = {"action": "save_project", "result": describe_value(result)}
        if filename is not None:
            payload["filename"] = filename
        return payload

    @staticmethod
    def _list_phases(global_object):
        try:
            phases = global_object.Phases
            iterator = iter(phases)
        except Exception:
            raise WorkerError("PLAXIS_ERROR", "PLAXIS phases could not be read.")
        items = []
        for index, phase in enumerate(iterator):
            items.append({
                "index": index,
                "name": describe_value(read_member_value(phase, "Name")),
                "identification": describe_value(read_member_value(phase, "Identification")),
                "phase_type": describe_value(read_member_value(phase, "TypeName")),
            })
        return {"count": len(items), "phases": items}

    @staticmethod
    def _list_materials(global_object):
        materials = None
        for candidate in ("Materials", "SoilMat", "PlateMat", "BeamMat", "InterfaceMat"):
            try:
                materials = getattr(global_object, candidate)
                break
            except Exception:
                continue
        if materials is None:
            raise WorkerError("PLAXIS_ERROR", "No PLAXIS material collection was found.")
        try:
            iterator = iter(materials)
        except Exception:
            raise WorkerError("PLAXIS_ERROR", "PLAXIS materials could not be read.")
        items = []
        for index, material in enumerate(iterator):
            items.append({
                "index": index,
                "name": describe_value(read_member_value(material, "Name")),
                "identification": describe_value(read_member_value(material, "Identification")),
                "material_type": describe_value(read_member_value(material, "TypeName")),
            })
        return {"count": len(items), "materials": items}

    @staticmethod
    def _project_info(global_object):
        return {
            "project_title": describe_value(read_member_value(global_object, "ProjectTitle")),
            "project_description": describe_value(read_member_value(global_object, "ProjectDescription")),
            "filename": describe_value(read_member_value(global_object, "Filename")),
            "unit_force": describe_value(read_member_value(global_object, "UnitForce")),
            "unit_length": describe_value(read_member_value(global_object, "UnitLength")),
            "unit_time": describe_value(read_member_value(global_object, "UnitTime")),
        }

    def _list_result_types(self, global_object, params):
        path = self._optional_string(params, "path", "ResultTypes")
        target = resolve_path(global_object, path)
        return {"path": path, "members": safe_dir(target), "summary": describe_value(target)}

    def _get_results(self, global_object, params):
        phase = resolve_phase(global_object, params.get("phase"))
        result_type_path = self._required_string(params, "result_type_path")
        fem_type = self._optional_string(params, "fem_type", "node")
        offset = self._nonnegative_integer(params, "offset", 0)
        limit = self._bounded_integer(params, "limit", 200, 1, 5000)
        result_type = resolve_path(global_object, result_type_path)
        try:
            getresults = getattr(global_object, "getresults")
        except Exception:
            raise WorkerError("WRONG_ROLE", "Connected PLAXIS endpoint does not expose Output results.")
        if not callable(getresults):
            raise WorkerError("WRONG_ROLE", "Connected PLAXIS endpoint does not expose Output results.")
        try:
            results = list(getresults(phase, result_type, fem_type))
        except Exception:
            raise WorkerError("PLAXIS_ERROR", "PLAXIS could not retrieve the requested results.")
        count = len(results)
        page = results[offset : offset + limit]
        serialized = [describe_value(item, max_items=limit) for item in page]
        returned_count = len(serialized)
        has_more = offset + returned_count < count
        return {
            "phase": describe_value(phase),
            "result_type_path": result_type_path,
            "fem_type": fem_type,
            "count": count,
            "offset": offset,
            "limit": limit,
            "returned_count": returned_count,
            "has_more": has_more,
            "next_offset": offset + returned_count if has_more else None,
            "results": serialized,
        }

    def _handshake(self):
        version, path = self._vendor_metadata()
        return {
            "protocol": PROTOCOL_VERSION,
            "role": self.role,
            "python_version": ".".join(str(part) for part in sys.version_info[:3]),
            "python_executable": sys.executable,
            "plxscripting_version": version,
            "plxscripting_path": path,
            "product_version": os.environ.get("PLAXIS_PRODUCT_VERSION"),
            "capabilities": list(TRANSPORT_OPERATIONS[:-1]),
            "operations": list(self._dispatcher.available_operations()),
        }

    @staticmethod
    def _load_new_server():
        try:
            from plxscripting.server import new_server
        except ImportError:
            raise WorkerError(
                "PLAXIS_IMPORT_FAILED",
                "The selected PLAXIS Python environment does not provide plxscripting.server.",
            )
        return new_server

    @staticmethod
    def _close_session(server):
        if server is None:
            return
        # Different PLAXIS releases expose their requests session under
        # different names.  Closing one, when present, is safe and avoids
        # retaining an authenticated HTTP connection after disconnect.
        for name in ("_requests_session", "_session", "session", "_http_session"):
            try:
                candidate = getattr(server, name)
            except Exception:
                continue
            close = getattr(candidate, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass
                break
        try:
            connection = getattr(server, "connection")
            candidate = getattr(connection, "session")
            close = getattr(candidate, "close", None)
            if callable(close):
                close()
        except Exception:
            pass

    @staticmethod
    def _vendor_metadata():
        try:
            import plxscripting
        except ImportError:
            return None, None
        return getattr(plxscripting, "__version__", None), getattr(plxscripting, "__file__", None)

    @staticmethod
    def _required_string(params, name, allow_empty=False):
        value = params.get(name)
        if not isinstance(value, str) or (not allow_empty and not value):
            raise WorkerError("INVALID_ARGUMENT", "Worker parameter '%s' must be a string." % name)
        return value

    @staticmethod
    def _required_port(params):
        value = params.get("port")
        if isinstance(value, bool) or not isinstance(value, int) or value < 1 or value > 65535:
            raise WorkerError("INVALID_ARGUMENT", "Worker parameter 'port' must be a valid TCP port.")
        return value

    @staticmethod
    def _optional_number(params, name):
        value = params.get(name)
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            raise WorkerError("INVALID_ARGUMENT", "Worker parameter '%s' must be a positive number." % name)
        return float(value)

    @staticmethod
    def _optional_string(params, name, default):
        value = params.get(name, default)
        if not isinstance(value, str):
            raise WorkerError("INVALID_ARGUMENT", "Worker parameter '%s' must be a string." % name)
        return value

    @staticmethod
    def _nonnegative_integer(params, name, default):
        value = params.get(name, default)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise WorkerError("INVALID_ARGUMENT", "Worker parameter '%s' must be a non-negative integer." % name)
        return value

    @staticmethod
    def _bounded_integer(params, name, default, minimum, maximum):
        value = params.get(name, default)
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum or value > maximum:
            raise WorkerError(
                "INVALID_ARGUMENT",
                "Worker parameter '%s' must be an integer between %d and %d." % (name, minimum, maximum),
            )
        return value

    def _transition(self, state, last_error=None):
        self._state = state
        self._last_error = last_error
        self._last_transition_at = self._timestamp()

    @staticmethod
    def _timestamp():
        return datetime.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"

    @staticmethod
    def _safe_attestation_value(value):
        if value is None or isinstance(value, (str, bool, int)):
            return value
        if isinstance(value, float) and math.isfinite(value):
            return value
        return None

    @staticmethod
    def _validate_request(request):
        if not isinstance(request, dict):
            raise WorkerError("INVALID_REQUEST", "Worker request must be an object.")
        if request.get("protocol") != PROTOCOL_VERSION:
            raise WorkerError("PROTOCOL_MISMATCH", "Worker protocol version is not supported.")
        if request.get("id") is None:
            raise WorkerError("INVALID_REQUEST", "Worker request requires an id.")
        if not isinstance(request.get("operation"), str) or not request["operation"]:
            raise WorkerError("INVALID_REQUEST", "Worker request requires an operation.")
        if not isinstance(request.get("params"), dict):
            raise WorkerError("INVALID_REQUEST", "Worker request parameters must be an object.")

    def _remember_secrets(self, value, key=""):
        if self._is_secret_name(key) and isinstance(value, str) and value:
            self._secret_values.add(value)
        if isinstance(value, dict):
            for item_key, item_value in value.items():
                self._remember_secrets(item_value, str(item_key))
        elif isinstance(value, (list, tuple)):
            for item in value:
                self._remember_secrets(item, key)

    @staticmethod
    def _is_secret_name(name):
        upper_name = name.upper()
        return any(marker in upper_name for marker in _SECRET_MARKERS)

    def _sanitize(self, message):
        clean = str(message)
        for secret in sorted(self._secret_values, key=len, reverse=True):
            if secret:
                clean = clean.replace(secret, "[REDACTED]")
        return clean

    def _success_response(self, request_id, result):
        return {"protocol": PROTOCOL_VERSION, "id": request_id, "ok": True, "result": result}

    def _error_response(self, request_id, code, message, retryable=False):
        return {
            "protocol": PROTOCOL_VERSION,
            "id": request_id,
            "ok": False,
            "error": {
                "code": code,
                "message": self._sanitize(message),
                "retryable": bool(retryable),
                "state": self._state,
            },
        }

    def _json_value(self, value, depth=0, max_depth=8, max_items=10000):
        if depth > max_depth:
            raise WorkerError("RESULT_TOO_DEEP", "PLAXIS returned a value that is too deeply nested.")
        if value is None or isinstance(value, (str, bool, int)):
            return value
        if isinstance(value, float):
            if value != value or value in (float("inf"), float("-inf")):
                raise WorkerError("UNSERIALIZABLE_RESULT", "PLAXIS returned a non-finite numeric value.")
            return value
        if isinstance(value, dict):
            if len(value) > max_items:
                raise WorkerError("RESULT_TOO_LARGE", "PLAXIS returned too many object members.")
            return {
                str(key): self._json_value(item, depth + 1, max_depth, max_items)
                for key, item in value.items()
            }
        if isinstance(value, (list, tuple, set)):
            if len(value) > max_items:
                raise WorkerError("RESULT_TOO_LARGE", "PLAXIS returned too many values.")
            return [self._json_value(item, depth + 1, max_depth, max_items) for item in value]
        if hasattr(value, "value"):
            try:
                return self._json_value(value.value, depth + 1, max_depth, max_items)
            except Exception:
                pass
        raise WorkerError(
            "UNSERIALIZABLE_RESULT",
            "PLAXIS returned a value that cannot be transferred safely.",
        )
