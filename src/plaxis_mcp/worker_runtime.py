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
    "create_phase", "set_current_phase", "activate", "deactivate", "calculate", "set_phase_property",
    "set_mode", "generate_mesh",
    "create_point", "create_line", "create_polygon", "create_borehole", "create_soillayer",
    "create_material", "assign_material", "create_structural_element",
)
OUTPUT_OPERATIONS = COMMON_OPERATIONS + ("list_result_types", "get_results", "get_single_result")
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
    # A "." immediately after a "]" is a legitimate separator (Plates[-1].Name),
    # not an empty member name -- only a "." with nothing meaningful before it
    # (leading, or after another ".") is the actual error this flag guards.
    just_closed_bracket = False
    while index < len(path):
        character = path[index]
        if character == ".":
            if not current and not just_closed_bracket:
                raise WorkerError("INVALID_PATH", "PLAXIS paths cannot contain empty member names.")
            if current:
                tokens.append(_public_member_name("".join(current), "path member"))
                current = []
            just_closed_bracket = False
            index += 1
            continue
        if character == "[":
            if current:
                tokens.append(_public_member_name("".join(current), "path member"))
                current = []
            end = path.find("]", index)
            raw_index = path[index + 1 : end].strip() if end != -1 else ""
            # PLAXIS's own command syntax uses negative indices for "last item"
            # (e.g. Plates[-1], seen in the activate command reference), so a
            # leading "-" must be accepted alongside plain digits.
            unsigned_index = raw_index[1:] if raw_index.startswith("-") else raw_index
            if not unsigned_index or not unsigned_index.isdigit():
                raise WorkerError("INVALID_PATH", "PLAXIS path indexes must be numeric and closed.")
            tokens.append(int(raw_index))
            index = end + 1
            just_closed_bracket = True
            continue
        current.append(character)
        just_closed_bracket = False
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


def resolve_reference(global_object, value, label):
    """Resolve one JSON-supplied name/path into a real PLAXIS object.

    plxscripting builds every command call as a command-line string
    (server.py:param_to_string): a real PLAXIS object serialises as its bare
    identifier, but a plain Python string is wrapped in quotes and sent as a
    TEXT LITERAL, not a reference -- even when the string names a real
    object. A JSON-supplied name must therefore be resolved into the actual
    object here, before it is ever passed on to a PLAXIS command; passing the
    raw string through (as call_method does, by design, for literal-only
    commands) would silently do the wrong thing for any command expecting an
    object or phase argument.
    """

    if not isinstance(value, str) or not value:
        raise WorkerError("INVALID_ARGUMENT", "%s must be a non-empty PLAXIS object path." % label)
    return resolve_path(global_object, value)


def resolve_reference_list(global_object, values, label):
    """Resolve a JSON array of paths for PLAXIS's "(obj1 obj2)" sequence form.

    A Python list of resolved objects passed as a single command argument is
    itself serialised by plxscripting as a parenthesised sequence, matching
    commands such as "activate (Plate_1 Plate_2) (Phase_1 Phase_2)" -- so the
    caller can pass the returned list straight through as one positional
    argument.
    """

    if not isinstance(values, list) or not values:
        raise WorkerError("INVALID_ARGUMENT", "%s must be a non-empty array of PLAXIS object paths." % label)
    return [resolve_reference(global_object, value, label) for value in values]


def call_plaxis_command(target, command_name, *args):
    """Call a named PLAXIS command on a target with already-resolved arguments.

    ``target`` is usually the global object (for a global command like
    activate or calculate), but the same call shape works for a command
    invoked on a resolved instance object -- setproperties's phase-scoped
    form is called on the target object being modified, not on the global
    object. Unlike call_method (which takes literal JSON args straight from
    an MCP client), every argument here is expected to already be either a
    literal value or a real PLAXIS object obtained via
    resolve_reference[_list] -- this is the shared call path for every
    first-class staged-construction, geometry, structural-element, and
    material operation.
    """

    try:
        command = getattr(target, command_name)
    except Exception:
        raise WorkerError("PLAXIS_ERROR", "PLAXIS does not expose the '%s' command." % command_name)
    if not callable(command):
        raise WorkerError("PLAXIS_ERROR", "PLAXIS '%s' is not a callable command." % command_name)
    try:
        return command(*args)
    except Exception:
        raise WorkerError("PLAXIS_ERROR", "PLAXIS could not execute '%s'." % command_name)


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
    # Truncation is always reported out of band, and only ever wraps the value
    # when something was actually dropped. An in-band marker (a "_truncated"
    # key, or a sentinel appended into the sequence itself) forces every
    # consumer to type-check each element, and a caller that sums or counts a
    # clipped sequence silently gets a wrong answer -- the exact failure the
    # no-silent-truncation rule exists to prevent.
    if isinstance(value, dict):
        items = list(value.items())
        described = {
            str(key): describe_value(item, depth + 1, max_depth, max_items)
            for key, item in items[:max_items]
        }
        if len(items) <= max_items:
            return described
        return {"kind": "mapping", "count": len(items), "truncated": True, "items": described}
    if isinstance(value, (list, tuple, set)):
        values = list(value)
        described = [describe_value(item, depth + 1, max_depth, max_items) for item in values[:max_items]]
        if len(values) <= max_items:
            return described
        return {"kind": "sequence", "count": len(values), "truncated": True, "items": described}
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
            self._drop_session("SERVER_INACTIVE")
            raise WorkerError("CONNECTION_FAILED", "The PLAXIS scripting server is not active.")
        identity_error = self._endpoint_identity_error(result.get("server_full_name"))
        if identity_error:
            self._drop_session("WRONG_ENDPOINT")
            raise WorkerError("WRONG_ENDPOINT", identity_error)
        if self._authentication_failed(self._global_object):
            self._drop_session("AUTHENTICATION_FAILED")
            raise WorkerError(
                "AUTHENTICATION_FAILED",
                "The PLAXIS scripting server rejected the stored password for this endpoint.",
            )
        return result

    @staticmethod
    def _authentication_failed(global_object):
        """Prove the password works, because connecting does not.

        new_server succeeds with any password: server.active and
        server_full_name are served unauthenticated, and only a model request
        is checked. Without this probe an endpoint reported state "connected"
        while every subsequent query failed, which is a false success and hides
        a wrong credential behind unrelated errors.

        A role-appropriate probe is attempted first. If it raises for a reason
        other than authentication -- the member simply not existing on this
        endpoint or project -- the session is still authenticated, so only the
        server's explicit rejection is treated as a failure.

        The server reports a rejected credential two different ways: a wrong
        password gives "the server failed to decrypt the request", and an empty
        one gives "request is missing Code key needed for decryption". Both name
        the decryption step, so that is what is matched.
        """

        for member in ("Project", "Phases"):
            try:
                getattr(global_object, member)
                return False
            except Exception as exc:
                if "decrypt" in str(exc).lower():
                    return True
        return False

    def _drop_session(self, reason):
        self._close_session(self._server)
        self._server = None
        self._global_object = None
        self._transition("disconnected", reason)

    def _endpoint_identity_error(self, server_full_name):
        """Reject a port that is not the PLAXIS server this role expects.

        Any local service can occupy the configured port, and the scripting
        handshake sends the password before the peer is identified. Observed
        live: an endpoint answering as "Qualys-Correlation" was accepted as a
        healthy PLAXIS connection. Failing closed here does not un-send the
        password, so the message tells the operator to treat it as exposed.
        """

        if not isinstance(server_full_name, str) or not server_full_name.strip():
            return ("The service on this port did not identify itself as a PLAXIS scripting "
                    "server. Treat the password for this endpoint as exposed and rotate it.")
        name = server_full_name.lower()
        if "plaxis" not in name:
            return ("The service on this port is not a PLAXIS scripting server. Treat the "
                    "password for this endpoint as exposed and rotate it.")
        # Match only on an explicit contradiction: a server naming the other
        # role is certainly wrong, while a build that omits the role word is
        # not proof of a mismatch.
        opposite = "output" if self.role == "input" else "input"
        if opposite in name and self.role not in name:
            return ("This profile is pinned to the PLAXIS %s endpoint but the server on this "
                    "port is %s." % (self.role, opposite))
        return None

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
            "create_phase": lambda server, global_object, params: self._create_phase(global_object, params),
            "set_current_phase": lambda server, global_object, params: self._set_current_phase(global_object, params),
            "activate": lambda server, global_object, params: self._set_active(global_object, params, True),
            "deactivate": lambda server, global_object, params: self._set_active(global_object, params, False),
            "calculate": lambda server, global_object, params: self._calculate(global_object, params),
            "set_phase_property": lambda server, global_object, params: self._set_phase_property(global_object, params),
            "set_mode": lambda server, global_object, params: self._set_mode(global_object, params),
            "generate_mesh": lambda server, global_object, params: self._generate_mesh(global_object, params),
            "create_point": lambda server, global_object, params: self._create_point(global_object, params),
            "create_line": lambda server, global_object, params: self._create_line(global_object, params),
            "create_polygon": lambda server, global_object, params: self._create_polygon(global_object, params),
            "create_borehole": lambda server, global_object, params: self._create_borehole(global_object, params),
            "create_soillayer": lambda server, global_object, params: self._create_soillayer(global_object, params),
            "create_material": lambda server, global_object, params: self._create_material(global_object, params),
            "assign_material": lambda server, global_object, params: self._assign_material(global_object, params),
            "create_structural_element": lambda server, global_object, params: self._create_structural_element(global_object, params),
            "list_result_types": lambda server, global_object, params: self._list_result_types(global_object, params),
            "get_results": lambda server, global_object, params: self._get_results(global_object, params),
            "get_single_result": lambda server, global_object, params: self._get_single_result(global_object, params),
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
        # A PLAXIS proxy accepts setattr for any name, including one that names
        # an object rather than a property: the assignment then lands on the
        # local proxy, shadows the real object, and reads back as the value the
        # caller sent. That reported a successful mutation while PLAXIS was
        # never touched, so refuse anything that is not a writable property.
        try:
            previous = getattr(target, tokens[-1])
        except Exception:
            raise WorkerError("INVALID_PATH", "PLAXIS property path could not be resolved.")
        # A property proxy exposes .value and is writable; an object proxy does
        # not. Both carry _guid, so the guid alone cannot tell them apart --
        # keying on it rejected legitimate sets such as Project.Title.
        is_property = hasattr(previous, "value")
        is_object = hasattr(previous, "_guid") or hasattr(previous, "get_cmd_line_repr")
        if is_object and not is_property:
            raise WorkerError(
                "INVALID_PATH",
                "This path names a PLAXIS object, not a writable property. "
                "Append the property to set, for example Project.Title.",
            )
        # Snapshot before mutating: a PLAXIS property proxy is a live reference,
        # so describing it after the set reports the new value as the old one.
        previous_value = describe_value(previous)
        try:
            setattr(target, tokens[-1], params.get("value"))
            value = getattr(target, tokens[-1])
        except Exception:
            raise WorkerError("PLAXIS_ERROR", "PLAXIS rejected the requested property update.")
        return {"path": path, "previous": previous_value, "value": describe_value(value)}

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
        # Opening a path that does not exist did not raise: it reported a
        # successful open of a file that was never there. The worker runs on
        # the PLAXIS host, so this is the same filesystem PLAXIS would read.
        if not os.path.isfile(filename):
            raise WorkerError("INVALID_PATH", "No PLAXIS project file exists at the requested path.")
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
        # PLAXIS exposes a single "save" command that takes an optional target
        # path. There is no "saveas": calling it raised, and every save to a new
        # path failed with a generic error.
        try:
            method = getattr(global_object, "save")
            result = method() if filename is None else method(filename)
        except Exception:
            raise WorkerError("PLAXIS_ERROR", "PLAXIS could not save the active project.")
        payload = {"action": "save_project", "result": describe_value(result)}
        if filename is not None:
            payload["filename"] = filename
            # A save that wrote nothing must not be reported as success.
            if not os.path.isfile(filename):
                raise WorkerError(
                    "PLAXIS_ERROR",
                    "PLAXIS reported a save but no project file exists at the requested path.",
                )
        return payload

    # -- Staged construction (WP-D) ------------------------------------------
    #
    # PLAXIS documents "phase" and "insertphase" as two separate commands,
    # each requiring a single reference phase (there is no zero-argument
    # overload for either -- InitialPhase always exists, so a first real
    # phase is always created relative to it). One tool with a position
    # covers both rather than shipping two near-identical operations.

    def _create_phase(self, global_object, params):
        reference_phase = resolve_reference(global_object, params.get("reference_phase"), "reference_phase")
        position = self._choice_string(params, "position", ("after", "before"), "after")
        command_name = "phase" if position == "after" else "insertphase"
        result = self._call_in_mode(global_object, "stages", command_name, reference_phase)
        return {"position": position, "reference_phase": describe_value(reference_phase), "result": describe_value(result)}

    def _set_current_phase(self, global_object, params):
        phase = resolve_reference(global_object, params.get("phase"), "phase")
        result = call_plaxis_command(global_object, "setcurrentphase", phase)
        return {"phase": describe_value(phase), "result": describe_value(result)}

    def _set_active(self, global_object, params, activate):
        # activate/deactivate's sequence-form signature: <{1,...}: Activatable>'
        # <{1,...}: Any>' -- a Python list argument is itself serialised as a
        # parenthesised sequence by plxscripting, so the two resolved lists are
        # passed straight through as the two positional arguments.
        objects = resolve_reference_list(global_object, params.get("paths"), "paths")
        phases = resolve_reference_list(global_object, params.get("phases"), "phases")
        command_name = "activate" if activate else "deactivate"
        result = call_plaxis_command(global_object, command_name, objects, phases)
        return {
            "action": command_name,
            "paths": params.get("paths"),
            "phases": params.get("phases"),
            "result": describe_value(result),
        }

    def _calculate(self, global_object, params):
        # calculate's explicit-phases signature is variadic ("calculate
        # Phase_1 Phase_6"), not a parenthesised sequence like activate's --
        # the resolved phases are star-unpacked into separate positional
        # arguments, not passed as a single list argument.
        phase_paths = params.get("phases")
        if phase_paths is None:
            result = self._call_in_mode(global_object, "stages", "calculate")
        else:
            phases = resolve_reference_list(global_object, phase_paths, "phases")
            result = self._call_in_mode(global_object, "stages", "calculate", *phases)
        return {"phases": phase_paths, "result": describe_value(result)}

    def _set_phase_property(self, global_object, params):
        # setproperties's phase-scoped signature is called ON the target
        # object being modified (matching how setattr already routes through
        # the target for the existing, non-phase-scoped set_property), not on
        # the global object -- the phase is the third argument to that call.
        target = resolve_reference(global_object, params.get("path"), "path")
        property_name = self._required_string(params, "property")
        phase = resolve_reference(global_object, params.get("phase"), "phase")
        value = params.get("value")
        result = call_plaxis_command(target, "setproperties", property_name, phase, value)
        return {
            "path": params.get("path"),
            "property": property_name,
            "phase": describe_value(phase),
            "value": value,
            "result": describe_value(result),
        }

    _MODE_COMMANDS = {
        "soil": "gotosoil",
        "structures": "gotostructures",
        "mesh": "gotomesh",
        "water": "gotowater",
        "stages": "gotostages",
    }
    # gotosoil/gotostructures take no parameters; only these three modes
    # additionally accept the optional force-new-intersection Boolean.
    _MODES_SUPPORTING_FORCE_INTERSECTION = frozenset(("mesh", "water", "stages"))

    def _set_mode(self, global_object, params):
        mode = self._choice_string(params, "mode", self._MODE_COMMANDS.keys(), None)
        force_new_intersection = self._optional_bool(params, "force_new_intersection")
        if force_new_intersection is not None and mode not in self._MODES_SUPPORTING_FORCE_INTERSECTION:
            raise WorkerError(
                "INVALID_ARGUMENT",
                "force_new_intersection is only meaningful for mode in: %s."
                % ", ".join(sorted(self._MODES_SUPPORTING_FORCE_INTERSECTION)),
            )
        command_name = self._MODE_COMMANDS[mode]
        args = () if force_new_intersection is None else (force_new_intersection,)
        result = call_plaxis_command(global_object, command_name, *args)
        return {"mode": mode, "result": describe_value(result)}

    def _call_in_mode(self, global_object, required_mode, command_name, *args):
        # Ensure PLAXIS Input is in the mode where this command actually
        # executes, then run it -- so a caller need not precede every
        # mesh/phase/calculate call with set_mode. Attribute exposure is NOT a
        # reliable readiness signal: "mesh", for one, is attribute-visible in
        # stages mode yet only *executes* in mesh mode (raising
        # "could not execute 'mesh'"). So switch unconditionally; gotoX is an
        # idempotent mode-set, and it shares the geometry precondition every
        # mode-scoped op here already requires.
        call_plaxis_command(global_object, self._MODE_COMMANDS[required_mode])
        return call_plaxis_command(global_object, command_name, *args)

    def _generate_mesh(self, global_object, params):
        relative_element_size = self._optional_number(params, "relative_element_size")
        enhanced_refinement = self._optional_bool(params, "enhanced_refinement")
        if enhanced_refinement is not None and relative_element_size is None:
            raise WorkerError(
                "INVALID_ARGUMENT",
                "enhanced_refinement requires relative_element_size to also be given.",
            )
        if relative_element_size is None:
            args = ()
        elif enhanced_refinement is None:
            args = (relative_element_size,)
        else:
            args = (relative_element_size, enhanced_refinement)
        # "mesh" is exposed only in mesh mode; _call_in_mode enters it
        # automatically. The un-prefixed "mesh" is correct: the command-line's
        # "_mesh" primitive is a different namespace and is NOT reachable via
        # the plxscripting proxy's attribute lookup.
        result = self._call_in_mode(global_object, "mesh", "mesh", *args)
        return {
            "relative_element_size": relative_element_size,
            "enhanced_refinement": enhanced_refinement,
            "result": describe_value(result),
        }

    # -- Geometry creation (WP-E) ---------------------------------------------
    #
    # point/line/polygon share one union parameter shape throughout the
    # command reference: each vertex is either a [x, y] literal coordinate
    # pair or a reference to an existing Point object. One resolver serves
    # all three rather than duplicating the same union three times.

    @staticmethod
    def _resolve_vertex(global_object, vertex, label):
        if isinstance(vertex, str):
            return resolve_reference(global_object, vertex, label)
        if (
            isinstance(vertex, (list, tuple))
            and len(vertex) == 2
            and all(isinstance(n, (int, float)) and not isinstance(n, bool) for n in vertex)
        ):
            return (vertex[0], vertex[1])
        raise WorkerError(
            "INVALID_ARGUMENT", "%s must be a [x, y] coordinate pair or a PLAXIS point path." % label
        )

    def _resolve_vertices(self, global_object, vertices, label, minimum):
        if not isinstance(vertices, list) or len(vertices) < minimum:
            raise WorkerError(
                "INVALID_ARGUMENT", "%s must be an array of at least %d vertices." % (label, minimum)
            )
        return [self._resolve_vertex(global_object, vertex, label) for vertex in vertices]

    def _create_point(self, global_object, params):
        # point's own parameters are always literal coordinates -- there is
        # no Point-reference form, unlike line/polygon.
        coordinates = self._resolve_vertices(global_object, params.get("coordinates"), "coordinates", 1)
        result = call_plaxis_command(global_object, "point", *coordinates)
        return {"coordinates": params.get("coordinates"), "result": describe_value(result)}

    def _create_line(self, global_object, params):
        # Signature 1 (exactly 2 vertices) and signature 2 (3+ vertices) are
        # both variadic point/coordinate lists; PLAXIS dispatches on the
        # argument count itself, so no branching is needed here. The third,
        # angle/relative/vector sub-command signature is not implemented in
        # this pass -- vertices only.
        vertices = self._resolve_vertices(global_object, params.get("vertices"), "vertices", 2)
        result = call_plaxis_command(global_object, "line", *vertices)
        return {"vertices": params.get("vertices"), "result": describe_value(result)}

    def _create_polygon(self, global_object, params):
        # Vertex-list signature only; the Polycurve-based signature
        # (polygon Polycurve_1 Polycurve_2 ...) is not implemented in this
        # pass, since it depends on a polycurve command this plan does not
        # wire.
        vertices = self._resolve_vertices(global_object, params.get("vertices"), "vertices", 3)
        result = call_plaxis_command(global_object, "polygon", *vertices)
        return {"vertices": params.get("vertices"), "result": describe_value(result)}

    def _create_borehole(self, global_object, params):
        x = self._required_number(params, "x")
        result = call_plaxis_command(global_object, "borehole", x)
        return {"x": x, "result": describe_value(result)}

    def _create_soillayer(self, global_object, params):
        height = self._required_number(params, "height")
        result = call_plaxis_command(global_object, "soillayer", height)
        return {"height": height, "result": describe_value(result)}

    # -- Materials (WP-G) ------------------------------------------------------
    #
    # soilmat/platemat/geogridmat/anchormat/embeddedbeammat all share one
    # identical shape in the command reference: a zero-argument form that
    # creates an unusable placeholder, and a free-form <PropValue{1,...}>
    # name/value-pair form. PLAXIS material models are not individually
    # schema'd here (10+ constitutive models, dozens of parameters each); a
    # property dict is the right shape, matching how setproperties itself
    # already works.

    _MATERIAL_COMMANDS = {
        "soil": "soilmat",
        "plate": "platemat",
        "geogrid": "geogridmat",
        "anchor": "anchormat",
        "embedded_beam": "embeddedbeammat",
    }

    def _create_material(self, global_object, params):
        kind = self._choice_string(params, "kind", self._MATERIAL_COMMANDS.keys(), None)
        properties = params.get("properties")
        command_name = self._MATERIAL_COMMANDS[kind]
        if properties is None:
            result = call_plaxis_command(global_object, command_name)
        else:
            if not isinstance(properties, dict) or not properties:
                raise WorkerError(
                    "INVALID_ARGUMENT", "properties must be a non-empty object of property-value pairs."
                )
            args = []
            for name, value in properties.items():
                if not isinstance(name, str) or not name:
                    raise WorkerError("INVALID_ARGUMENT", "Every material property name must be a non-empty string.")
                args.append(name)
                args.append(value)
            result = call_plaxis_command(global_object, command_name, *args)
        return {"kind": kind, "properties": properties, "result": describe_value(result)}

    def _assign_material(self, global_object, params):
        # setmaterial's "one or more features" signature (<{1,...}: Feature'>'
        # Material') already covers a single feature, so the resolved list is
        # always passed as one argument -- no need to special-case count 1
        # against the doc's separate single-feature signature.
        features = resolve_reference_list(global_object, params.get("paths"), "paths")
        material = resolve_reference(global_object, params.get("material"), "material")
        result = call_plaxis_command(global_object, "setmaterial", features, material)
        return {"paths": params.get("paths"), "material": params.get("material"), "result": describe_value(result)}

    # -- Structural elements (WP-F) --------------------------------------------
    #
    # plate/geogrid/embeddedbeam/n2nanchor all share an identical first
    # signature in the command reference: "adds <element> features to one or
    # more existing lines" (<{1,...}: <Line'|Polycurve'>>'), confirmed by
    # reading each command's first signature individually rather than
    # assuming the pattern held after the first match. Only that signature is
    # implemented -- creating a new line inline, or the element-specific
    # extra overloads (e.g. n2nanchor's material/prestress-in-one-call form),
    # are not. Build the element on an existing line (via create_line) first.

    _STRUCTURAL_ELEMENT_COMMANDS = {
        "plate": "plate",
        "geogrid": "geogrid",
        "embedded_beam": "embeddedbeam",
        "n2n_anchor": "n2nanchor",
    }

    def _create_structural_element(self, global_object, params):
        kind = self._choice_string(params, "kind", self._STRUCTURAL_ELEMENT_COMMANDS.keys(), None)
        lines = resolve_reference_list(global_object, params.get("paths"), "paths")
        command_name = self._STRUCTURAL_ELEMENT_COMMANDS[kind]
        result = call_plaxis_command(global_object, command_name, *lines)
        return {"kind": kind, "paths": params.get("paths"), "result": describe_value(result)}

    @staticmethod
    def _list_phases(global_object):
        try:
            phases = global_object.Phases
            iterator = iter(phases)
        except Exception:
            # Observed live: a PLAXIS 2D Input project that has not reached
            # staged construction exposes no Phases collection at all, while
            # the Output endpoint for a calculated project does. Name the
            # likely cause instead of reporting a bare failure.
            raise WorkerError(
                "PLAXIS_ERROR",
                "This PLAXIS project exposes no phases. An Input project only lists phases "
                "once staged construction exists; calculated results are on the Output endpoint.",
            )
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
        """Read project metadata from the Project object that owns it.

        These names live on ``g.Project`` (Title, Comments, Filename, Unit*),
        not on the global object. Reading them off the global returned None for
        every field, so a loaded project was reported as an empty one -- a
        silent wrong answer rather than a visible failure. PLAXIS Output
        exposes no Project object at all, which is now stated instead of being
        rendered as a project with no title.
        """

        project = read_member_value(global_object, "Project")
        if project is None:
            return {
                "available": False,
                "reason": "This PLAXIS endpoint exposes no Project object; "
                          "project metadata is available on the Input endpoint.",
            }
        fields = (
            ("project_title", "Title"),
            ("project_description", "Comments"),
            ("company", "Company"),
            ("filename", "Filename"),
            ("unit_force", "UnitForce"),
            ("unit_length", "UnitLength"),
            ("unit_time", "UnitTime"),
        )
        info = {"available": True}
        for key, member in fields:
            info[key] = describe_value(read_member_value(project, member))
        return info

    def _list_result_types(self, global_object, params):
        path = self._optional_string(params, "path", "ResultTypes")
        target = resolve_path(global_object, path)
        return {"path": path, "members": safe_dir(target), "summary": describe_value(target)}

    # The command reference (commands_getresults.html) documents exactly these
    # four fem_type values; anything else fails server-side with an unhelpful
    # error, so validate it here instead.
    _FEM_TYPES = frozenset(("node", "stresspoint", "element", "materialpoint"))

    def _get_results(self, global_object, params):
        phase = resolve_phase(global_object, params.get("phase"))
        result_type_path = self._required_string(params, "result_type_path")
        fem_type = self._choice_string(params, "fem_type", self._FEM_TYPES, "node")
        # Result smoothing is a real, optional trailing argument on getresults
        # (e.g. "getresults Phase_1 ResultTypes.Soil.Suction \"node\" True") that
        # was previously unreachable through this tool.
        smoothing = self._optional_bool(params, "smoothing")
        offset = self._nonnegative_integer(params, "offset", 0)
        limit = self._bounded_integer(params, "limit", 200, 1, 5000)
        result_type = resolve_path(global_object, result_type_path)
        try:
            getresults = getattr(global_object, "getresults")
        except Exception:
            raise WorkerError("WRONG_ROLE", "Connected PLAXIS endpoint does not expose Output results.")
        if not callable(getresults):
            raise WorkerError("WRONG_ROLE", "Connected PLAXIS endpoint does not expose Output results.")
        call_args = (phase, result_type, fem_type) if smoothing is None else (phase, result_type, fem_type, smoothing)
        try:
            results = list(getresults(*call_args))
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
            "smoothing": smoothing,
            "count": count,
            "offset": offset,
            "limit": limit,
            "returned_count": returned_count,
            "has_more": has_more,
            "next_offset": offset + returned_count if has_more else None,
            "results": serialized,
        }

    def _get_single_result(self, global_object, params):
        # commands_getsingleresult.html has six signatures: by named
        # CurvePoint, by (x, y) coordinate, each with an optional trailing
        # smoothing Boolean -- those four are implemented here. The remaining
        # two (a directional vector for smoothing, and an updated-mesh-
        # geometry Boolean) are not; use get_results for those cases.
        phase = resolve_phase(global_object, params.get("phase"))
        result_type_path = self._required_string(params, "result_type_path")
        result_type = resolve_path(global_object, result_type_path)
        point = params.get("point")
        coords = params.get("coords")
        if (point is None) == (coords is None):
            raise WorkerError("INVALID_ARGUMENT", "Specify exactly one of point or coords.")
        smoothing = self._optional_bool(params, "smoothing")

        if point is not None:
            target = resolve_reference(global_object, point, "point")
            call_args = (phase, result_type, target)
        else:
            if (
                not isinstance(coords, list)
                or len(coords) != 2
                or not all(isinstance(n, (int, float)) and not isinstance(n, bool) for n in coords)
            ):
                raise WorkerError("INVALID_ARGUMENT", "coords must be a [x, y] pair of numbers.")
            call_args = (phase, result_type, coords[0], coords[1])
        if smoothing is not None:
            call_args = call_args + (smoothing,)

        try:
            getsingleresult = getattr(global_object, "getsingleresult")
        except Exception:
            raise WorkerError("WRONG_ROLE", "Connected PLAXIS endpoint does not expose Output results.")
        if not callable(getsingleresult):
            raise WorkerError("WRONG_ROLE", "Connected PLAXIS endpoint does not expose Output results.")
        try:
            result = getsingleresult(*call_args)
        except Exception:
            raise WorkerError("PLAXIS_ERROR", "PLAXIS could not retrieve the requested result.")
        return {
            "phase": describe_value(phase),
            "result_type_path": result_type_path,
            "point": point,
            "coords": coords,
            "smoothing": smoothing,
            "result": describe_value(result),
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
    def _required_number(params, name):
        # Deliberately no sign constraint: a coordinate such as borehole's x
        # can legitimately be negative or zero, unlike _optional_number's
        # existing callers (timeouts, mesh size), which genuinely must be
        # positive.
        value = params.get(name)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise WorkerError("INVALID_ARGUMENT", "Worker parameter '%s' must be a number." % name)
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

    @staticmethod
    def _choice_string(params, name, choices, default):
        value = params.get(name, default)
        if not isinstance(value, str) or value not in choices:
            raise WorkerError(
                "INVALID_ARGUMENT",
                "Worker parameter '%s' must be one of: %s." % (name, ", ".join(sorted(choices))),
            )
        return value

    @staticmethod
    def _optional_bool(params, name, default=None):
        value = params.get(name, default)
        if value is not None and not isinstance(value, bool):
            raise WorkerError("INVALID_ARGUMENT", "Worker parameter '%s' must be a boolean." % name)
        return value

    def _transition(self, state, last_error=None):
        self._state = state
        self._last_error = last_error
        self._last_transition_at = self._timestamp()

    @staticmethod
    def _timestamp():
        # utcnow() is deprecated from 3.12, and datetime.UTC does not exist
        # before 3.11 -- timezone.utc is the one spelling valid across the whole
        # 3.7-3.13 worker range. Dropping tzinfo keeps the trailing "Z" form.
        return (
            datetime.datetime.now(datetime.timezone.utc)
            .replace(tzinfo=None, microsecond=0)
            .isoformat()
            + "Z"
        )

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
