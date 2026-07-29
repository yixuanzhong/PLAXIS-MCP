"""Python 3.7-compatible runtime used inside a PLAXIS Python bundle.

Do not add modern Python syntax here: this module is deliberately exercised
under the Python 3.7.4 that PLAXIS V20 and V21 bundle, as well as newer PLAXIS
bundle interpreters.
"""

from __future__ import print_function

import datetime
import json
import math
import os
import re
import sys
import time


PROTOCOL_VERSION = 1
COMMON_OPERATIONS = (
    "connection_status", "list_members", "inspect", "project_info", "list_phases", "list_materials",
)
INPUT_OPERATIONS = COMMON_OPERATIONS + (
    "list_objects", "model_state",
    "set_property", "call_method", "new_project", "open_project", "close_project", "recover_project", "save_project",
    "create_phase", "set_current_phase", "activate", "deactivate", "calculate", "view_results",
    "set_phase_property",
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

# WP-I. PLAXIS is routinely busy for a few seconds after new_project, and a
# connect that lands in that window used to fail once, non-retryably -- which
# an agent read as permanent and answered around, from its own knowledge,
# about a model it had never opened. Retrying a slow server here is cheaper
# than any client-side recovery, and the policy lives with the worker that
# applies it; the host imports these to size its own request deadline so it
# cannot time out mid-retry.
CONNECT_ATTEMPTS = 3
CONNECT_BACKOFF_SECONDS = (0.5, 1.5)
# Sent by `identify` in place of the endpoint's real password. The scripting
# handshake transmits whatever password it is given before the peer names
# itself, so identifying a port with the real one would disclose it to whatever
# is listening -- which is the failure the identity check exists to catch.
IDENTITY_PROBE_PASSWORD = "plaxis-mcp-identity-probe"
# Signatures of a server that exists but is not answering yet. Matched on text
# as well as exception class name because plxscripting wraps its HTTP client's
# errors and the class reaching us differs across releases.
_RETRYABLE_CONNECTION_MARKERS = (
    "timed out", "timeout", "max retries exceeded", "connection aborted",
    "connection reset", "remote end closed",
)
# Nothing is listening on the port. Retrying that is pure latency, so it fails
# on the first attempt and says so.
_DEAD_PORT_MARKERS = ("refused", "target machine actively refused", "unreachable")


def connect_deadline(connect_timeout, startup_timeout=0.0):
    """The host-side ceiling for one connect, including the worker's retries."""

    return float(startup_timeout) + float(connect_timeout) * CONNECT_ATTEMPTS + sum(
        CONNECT_BACKOFF_SECONDS
    )


def classify_connection_error(exc):
    """Return "retryable", "dead_port", or "unknown" for a connect failure."""

    text = ("%s %s" % (type(exc).__name__, exc)).lower()
    if any(marker in text for marker in _DEAD_PORT_MARKERS):
        return "dead_port"
    if any(marker in text for marker in _RETRYABLE_CONNECTION_MARKERS):
        return "retryable"
    return "unknown"


class WorkerError(Exception):
    """Safe operational failure sent to the host as a JSON error."""

    def __init__(
        self,
        code,
        message,
        retryable=False,
        plaxis_message=None,
        plaxis_message_truncated=False,
        details=None,
    ):
        super(WorkerError, self).__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        # PLAXIS's own validation text, sanitised (WP-A). It is a separate
        # field rather than an addition to message so that no client matching
        # on the existing wording breaks, and so a host can render or log the
        # two independently.
        self.plaxis_message = plaxis_message
        self.plaxis_message_truncated = plaxis_message_truncated
        # Structured, operation-specific evidence (e.g. the per-phase table a
        # failed calculate rebuilds). Always JSON-safe, never proxies.
        self.details = details


_VENDOR_MESSAGE_LIMIT = 300
# A Windows drive path, a UNC share, or a file:// URL. PLAXIS's validation
# messages never need one, while an open/save failure or an import error can
# carry the operator's directory layout -- which is not the caller's business.
#
# Windows directory names contain spaces constantly ("My Projects", "Program
# Files"), so a pattern that stops at the first space is not a redaction: it
# leaves the tail of the path in the message. Interior segments therefore
# allow spaces and are anchored by the separator that follows them, and a
# quoted path is taken whole. Two guards stop that from eating the sentence
# around it: a segment may not contain ":", which is what let one match run on
# through the prose into a second path later in the line, and the last
# unquoted segment stops at whitespace.
#
# The one thing that survives is a trailing word of an unquoted path that ends
# in a directory name with a space and no closing separator ("...\My Docs"
# leaves "Docs"). Prose and directory names are not distinguishable there, and
# a bare word carries no drive, no separators and no ancestor chain -- the
# layout, which is what must not leak, is gone either way.
_PATH_PATTERN = re.compile(
    r"""
    file://\S+
    | (?P<quote>["'])                     # a quoted path is taken whole
      (?:\\\\|[A-Za-z]:[\\/])
      [^"'\r\n]*
      (?P=quote)
    | (?:\\\\|[A-Za-z]:[\\/])             # UNC share or drive letter
      (?:[^\\/\r\n"':]*[\\/])*            # interior segments, spaces allowed
      [^\s\\/\r\n"':]*                    # last segment, up to whitespace
    """,
    re.VERBOSE,
)
_PATH_PLACEHOLDER = "[path]"
_TRACEBACK_PREFIXES = ('File "', "Traceback (most recent call last)")


def sanitise_vendor_message(exc):
    """Reduce a vendor exception to the one line a caller can act on.

    Returns ``(text, truncated)``, or ``None`` when nothing survives. The
    truncation flag rides alongside the text instead of being folded into it:
    an ellipsis inside the string is exactly the silent truncation this repo
    forbids, so the caller puts the flag in its own envelope field.

    Suppressing vendor exceptions wholesale was right for tracebacks and wrong
    for PLAXIS's own validation messages, which are API contract -- they name
    the rejected property, the unknown parameter, the read-only member. What
    is dropped here is everything else: frames, filesystem paths, and the
    plxscripting header line that precedes the real complaint.
    """

    if exc is None:
        return None
    try:
        text = exc if isinstance(exc, str) else str(exc)
    except Exception:
        return None
    if not isinstance(text, str) or not text:
        return None

    lines = []
    skip_next = False
    for raw_line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith(_TRACEBACK_PREFIXES):
            # A traceback frame is two lines: the File "..." location and the
            # source line echoed under it. Dropping only the first leaves the
            # source line looking like a message, and it is the one that gets
            # picked as the complaint.
            skip_next = line.startswith('File "')
            continue
        if skip_next:
            skip_next = False
            continue
        lines.append(line)
    if not lines:
        return None

    # plxscripting raises its errors as a header line plus PLAXIS's actual
    # complaint on the next one (server.py: "Unsuccessful command:\n" +
    # extra info), so a strict first-line rule would deliver only the header.
    # Skip lines that are nothing but a header -- text ending in a colon --
    # while a later line is still available to carry the content.
    index = 0
    while index + 1 < len(lines) and lines[index].endswith(":"):
        index += 1

    message = _PATH_PATTERN.sub(_PATH_PLACEHOLDER, lines[index]).strip()
    if not any(character.isalnum() for character in message.replace(_PATH_PLACEHOLDER, "")):
        return None
    if len(message) > _VENDOR_MESSAGE_LIMIT:
        return (message[:_VENDOR_MESSAGE_LIMIT], True)
    return (message, False)


def vendor_error(code, message, exc=None, retryable=False, details=None):
    """Build a WorkerError that carries PLAXIS's own message when one exists."""

    sanitised = sanitise_vendor_message(exc)
    plaxis_message, truncated = sanitised if sanitised else (None, False)
    return WorkerError(
        code,
        message,
        retryable=retryable,
        plaxis_message=plaxis_message,
        plaxis_message_truncated=truncated,
        details=details,
    )


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
        except Exception as exc:
            raise vendor_error("INVALID_PATH", "PLAXIS object path could not be resolved.", exc)
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
    except Exception as exc:
        raise vendor_error(
            "PLAXIS_ERROR", "PLAXIS does not expose the '%s' command." % command_name, exc
        )
    if not callable(command):
        raise WorkerError("PLAXIS_ERROR", "PLAXIS '%s' is not a callable command." % command_name)
    try:
        return command(*args)
    except Exception as exc:
        raise vendor_error(
            "PLAXIS_ERROR", "PLAXIS could not execute '%s'." % command_name, exc
        )


_PATH_NAME_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")


def is_path_name(name):
    """Whether a PLAXIS object name is shaped like a resolvable path token."""

    return isinstance(name, str) and bool(_PATH_NAME_PATTERN.match(name))


def render_path(tokens):
    """Rebuild a path string from parsed tokens (Plates[0].Name)."""

    rendered = ""
    for token in tokens:
        if isinstance(token, int):
            rendered += "[%d]" % token
        elif rendered:
            rendered += "." + token
        else:
            rendered = token
    return rendered


def object_path(value):
    """The exact string a reference parameter accepts for this object (WP-B).

    Every reference parameter here takes a path string that resolve_path can
    walk, while described objects used to carry no such string -- so an agent
    fed a tool's own output straight back and PLAXIS answered with a parse
    error. PLAXIS's own get_cmd_line_repr is not that string: it returns the
    object's GUID, which only the PLAXIS server can resolve back to a name.
    The Name property is what resolve_path looks up on the global object, so
    that is what is published, and only when it is shaped like a name.
    """

    name = read_member_value(value, "Name")
    return name if is_path_name(name) else None


def read_member(obj, name):
    """Read one member as ``(present, value)``.

    The presence flag exists so a member this PLAXIS generation does not
    expose stays distinguishable from one whose value really is None -- a
    readback that silently reports both as null is a wrong answer, not a
    missing one.
    """

    try:
        value = getattr(obj, name)
    except Exception:
        return (False, None)
    if hasattr(value, "value"):
        try:
            return (True, value.value)
        except Exception:
            return (True, None)
    return (True, value)


def read_member_value(obj, name):
    return read_member(obj, name)[1]


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
        # WP-B: the one field a caller can feed back into any reference
        # parameter. Derived from the Name already read above rather than
        # re-reading it, since every read is a PLAXIS round trip.
        if is_path_name(result.get("name")):
            result["path"] = result["name"]
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
    except Exception as exc:
        raise vendor_error("PLAXIS_ERROR", "The PLAXIS model does not expose phases.", exc)
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
            elif operation == "identify":
                result = self.identify(params)
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
            return self._error_response(
                request_id,
                exc.code,
                exc.message,
                exc.retryable,
                plaxis_message=exc.plaxis_message,
                plaxis_message_truncated=exc.plaxis_message_truncated,
                details=exc.details,
            )
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
        except WorkerError:
            self._transition("disconnected", "CONNECTION_FAILED")
            raise
        server, global_object = self._connect_with_retries(
            factory, host, port, password, timeout, request_timeout
        )

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
                "The PLAXIS scripting server rejected the stored password for this "
                "endpoint. Only the operator at this machine can replace it: running "
                "`plaxis-mcp credentials set --profile %s.toml` in a real terminal "
                "prompts with hidden input and writes to Windows Credential Manager. It "
                "has to be a terminal a person is typing at -- the prompt reads the "
                "console directly, so running it from an agent's shell hangs rather "
                "than failing. Do not ask "
                "anyone to type the password into the chat. The connect tool takes no "
                "password argument, so a password sent that way cannot be used to "
                "connect -- it would only be recorded in the transcript and sent to the "
                "model provider." % self.role,
            )
        return result

    def _connect_with_retries(self, factory, host, port, password, timeout, request_timeout):
        """Connect, retrying a server that is present but not answering yet.

        PLAXIS is busy for seconds after new_project, and the old single
        attempt reported that as a permanent, non-retryable failure. An agent
        that believes PLAXIS is permanently unreachable does not stop -- in the
        smoke test it produced a plausible ground-profile table for a model it
        had never opened. So a slow server is retried here, a dead port fails
        on the first attempt, and either way the message says outright that
        nothing was read.
        """

        attempt = 0
        while True:
            try:
                return factory(
                    address=host,
                    port=port,
                    timeout=timeout,
                    request_timeout=request_timeout,
                    password=password,
                )
            except WorkerError:
                self._transition("disconnected", "CONNECTION_FAILED")
                raise
            except Exception as exc:
                classification = classify_connection_error(exc)
                attempt += 1
                # Only a failure recognised as "the server is there but slow"
                # is retried. Retrying anything else spends the caller's time
                # and, worse, reports the outcome in the language of a
                # timeout -- an unrecognised failure is neither a timeout nor
                # proof that a retry would help, and must not claim to be.
                if classification == "retryable" and attempt < CONNECT_ATTEMPTS:
                    time.sleep(CONNECT_BACKOFF_SECONDS[attempt - 1])
                    continue
                self._transition("disconnected", "CONNECTION_FAILED")
                raise vendor_error(
                    "CONNECTION_FAILED",
                    self._connection_failure_message(host, port, classification, attempt),
                    exc,
                    retryable=classification == "retryable",
                )

    @staticmethod
    def _connection_failure_message(host, port, classification, attempts):
        if classification == "dead_port":
            return (
                "Nothing is listening on the PLAXIS scripting port %s:%s. No model data was "
                "read -- start PLAXIS, enable its scripting server on this port, and connect "
                "again." % (host, port)
            )
        if classification == "retryable":
            return (
                "The PLAXIS scripting server at %s:%s did not answer within the connect timeout "
                "(%d attempts). No model data was read; PLAXIS is often busy for a few seconds "
                "after a new or opened project, so this connect can be retried."
                % (host, port, attempts)
            )
        return (
            "The connection to the PLAXIS scripting server at %s:%s failed for a reason this "
            "build does not recognise; plaxis_message carries what the connection layer "
            "reported. No model data was read, and it is not known whether retrying helps."
            % (host, port)
        )

    def identify(self, params):
        """Name the service on a port without sending this endpoint's password.

        Measured live on PLAXIS 2D V22: one running PLAXIS Input process holds
        several loopback ports -- 10003 answered as "PLAXIS 2D Input/22.2.0.1078"
        and 10141 as "sgcWebSockets 4.0.2" -- so owning the process proves the
        port is PLAXIS's, not that it is the scripting server. Only the peer's
        own name separates them.

        ``server_full_name`` and ``active`` are served unauthenticated, which
        the same measurement confirmed by reading both with a deliberately
        wrong password. So the probe sends a throwaway constant and the real
        password stays unsent until a port has identified itself.
        """

        host = self._required_string(params, "host")
        port = self._required_port(params)
        timeout = self._optional_number(params, "timeout")
        factory = self._new_server_factory or self._load_new_server()
        server = None
        try:
            server, _ = factory(
                address=host,
                port=port,
                timeout=timeout,
                request_timeout=timeout,
                password=IDENTITY_PROBE_PASSWORD,
            )
            return {
                "host": host,
                "port": port,
                "active": self._safe_attestation_value(read_member_value(server, "active")),
                "server_full_name": self._safe_attestation_value(
                    read_member_value(server, "server_full_name")
                ),
            }
        except Exception:
            # A port that cannot be identified is simply not a candidate; the
            # caller is choosing between ports, not diagnosing this one.
            return {"host": host, "port": port, "active": None, "server_full_name": None}
        finally:
            if server is not None:
                self._close_session(server)

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
        except Exception as exc:
            failure_code = "PLAXIS_ERROR"
            raise vendor_error("PLAXIS_ERROR", "The requested PLAXIS operation failed.", exc)
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
            "list_objects": lambda server, global_object, params: self._list_objects(global_object, params),
            "model_state": lambda server, global_object, params: self._model_state(global_object, params),
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
            "view_results": lambda server, global_object, params: self._view_results(global_object, params),
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

    # A property whose value is another PLAXIS object, not a literal. PLAXIS
    # needs a real object reference for these, and plxscripting sends a plain
    # string as a quoted text literal -- so a set_property write can never
    # succeed here no matter how the value is spelled. Each entry names the
    # tool that does work (WP-C).
    _OBJECT_REFERENCE_PROPERTIES = {
        "Material": (
            "`%(path)s` is a material reference, not a writable property. "
            "Use assign_material(paths=[\"%(owner)s\"], material=\"<material path>\") instead; "
            "list_materials gives each material's `path`."
        ),
    }

    @staticmethod
    def _scalar_value(params, name):
        """Reject the shapes PLAXIS cannot receive, and name what to send.

        A dict or list here is not a PLAXIS value: plxscripting serialises a
        command as a command line, where a mapping has no representation at
        all and a sequence becomes a parenthesised group. Observed live, an
        agent fed describe_value's own object dict back in and PLAXIS answered
        with a parse error rather than anything nameable.
        """

        value = params.get(name)
        if value is None or isinstance(value, (str, bool, int, float)):
            return value
        raise WorkerError(
            "INVALID_ARGUMENT",
            "Worker parameter '%s' must be a scalar (string, number, boolean, or null). "
            "Object and array values are not accepted here; pass the object's `path` string "
            "(for example \"SoilMat_1\"), or use assign_material for a material reference." % name,
        )

    _TOPMOST_LAYER_NAME = re.compile(r"^Soillayer_0*1$")

    @classmethod
    def _is_below_the_topmost_layer(cls, tokens):
        """Whether a path provably names a layer other than the topmost one.

        Only the layers below the top derive their Top from the layer above,
        so the guard must be able to prove which layer it is looking at.
        "Provably" is the whole point: an unindexed or unfamiliar spelling
        means unknown, and unknown must let the write through to PLAXIS rather
        than block a mutation that may well be valid.
        """

        for position, token in enumerate(tokens):
            if token == "Soillayers" and position + 1 < len(tokens):
                following = tokens[position + 1]
                if isinstance(following, int):
                    return following > 0
                return False
            if isinstance(token, str) and token.startswith("Soillayer_"):
                return not cls._TOPMOST_LAYER_NAME.match(token)
        return False

    _LAYER_NAME = re.compile(r"^Soillayer_\d+$")
    # A soil layer carries one entry per borehole zone for each of these, so the
    # layer-level member is a PlxObjectPropertyList rather than a value. Read live off
    # list_members on a V22 Soillayer, not from the object-reference doc.
    _PER_ZONE_LAYER_PROPERTIES = ("Top", "Bottom", "Thickness", "top_manual")

    @classmethod
    def _names_a_soil_layer(cls, tokens):
        """Whether `tokens` provably ends at a soil layer object."""

        if not tokens:
            return False
        last = tokens[-1]
        if isinstance(last, str):
            return bool(cls._LAYER_NAME.match(last))
        return len(tokens) >= 2 and tokens[-2] == "Soillayers"

    @classmethod
    def _per_zone_property_message(cls, tokens):
        """Name the zone a layer-level geometry write is missing (WP-D).

        `Soillayer_1.Top` reads back as `[0]` -- one entry per borehole zone --
        and assigning to it raises PLAXIS's "'PlxObjectPropertyList' object has
        no attribute '__set__'", which names the internal type and no way
        forward. Measured live: an agent that hit it spent eleven calls
        permuting Top/Bottom/Thickness/top_manual across two layers, in both
        set_property and call_method spellings, then reset the project.

        Narrow on purpose, like the derived-Top rule below: it fires only where
        the path proves it ends at a soil layer and names no zone of its own.
        """

        if not tokens or tokens[-1] not in cls._PER_ZONE_LAYER_PROPERTIES:
            return None
        owner = tokens[:-1]
        if any(token == "Zones" for token in owner) or not cls._names_a_soil_layer(owner):
            return None
        return (
            "`%(path)s` is a per-zone list, not a single value: a soil layer holds one "
            "%(prop)s per borehole zone. Set it on the zone instead -- "
            "`%(layer)s.Zones[0].%(prop)s` -- and list_members on `%(layer)s.Zones` shows "
            "how many zones there are. Below the topmost layer a zone's Top is derived "
            "from the zone above it, so set `Bottom` or `Thickness` there."
            % {
                "path": render_path(tokens),
                "prop": tokens[-1],
                "layer": render_path(owner),
            }
        )

    @classmethod
    def _derived_property_message(cls, tokens):
        """Name the domain rule behind a property PLAXIS computes (WP-D).

        plxscripting exposes no writability flag -- read-only is a fact the
        PLAXIS server holds -- so this cannot be a general check. It is a
        table of the rules that cost real time live: the smoke test spent ten
        minutes and a killed run guessing at forms of a zone Top write that
        PLAXIS was never going to accept.

        The guard is deliberately narrow. It fires only where the path proves
        the zone belongs to a layer below the top; the topmost layer's Top is
        the one the plan records as writable, and refusing that would trade a
        thrash for a blocked mutation. Everything it cannot prove goes to
        PLAXIS, whose own answer is now legible (WP-A).
        """

        if (
            tokens[-1] == "Top"
            and any(token == "Zones" for token in tokens[:-1])
            and cls._is_below_the_topmost_layer(tokens[:-1])
        ):
            return (
                "`%s` is derived and read-only: below the topmost layer a zone's top boundary "
                "follows the bottom of the zone above it. Set `Bottom` (or `Thickness`) on the "
                "zone above instead. `top_manual` is writable but PLAXIS honours it only for "
                "the topmost zone." % render_path(tokens)
            )
        return None

    def _set_property(self, global_object, params):
        path = self._required_string(params, "path")
        value_to_set = self._scalar_value(params, "value")
        tokens = parse_path(path)
        if not tokens or isinstance(tokens[-1], int):
            raise WorkerError("INVALID_PATH", "Property paths must end with an attribute name.")
        reference_message = self._OBJECT_REFERENCE_PROPERTIES.get(tokens[-1])
        if reference_message is not None:
            raise WorkerError(
                "WRONG_TOOL",
                reference_message % {"path": render_path(tokens), "owner": render_path(tokens[:-1])},
            )
        derived_message = self._derived_property_message(tokens)
        if derived_message is not None:
            raise WorkerError("READ_ONLY_PROPERTY", derived_message)
        per_zone_message = self._per_zone_property_message(tokens)
        if per_zone_message is not None:
            raise WorkerError("INVALID_PATH", per_zone_message)
        target = global_object
        for token in tokens[:-1]:
            try:
                target = target[token] if isinstance(token, int) else getattr(target, token)
            except Exception as exc:
                raise vendor_error("INVALID_PATH", "PLAXIS property path could not be resolved.", exc)
        # A PLAXIS proxy accepts setattr for any name, including one that names
        # an object rather than a property: the assignment then lands on the
        # local proxy, shadows the real object, and reads back as the value the
        # caller sent. That reported a successful mutation while PLAXIS was
        # never touched, so refuse anything that is not a writable property.
        try:
            previous = getattr(target, tokens[-1])
        except Exception as exc:
            raise vendor_error("INVALID_PATH", "PLAXIS property path could not be resolved.", exc)
        # A property proxy exposes .value and is writable; an object proxy does
        # not. Both carry _guid, so the guid alone cannot tell them apart --
        # keying on it rejected legitimate sets such as Project.Title.
        is_property = hasattr(previous, "value")
        is_object = hasattr(previous, "_guid") or hasattr(previous, "get_cmd_line_repr")
        if is_object and not is_property:
            raise WorkerError(
                "INVALID_PATH",
                "`%s` names a PLAXIS object, not a writable property. Append the property to "
                "set (for example Project.Title); list_members on this path shows which "
                "properties it has." % path,
            )
        # Snapshot before mutating: a PLAXIS property proxy is a live reference,
        # so describing it after the set reports the new value as the old one.
        previous_value = describe_value(previous)
        try:
            setattr(target, tokens[-1], value_to_set)
            value = getattr(target, tokens[-1])
        except Exception as exc:
            raise vendor_error(
                "PLAXIS_ERROR", "PLAXIS rejected the requested property update.", exc
            )
        result = {"path": path, "previous": previous_value, "value": describe_value(value)}
        if tokens[-1] == "Identification":
            # Setting Identification renames the object: PLAXIS answers
            # "SoilMat_1 renamed to SZ", and the path used here is dead from
            # this call onwards. Returning the new one is the difference
            # between one more call and a run of failing ones (WP-B).
            renamed = object_path(target)
            if renamed is not None:
                result["object_path"] = renamed
        return result

    def _call_method(self, global_object, params):
        path = self._optional_string(params, "path", "")
        method = self._required_string(params, "method")
        _public_member_name(method, "method")
        args = params.get("args", [])
        if not isinstance(args, list):
            raise WorkerError("INVALID_ARGUMENT", "PLAXIS method arguments must be an array.")
        # The tool contract has always said "literal arguments only"; the
        # schema used to admit nested objects and arrays anyway, and every
        # malformed command in the smoke-test console transcripts came through
        # this hatch. Contract and schema now agree (WP-H).
        for index, argument in enumerate(args):
            if argument is not None and not isinstance(argument, (str, bool, int, float)):
                raise WorkerError(
                    "INVALID_ARGUMENT",
                    "PLAXIS method argument %d must be a scalar (string, number, boolean, or "
                    "null). This tool cannot pass an object or array to PLAXIS; use the "
                    "first-class tool for that command, or pass an object's `path` string to "
                    "a tool that takes references." % index,
                )
        # The same layer-level geometry write, spelled as a method call. Live, an agent
        # that had exhausted set_property moved straight to `Soillayer_1.set("Bottom", -5)`
        # and `Soillayer_1.Top.set(0)`; PLAXIS answers those with "Invalid parameters" and
        # "no attribute 'set'", neither of which names the zone it is missing.
        per_zone_message = self._per_zone_property_message(parse_path(path)) or (
            self._per_zone_property_message(parse_path(path) + [args[0]])
            if method == "set" and args and isinstance(args[0], str)
            else None
        )
        if per_zone_message is not None:
            raise WorkerError("INVALID_PATH", per_zone_message)
        target = resolve_path(global_object, path)
        try:
            callable_member = getattr(target, method)
        except Exception as exc:
            raise vendor_error("INVALID_PATH", "PLAXIS method path could not be resolved.", exc)
        if not callable(callable_member):
            raise WorkerError("INVALID_ARGUMENT", "The requested PLAXIS member is not callable.")
        try:
            result = callable_member(*args)
        except Exception as exc:
            raise vendor_error("PLAXIS_ERROR", "The requested PLAXIS method failed.", exc)
        return {"path": path, "method": method, "args": args, "result": describe_value(result)}

    @staticmethod
    def _new_project(server):
        try:
            result = server.new()
        except Exception as exc:
            raise vendor_error("PLAXIS_ERROR", "PLAXIS could not create a new project.", exc)
        return {"action": "new_project", "result": describe_value(result)}

    @staticmethod
    def _resolve_project_path(filename):
        # Absolute paths pass through unchanged. A relative path resolves
        # against PLAXIS_PROJECT_DIR -- the MCP host's launch directory, which a
        # client sets to its working/session directory -- so a bare filename
        # saves where the user is working instead of in PLAXIS's own cwd. The
        # resolved absolute path is what PLAXIS receives and what the caller is
        # told, so it always learns where the file actually went.
        if os.path.isabs(filename):
            return filename
        base = os.environ.get("PLAXIS_PROJECT_DIR")
        if not base:
            return filename
        return os.path.normpath(os.path.join(base, filename))

    def _open_project(self, server, params):
        filename = self._resolve_project_path(self._required_string(params, "filename"))
        # Opening a path that does not exist did not raise: it reported a
        # successful open of a file that was never there. The worker runs on
        # the PLAXIS host, so this is the same filesystem PLAXIS would read.
        if not os.path.isfile(filename):
            raise WorkerError("INVALID_PATH", "No PLAXIS project file exists at the requested path.")
        try:
            result = server.open(filename)
        except Exception as exc:
            raise vendor_error("PLAXIS_ERROR", "PLAXIS could not open the requested project.", exc)
        return {"action": "open_project", "filename": filename, "result": describe_value(result)}

    @staticmethod
    def _close_project(server):
        try:
            result = server.close()
        except Exception as exc:
            raise vendor_error("PLAXIS_ERROR", "PLAXIS could not close the active project.", exc)
        return {"action": "close_project", "result": describe_value(result)}

    @staticmethod
    def _recover_project(server):
        try:
            result = server.recover()
        except Exception as exc:
            raise vendor_error("PLAXIS_ERROR", "PLAXIS could not recover the active project.", exc)
        return {"action": "recover_project", "result": describe_value(result)}

    def _save_project(self, global_object, params):
        filename = params.get("filename")
        if filename is not None and (not isinstance(filename, str) or not filename):
            raise WorkerError("INVALID_ARGUMENT", "PLAXIS save filename must be a non-empty string when supplied.")
        if filename is not None:
            filename = self._resolve_project_path(filename)
        # PLAXIS exposes a single "save" command that takes an optional target
        # path. There is no "saveas": calling it raised, and every save to a new
        # path failed with a generic error.
        try:
            method = getattr(global_object, "save")
            result = method() if filename is None else method(filename)
        except Exception as exc:
            raise vendor_error("PLAXIS_ERROR", "PLAXIS could not save the active project.", exc)
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

    def _view_results(self, global_object, params):
        """Open a calculated phase in PLAXIS Output.

        Without this there is no route at all from a calculated Input model to
        the Output endpoint: Output reads only what its application already
        has open, the Output role has no open_project by design, and PLAXIS's
        own "view" takes a phase *object*, which call_method cannot pass since
        it accepts scalars only. Found live -- every Output tool answered
        "No active project" against a model that had just been calculated.
        """

        phase = resolve_reference(global_object, params.get("phase"), "phase")
        result = call_plaxis_command(global_object, "view", phase)
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

    # What a calculate readback reports per phase, in PLAXIS's own member
    # names. Read through candidate lists because the members a phase exposes
    # are not identical across PLAXIS generations; anything absent is reported
    # as absent rather than guessed at.
    _PHASE_OUTCOME_MEMBERS = (
        ("identification", ("Identification",)),
        ("number", ("Number",)),
        ("should_calculate", ("ShouldCalculate",)),
        ("status", ("CalculationResult",)),
        ("calculation_type", ("DeformCalcType", "CalculationType")),
        ("pore_pressure_calculation_type", ("PorePresCalcType", "PoreCalcType")),
        ("first_step", ("FirstStep",)),
        ("last_step", ("LastStep",)),
        ("log", ("LogInfo",)),
    )

    # CalculationResult is an integer code. These three were read off a live
    # calculate whose own summary text named which phases did what, so the
    # mapping is observed rather than guessed: PLAXIS reported Phase_4 as
    # failed (2), Phases 5-9 as not calculated because their start phase had
    # not succeeded (0), and the rest as calculated (1). Any other code is
    # left unnamed -- the raw value is always reported beside this.
    _PHASE_STATUS_TEXT = {0: "not_calculated", 1: "calculated", 2: "failed"}

    def _phase_outcomes(self, global_object):
        """Rebuild the per-phase calculation table from the model (WP-F).

        A failed calculate used to deliver "PLAXIS could not execute
        'calculate'" and nothing else: no failing phase, no log, no step
        counts. This is the one blocker the smoke test could not diagnose from
        the MCP side at all, so the table is rebuilt on the failure path as
        well as the success path.
        """

        try:
            phases = list(global_object.Phases)
        except Exception:
            return None
        outcomes = []
        for index, phase in enumerate(phases):
            entry = {"index": index, "name": describe_value(read_member_value(phase, "Name"))}
            path = object_path(phase)
            if path is not None:
                entry["path"] = path
            missing = []
            for key, candidates in self._PHASE_OUTCOME_MEMBERS:
                entry[key] = None
                for member in candidates:
                    present, value = read_member(phase, member)
                    if present:
                        entry[key] = describe_value(value)
                        break
                else:
                    missing.append(candidates[0])
            if missing:
                # Absent is a finding, not a blank: an agent reading "log: null"
                # otherwise cannot tell an empty log from an endpoint that has
                # no LogInfo member at all.
                entry["unavailable_members"] = missing
            entry["status_text"] = self._PHASE_STATUS_TEXT.get(entry.get("status"))
            outcomes.append(entry)
        return outcomes

    def _calculate(self, global_object, params):
        # calculate's explicit-phases signature is variadic ("calculate
        # Phase_1 Phase_6"), not a parenthesised sequence like activate's --
        # the resolved phases are star-unpacked into separate positional
        # arguments, not passed as a single list argument.
        phase_paths = params.get("phases")
        if phase_paths is None:
            arguments = ()
        else:
            arguments = tuple(resolve_reference_list(global_object, phase_paths, "phases"))
        try:
            result = self._call_in_mode(global_object, "stages", "calculate", *arguments)
        except WorkerError as exc:
            details = dict(exc.details or {})
            details["phases"] = self._phase_outcomes(global_object)
            exc.details = details
            raise
        return {
            "phases": phase_paths,
            "result": describe_value(result),
            "phase_outcomes": self._phase_outcomes(global_object),
        }

    def _set_phase_property(self, global_object, params):
        # setproperties's phase-scoped signature is called ON the target
        # object being modified (matching how setattr already routes through
        # the target for the existing, non-phase-scoped set_property), not on
        # the global object -- the phase is the third argument to that call.
        target = resolve_reference(global_object, params.get("path"), "path")
        property_name = self._required_string(params, "property")
        phase = resolve_reference(global_object, params.get("phase"), "phase")
        # Same command line, same limitation as set_property: a mapping has no
        # command-line form and a sequence becomes a parenthesised group.
        value = self._scalar_value(params, "value")
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

    # The property names PLAXIS 2D V22 exposes on each material type, listed as
    # identifiers only: this is API surface, not the vendor's descriptive text,
    # which is never copied into this repo (see the compliance note in the
    # error-fidelity plan). Validating against it turns "Unknown property: EA"
    # -- something only PLAXIS could tell you, after the call -- into a
    # rejection that names the valid spellings first (WP-E). The list is V22's;
    # another PLAXIS generation may name things differently, which is why the
    # rejection message names the way through rather than being a dead end.
    _MATERIAL_PROPERTIES = {
        "soil": (
            "Name", "Comments", "UserFeatures", "Identification", "Colour", "SoilModel", "DrainageType",
            "gammaUnsat", "gammaSat", "eInit", "nInit", "RayleighDampingInputMethod", "RayleighAlpha",
            "RayleighBeta", "TargetDamping1", "TargetDamping2", "TargetFrequency1", "TargetFrequency2",
            "ERef", "nu", "GRef", "EOed", "EInc", "verticalRef", "Vs", "Vp", "cRef", "sURef", "phi",
            "phiU", "psi", "cInc", "sUInc", "TensionCutOff", "TensileStrength", "E50ref", "EOedRef",
            "EURRef", "nuUR", "UseAlternatives", "Cc", "Cs", "PowerM", "pRef", "DilatancyCutOff",
            "eMin", "eMax", "UseDefault", "K0NC", "RF", "G0ref", "gamma07", "lambdaModified",
            "kappaModified", "MCsl", "muModified", "ET", "nuNT", "EN", "nuTS", "GNT", "NumberOfPlanes",
            "c1", "phi1", "psi1", "alpha11", "TensionCutOff1", "TensileStrength1", "c2", "phi2", "psi2",
            "alpha12", "TensionCutOff2", "TensileStrength2", "lambda", "kappa", "sUARef", "sUAInc",
            "sUCTxsUARatio", "sUPsUARatio", "sUDSSsUARatio", "gammaFC", "gammaFE", "gammaFDSS",
            "tau0sUARatio", "Erm", "AbsSigmaCI", "HoekBrownParameters", "mb", "s", "a", "mi", "GSI",
            "D", "SigmaT", "SigmaC", "psiMax", "sigmaPsi", "alphaStar", "v0Dot", "tau", "E28", "fC28",
            "fC0N", "fCFN", "fCUN", "GC28", "phiMax", "ConcretePsi", "gammatFC", "fT28", "fTUN", "GT28",
            "gammaFT", "epsCPP", "aDuct", "TimeDependentMechanicalProperties", "tHyd", "E1E28Ratio",
            "StrengthFunction", "fC1fC28Ratio", "epsPCP1H", "epsPCP8H", "epsPCP24H", "IncludeShrinkage",
            "epsInfiniteShr", "tShr50", "IncludeCreep", "phiCr", "tCr50", "KBeModified", "KGeModified",
            "KGpModified", "me", "ne", "UBCSNP", "c", "phiCv", "phiP", "sigT", "N160",
            "SetToDefaultValues", "FDens", "FEPost", "UBCRF", "nuU", "GMaxtauCRatio", "tauCRef",
            "tauCInc", "tauEtauCRatio", "tauDSStauCRatio", "tau0tauCRatio",
            "ExcessPorePressureDetermination", "UndrainedNuDefinitionMethod", "alphaBiot",
            "nuUEquivalent", "SkemptonB", "Kwn", "GroundwaterClassificationType",
            "GroundwaterSoilClassStandard", "SWCCFittingMethod", "PermeabilityFittingMethod",
            "SubsoilTopsoil", "GroundwaterSoilClassUSDA", "GroundwaterSubSoilClassStaring",
            "GroundwaterTopSoilClassStaring", "ClayFraction", "SiltFraction", "SandFraction",
            "SaturationResidual", "SaturationSaturated", "GenuchtenGn", "GenuchtenGa", "GenuchtenGl",
            "GwUseDefaults", "GwDefaultsMethod", "PermeabilityX", "PermeabilityY",
            "VoidRatioDependency", "Ck", "NInitGroundwater", "GwPsiUnsat", "ThCs", "ThLambdaS", "rhoS",
            "ExpansionType", "ThAlphaSV", "ThAlphaSX", "ThAlphaSY", "ThAlphaSZ", "PhaseChange",
            "UnfrozenWaterSaturationMethod", "UnfrozenWaterSaturationTable", "Dv", "fTv",
            "InterfaceStiffnessMethod", "knInter", "ksInter", "InterfaceStrengthMethod", "Rinter",
            "RinterResidual", "GapClosure", "DeltaInter", "CrossPermeability", "DrainageConductivity",
            "HydraulicResistance", "Rthermal", "K0_method", "K0x", "K0z", "K0xIsK0z", "OCR", "POP",
            "EoedInter", "UDPowerInter", "UDPRefInter", "CInter", "PhiInter", "PsiInter", "DllFile",
            # Live-verified V22 spellings the object-reference page does not use.
            # Observed on a real SoilMat (2026-07-24) after the doc-derived list
            # rejected InterfaceStrengthDetermination, which PLAXIS itself accepts:
            # the doc says InterfaceStrengthMethod, K0_method, K0x/K0z,
            # PermeabilityX/Y, Ck, ExpansionType. Both spellings are kept -- this
            # list exists to catch invented names, not to arbitrate spelling.
            "InterfaceStiffnessDetermination", "InterfaceStrengthDetermination", "K0Determination",
            "K0Primary", "K0Secondary", "PermHorizontalPrimary", "PermVertical",
            "ThermalExpansionType", "cK",
            "ModelInDll", "User1", "User2", "User3", "User4", "User5", "User6", "User7", "User8",
            "User9", "User10", "User11", "User12", "User13", "User14", "User15", "User16", "User17",
            "User18", "User19", "User20", "User21", "User22", "User23", "User24", "User25", "User26",
            "User27", "User28", "User29", "User30", "User31", "User32", "User33", "User34", "User35",
            "User36", "User37", "User38", "User39", "User40", "User41", "User42", "User43", "User44",
            "User45", "User46", "User47", "User48", "User49", "User50",
        ),
        "plate": (
            "Name", "Comments", "UserFeatures", "Identification", "Colour", "MaterialType", "w",
            "DampingMethod", "RayleighAlpha", "RayleighBeta", "TargetDamping1", "TargetDamping2",
            "TargetFrequency1", "TargetFrequency2", "PreventPunching", "IsIsotropic", "EA1", "EA2",
            "E1", "E2", "EI", "StructNu", "d", "Mp", "Np1", "Np2", "MkappaDiagram",
            "StructuralSpecificHeatCapacity", "ThermalConductivity", "Density", "ThermalExpansion",
            "AEffT",
            # Live-verified: PLAXIS exposes "Isotropic"; the doc says "IsIsotropic".
            "Isotropic",
        ),
        "geogrid": (
            "Name", "Comments", "UserFeatures", "Identification", "Colour", "MaterialType",
            "IsIsotropic", "Isotropic", "EA1", "EA2", "Np1", "Np2", "N1eps1", "N2eps2", "EA1Short", "EA2Short",
            "EA1Long", "EA2Long", "RetardationTime", "StructuralSpecificHeatCapacity",
            "ThermalConductivity", "Density", "ThermalExpansion", "AEffT",
        ),
        "anchor": (
            "Name", "Comments", "UserFeatures", "Identification", "Colour", "MaterialType", "LSpacing",
            "EA", "FMaxTens", "FMaxComp", "FResTens", "FResComp", "StructuralSpecificHeatCapacity",
            "ThermalConductivity", "Density", "ThermalExpansion", "AEffT",
        ),
        "embedded_beam": (
            "Name", "Comments", "UserFeatures", "Identification", "Colour", "MaterialType", "Gamma",
            "DampingMethod", "RayleighAlpha", "RayleighBeta", "TargetDamping1", "TargetDamping2",
            "TargetFrequency1", "TargetFrequency2", "LSpacing", "CrossSectionType",
            "PredefinedCrossSectionType", "Diameter", "Thickness", "Width", "A", "I", "E", "Mp", "Np",
            "AxialSkinResistance", "TSkinStartMax", "TSkinEndMax", "MultiLinearAxialSkinResistance",
            "TMax", "LateralResistance", "TLatStartMax", "TLatEndMax", "MultiLinearLateralResistance",
            "Fmax", "DefaultValues", "AxialStiffnessFactor", "LateralStiffnessFactor",
            "BaseStiffnessFactor",
        ),
    }
    # How many valid names one rejection lists. The total is always stated with
    # it, so a caller can tell a complete list from a clipped one.
    _MATERIAL_PROPERTY_SAMPLE = 20

    _MATERIAL_TYPE_NAMES = {
        "soil": "SoilMat",
        "plate": "PlateMat",
        "geogrid": "GeogridMat",
        "anchor": "AnchorMat",
        "embedded_beam": "EmbeddedBeamMat",
    }

    def _live_material_properties(self, global_object, kind):
        """What a material of this kind in THIS model actually exposes.

        The catalog is a fast accept-list, not an oracle, and two live facts
        say it cannot be one. PLAXIS's own spellings differ from its object
        reference in places -- it accepts InterfaceStrengthDetermination where
        the doc says InterfaceStrengthMethod -- and a soil material exposes a
        different member set per constitutive model, because plxscripting
        rebuilds a material's members as its model changes. So a name the
        catalog does not know is checked against the model before it is
        refused. This path only ever accepts more, never fewer.
        """

        type_name = self._MATERIAL_TYPE_NAMES.get(kind)
        if type_name is None:
            return None
        try:
            materials = list(global_object.Materials)
        except Exception:
            return None
        for material in materials:
            if read_member_value(material, "TypeName") == type_name:
                members = safe_dir(material)
                if members:
                    return set(members)
        return None

    def _validate_material_properties(self, global_object, kind, names):
        known = self._MATERIAL_PROPERTIES.get(kind)
        if not known:
            return
        unknown = [name for name in names if name not in known]
        if not unknown:
            return
        live = self._live_material_properties(global_object, kind)
        for name in unknown:
            if live is not None and name in live:
                continue
            self._reject_material_property_name(kind, name, known, live)

    def _reject_material_property_name(self, kind, name, known, live):
        accepted = set(known) | (live or set())
        for candidate in sorted(accepted):
            if candidate.lower() == name.lower():
                raise WorkerError(
                    "INVALID_ARGUMENT",
                    "PLAXIS property names are case-sensitive: use '%s', not '%s', for material "
                    "kind '%s'." % (candidate, name, kind),
                )
        checked = (
            " It is not on an existing %s in this model either."
            % self._MATERIAL_TYPE_NAMES.get(kind, kind)
            if live is not None
            else ""
        )
        raise WorkerError(
            "INVALID_ARGUMENT",
            "Unknown property '%s' for material kind '%s'.%s PLAXIS 2D V22 names %d properties "
            "for this kind; the first %d are: %s. If '%s' is valid in your PLAXIS version, create "
            "the material without it and set it afterwards with set_property on the created "
            "material."
            % (
                name,
                kind,
                checked,
                len(known),
                min(self._MATERIAL_PROPERTY_SAMPLE, len(known)),
                ", ".join(known[: self._MATERIAL_PROPERTY_SAMPLE]),
                name,
            ),
        )

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
            for name in properties:
                if not isinstance(name, str) or not name:
                    raise WorkerError("INVALID_ARGUMENT", "Every material property name must be a non-empty string.")
            self._validate_material_properties(global_object, kind, properties)
            for name, value in properties.items():
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
        except Exception as exc:
            # Observed live: a PLAXIS 2D Input project that has not reached
            # staged construction exposes no Phases collection at all, while
            # the Output endpoint for a calculated project does. Name the
            # likely cause instead of reporting a bare failure.
            raise vendor_error(
                "PLAXIS_ERROR",
                "This PLAXIS project exposes no phases. An Input project only lists phases "
                "once staged construction exists; calculated results are on the Output endpoint.",
                exc,
            )
        items = []
        for index, phase in enumerate(iterator):
            item = {
                "index": index,
                "name": describe_value(read_member_value(phase, "Name")),
                "identification": describe_value(read_member_value(phase, "Identification")),
                "phase_type": describe_value(read_member_value(phase, "TypeName")),
            }
            # Every object in a result carries the string that can be passed
            # back (WP-B) -- these two listings predate that rule and were the
            # ones an agent reads first.
            path = object_path(phase)
            if path is not None:
                item["path"] = path
            items.append(item)
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
        except Exception as exc:
            raise vendor_error("PLAXIS_ERROR", "PLAXIS materials could not be read.", exc)
        items = []
        for index, material in enumerate(iterator):
            item = {
                "index": index,
                "name": describe_value(read_member_value(material, "Name")),
                "identification": describe_value(read_member_value(material, "Identification")),
                "material_type": describe_value(read_member_value(material, "TypeName")),
            }
            path = object_path(material)
            if path is not None:
                item["path"] = path
            items.append(item)
        return {"count": len(items), "materials": items}

    # -- Listings and preflight (WP-G, WP-H) -----------------------------------
    #
    # There were listings for phases and materials and nothing else, so an
    # agent looking for "the soil cluster at x=0-15" fell back to call_method
    # dump/__dump -- 58 such calls in one smoke-test run, and the source of
    # nearly every malformed command in the console transcripts.

    _OBJECT_COLLECTIONS = {
        "point": ("Points",),
        "line": ("Lines",),
        "polygon": ("Polygons",),
        "soil": ("Soils",),
        "soillayer": ("Soillayers",),
        "borehole": ("Boreholes",),
        "plate": ("Plates",),
        "geogrid": ("Geogrids",),
        # A 2D model keeps its two anchor kinds in separate collections, and
        # an embedded beam is listed per row; each item carries the collection
        # it came from, so a merged listing stays unambiguous.
        "anchor": ("NodeToNodeAnchors", "FixedEndAnchors"),
        "embedded_beam": ("EmbeddedBeamRows", "EmbeddedBeams"),
    }
    # Enough geometry to pick the object the caller means without a dump
    # sweep -- never the whole member list, which is what made dump useless.
    #
    # Which of these exist depends on the mode PLAXIS is in, and that is why
    # both spellings are probed. Verified live: in staged/mesh mode every
    # collection yields CutObjects, which carry BoundingBox, Area and Length
    # but no x/y; in structures and soil mode the base objects carry x/y. The
    # ones an object does not have are reported as unavailable, never guessed.
    _OBJECT_SUMMARY_MEMBERS = {
        "point": ("x", "y", "BoundingBox"),
        "line": ("Length", "BoundingBox"),
        "polygon": ("x", "y", "Area", "BoundingBox"),
        "borehole": ("x", "Head"),
        "soil": ("Area", "BoundingBox"),
        "plate": ("Length", "BoundingBox"),
        "geogrid": ("Length", "BoundingBox"),
        "anchor": ("Length", "BoundingBox"),
        "embedded_beam": ("Length", "BoundingBox"),
    }
    _OBJECT_REFERENCE_MEMBERS = {
        "line": ("First", "Second"),
        "polygon": ("Material",),
        "soil": ("Parent", "Material"),
        "soillayer": ("Soil",),
        "plate": ("Parent", "Material"),
        "geogrid": ("Parent", "Material"),
        "anchor": ("Parent", "Material"),
        "embedded_beam": ("Parent", "Material"),
    }
    # A feature (Soil, Plate, ...) carries no coordinates of its own -- its
    # geometry lives on the Parent it was built on. Without these, answering
    # "which soil cluster covers x=0-15, y=6-8" needed a second lookup per
    # candidate, which is the dump sweep WP-H exists to remove, so the parent's
    # own coordinates are read straight into the feature's entry.
    _REFERENCE_GEOMETRY_MEMBERS = {
        "Parent": ("x", "y", "Length", "Area", "BoundingBox"),
        "First": ("x", "y"),
        "Second": ("x", "y"),
    }
    # PLAXIS renders a bounding box as one formatted string, verified live:
    # "min: (0.00; 6.00; 0.00) max: (12.00; 8.00; 0.00)". That is the answer to
    # "which cluster covers x=0-15, y=6-8", but only after parsing, so the
    # numbers are published beside the original -- never instead of it.
    _BOUNDING_BOX_PATTERN = re.compile(
        r"min:\s*\(([^)]*)\)\s*max:\s*\(([^)]*)\)", re.IGNORECASE
    )
    _BOUNDING_BOX_AXES = ("x", "y", "z")

    @classmethod
    def _parse_bounding_box(cls, described):
        """Turn PLAXIS's bounding-box string into per-axis [min, max] numbers."""

        if not isinstance(described, str):
            return None
        match = cls._BOUNDING_BOX_PATTERN.search(described)
        if match is None:
            return None
        bounds = {}
        for axis, low, high in zip(
            cls._BOUNDING_BOX_AXES,
            match.group(1).split(";"),
            match.group(2).split(";"),
        ):
            try:
                bounds[axis] = [float(low.strip()), float(high.strip())]
            except (TypeError, ValueError):
                return None
        return bounds or None

    @staticmethod
    def _coordinate_range(described):
        """[min, max] for a coordinate PLAXIS returns as a sequence.

        A polygon reports its vertex coordinates, so the bounds a caller
        actually asks in are one step away from the raw value. Derived only
        from an all-numeric sequence, and named as a range so nothing mistakes
        it for a value PLAXIS itself reported.
        """

        if not isinstance(described, (list, tuple)) or not described:
            return None
        numbers = [
            item for item in described
            if isinstance(item, (int, float)) and not isinstance(item, bool)
        ]
        if len(numbers) != len(described):
            return None
        return [min(numbers), max(numbers)]

    @classmethod
    def _summarise_members(cls, item, members):
        """Read a short member list, reporting absence rather than hiding it."""

        summary, missing = {}, []
        for member in members:
            present, value = read_member(item, member)
            if not present:
                missing.append(member)
                continue
            described = describe_value(value)
            summary[member] = described
            span = cls._coordinate_range(described)
            if span is not None:
                summary[member + "_range"] = span
            bounds = cls._parse_bounding_box(described)
            if bounds is not None:
                summary["bounds"] = bounds
        return summary, missing

    @classmethod
    def _reference_summary(cls, value, geometry_members=()):
        """Describe a referenced object as the path that can be passed back."""

        if value is None or isinstance(value, (str, bool, int, float)):
            return describe_value(value)
        summary = {}
        path = object_path(value)
        if path is not None:
            summary["path"] = path
        identification = read_member_value(value, "Identification")
        if isinstance(identification, (str, bool, int, float)):
            summary["identification"] = identification
        geometry, _ = cls._summarise_members(value, geometry_members)
        if geometry:
            summary["geometry"] = geometry
        return summary or None

    def _describe_listed_object(self, kind, collection, index, item):
        entry = {
            "index": index,
            "collection": collection,
            "name": describe_value(read_member_value(item, "Name")),
            "identification": describe_value(read_member_value(item, "Identification")),
            "type_name": describe_value(read_member_value(item, "TypeName")),
        }
        path = object_path(item)
        if path is not None:
            entry["path"] = path
        geometry, missing = self._summarise_members(
            item, self._OBJECT_SUMMARY_MEMBERS.get(kind, ())
        )
        if geometry:
            entry["geometry"] = geometry
        references = {}
        for member in self._OBJECT_REFERENCE_MEMBERS.get(kind, ()):
            present, value = read_member(item, member)
            if present:
                references[member] = self._reference_summary(
                    value, self._REFERENCE_GEOMETRY_MEMBERS.get(member, ())
                )
            else:
                missing.append(member)
        if references:
            entry["references"] = references
        if missing:
            # Stated, not silent: a caller must be able to tell a member this
            # object does not have from one that was never looked at.
            entry["unavailable_members"] = missing
        return entry

    def _list_objects(self, global_object, params):
        kind = self._choice_string(params, "kind", self._OBJECT_COLLECTIONS.keys(), None)
        offset = self._nonnegative_integer(params, "offset", 0)
        limit = self._bounded_integer(params, "limit", 200, 1, 2000)
        found, missing = [], []
        for collection in self._OBJECT_COLLECTIONS[kind]:
            try:
                items = list(getattr(global_object, collection))
            except Exception:
                missing.append(collection)
                continue
            found.append((collection, items))
        if not found:
            raise WorkerError(
                "PLAXIS_ERROR",
                "This PLAXIS endpoint exposes no %s collection (%s). A collection appears only "
                "once the model has an object of that kind, and only in a mode that shows it."
                % (kind, ", ".join(missing)),
            )
        flattened = [
            (collection, index, item)
            for collection, items in found
            for index, item in enumerate(items)
        ]
        page = flattened[offset : offset + limit]
        objects = [
            self._describe_listed_object(kind, collection, index, item)
            for collection, index, item in page
        ]
        returned_count = len(objects)
        has_more = offset + returned_count < len(flattened)
        result = {
            "kind": kind,
            "collections": [collection for collection, _ in found],
            "count": len(flattened),
            "offset": offset,
            "limit": limit,
            "returned_count": returned_count,
            "has_more": has_more,
            "next_offset": offset + returned_count if has_more else None,
            "objects": objects,
        }
        if missing:
            result["unavailable_collections"] = missing
        return result

    # Feature collections whose members must carry a material before PLAXIS
    # will calculate. Kept separate from _OBJECT_COLLECTIONS: this is the
    # calculable-model question, not the "show me what exists" one.
    _MATERIAL_BEARING_COLLECTIONS = (
        "Soils", "Plates", "Geogrids", "NodeToNodeAnchors", "FixedEndAnchors", "EmbeddedBeamRows",
    )
    # A preflight must stay cheap; every member read is a PLAXIS round trip.
    # Hitting this cap is reported, never absorbed.
    _MODEL_STATE_SCAN_LIMIT = 500
    # Model.MeshStatus.Status is the only mesh-staleness candidate PLAXIS
    # exposes, and on 2D V22 it is not one. Verified live on the smoke-test
    # fixture (2026-07-24): Status read 1 when freshly opened, still 1 after a
    # meshed geometry point was moved from y=8 to y=-3.5, and still 1 after a
    # soil-layer Top change -- with NumberOfElements frozen at its old 298
    # throughout. So an element count proves a mesh was generated once, never
    # that it matches the geometry now, and no arithmetic on these two numbers
    # can recover the difference.
    #
    # The markers below therefore only ever run against a *textual* status, in
    # case another PLAXIS generation reports one. V22's integer lands in
    # "not_available", which is stated as a standing caveat rather than folded
    # into blocking or unknown: a limit of the API is not a finding about the
    # model, and pretending either way is what produces a false clean bill.
    _MESH_STALE_MARKERS = (
        "regenerat", "outdated", "out of date", "not up to date", "stale", "invalid",
        "no mesh", "none", "absent", "missing", "failed", "error",
    )
    _MESH_READY_MARKERS = ("up to date", "ok", "generated", "valid", "success", "done")
    _MESH_STALENESS_UNAVAILABLE = (
        "PLAXIS reports its mesh status as a code (%r) with no documented meaning, and it was "
        "observed not to change after a geometry edit -- so whether the mesh still matches the "
        "geometry cannot be read here. Regenerate the mesh if the geometry changed since it was "
        "built."
    )

    @classmethod
    def _mesh_staleness(cls, status):
        """Classify a reported mesh status: ready, stale, unrecognised, or absent."""

        if status is None:
            return "not_available"
        if not isinstance(status, str):
            # An enum code. V22's does not move with the geometry (see above).
            return "not_available"
        if not status.strip():
            return "not_available"
        text = status.strip().lower()
        # Stale first: "not up to date" contains "up to date".
        for marker in cls._MESH_STALE_MARKERS:
            if marker in text:
                return "stale"
        for marker in cls._MESH_READY_MARKERS:
            if marker in text:
                return "ready"
        return "unrecognised"

    def _mesh_state(self, global_object):
        model = read_member_value(global_object, "Model")
        if model is None:
            return {
                "available": False,
                "reason": "This PLAXIS endpoint exposes no Model object, which is where mesh "
                          "status lives.",
            }
        present, status = read_member(model, "MeshStatus")
        if not present:
            return {
                "available": False,
                "reason": "This PLAXIS Model object exposes no MeshStatus member.",
            }
        state = {"available": True}
        missing = []
        for key, member in (
            ("status", "Status"),
            ("element_count", "NumberOfElements"),
            ("node_count", "NumberOfNodes"),
        ):
            found, value = read_member(status, member)
            if found:
                state[key] = describe_value(value)
            else:
                missing.append(member)
                state[key] = None
        feedback_present, feedback = read_member(model, "MeshFeedback")
        if feedback_present:
            state["feedback"] = describe_value(feedback)
        else:
            missing.append("MeshFeedback")
        state["staleness"] = self._mesh_staleness(state.get("status"))
        if missing:
            state["unavailable_members"] = missing
        return state

    # PLAXIS's own token for a material whose identification was never set --
    # what a zero-argument create_material leaves behind. The closed calculate
    # blocker was exactly this: `Invalid material "<NoName> [PlateMat_2]"`, a
    # placeholder assigned to a plate. PLAXIS flags such a material by this
    # identification, so keying on it matches PLAXIS's own semantics.
    _PLACEHOLDER_IDENTIFICATION = "<NoName>"

    @staticmethod
    def _feature_entry(collection, item):
        entry = {"collection": collection, "name": describe_value(read_member_value(item, "Name"))}
        path = object_path(item)
        if path is not None:
            entry["path"] = path
        return entry

    def _material_readiness(self, global_object):
        """Scan material-bearing features for what would stop a calculate.

        Two distinct findings, because a caller acts differently on each: a
        feature with no material at all, and a feature whose material was never
        configured (identification still the placeholder token). The second is
        the failure that killed the earlier calculate, and it is invisible to
        an unassigned-materials check because the material is present.
        """

        unassigned, placeholder, scanned, truncated = [], [], 0, False
        for collection in self._MATERIAL_BEARING_COLLECTIONS:
            try:
                items = list(getattr(global_object, collection))
            except Exception:
                continue
            for item in items:
                if scanned >= self._MODEL_STATE_SCAN_LIMIT:
                    truncated = True
                    break
                scanned += 1
                present, material = read_member(item, "Material")
                if not present or material is None:
                    unassigned.append(self._feature_entry(collection, item))
                    continue
                if read_member_value(material, "Identification") == self._PLACEHOLDER_IDENTIFICATION:
                    entry = self._feature_entry(collection, item)
                    material_path = object_path(material)
                    if material_path is not None:
                        entry["material"] = material_path
                    placeholder.append(entry)
            if truncated:
                break
        result = {
            "scanned": scanned,
            "count": len(unassigned),
            "features": unassigned,
            "placeholder_count": len(placeholder),
            "placeholder_features": placeholder,
        }
        if truncated:
            result["scan_truncated"] = True
            result["scan_limit"] = self._MODEL_STATE_SCAN_LIMIT
        return result

    def _model_state(self, global_object, params):
        """Answer "is this model calculable?" without changing anything (WP-G).

        Every section reports what it could actually read. A check that could
        not be evaluated is named in "unknown" instead of being folded into
        "blocking", because "nothing is blocking" and "nothing could be
        checked" are different answers and an agent acts differently on each.
        """

        mesh = self._mesh_state(global_object)
        phases = self._phase_outcomes(global_object)
        materials = self._material_readiness(global_object)
        blocking, unknown, caveats = [], [], []

        if not mesh.get("available"):
            unknown.append("mesh: " + str(mesh.get("reason", "mesh status is not readable")))
        elif isinstance(mesh.get("element_count"), int) and mesh["element_count"] <= 0:
            blocking.append("No mesh has been generated; run generate_mesh before calculating.")
        elif mesh.get("staleness") == "stale":
            blocking.append(
                "PLAXIS reports the mesh as %s; regenerate it with generate_mesh before "
                "calculating." % mesh.get("status")
            )
        elif mesh.get("element_count") is None:
            unknown.append("mesh: PLAXIS reported no element count.")
        elif mesh.get("staleness") == "not_available":
            caveats.append(self._MESH_STALENESS_UNAVAILABLE % (mesh.get("status"),))
        elif mesh.get("staleness") != "ready":
            unknown.append(
                "mesh: PLAXIS reported status %r, which this build does not recognise as either "
                "up to date or stale." % (mesh.get("status"),)
            )

        if phases is None:
            unknown.append(
                "phases: this project exposes no Phases collection, so no phase check ran."
            )
        else:
            marked = [phase for phase in phases if phase.get("should_calculate") is True]
            unmarked_unknown = [
                phase for phase in phases if phase.get("should_calculate") is None
            ]
            if not phases:
                blocking.append("The model has no phases; create one before calculating.")
            elif len(phases) == 1:
                blocking.append(
                    "The model has only the initial phase; create a phase before calculating."
                )
            elif not marked and not unmarked_unknown:
                blocking.append(
                    "No phase is marked for calculation (ShouldCalculate is false on every phase)."
                )
            if unmarked_unknown:
                unknown.append(
                    "phases: %d phase(s) do not expose ShouldCalculate." % len(unmarked_unknown)
                )

        if materials["count"]:
            blocking.append(
                "%d feature(s) have no material assigned: %s."
                % (
                    materials["count"],
                    ", ".join(
                        str(feature.get("path") or feature.get("name"))
                        for feature in materials["features"][:10]
                    ),
                )
            )
        if materials["placeholder_count"]:
            # PLAXIS rejected exactly this at calculate ("Invalid material
            # <NoName> ..."), so it is a real blocker, not a caveat. The
            # material is named so the fix -- configure it or reassign -- is one
            # step, instead of a calculate that fails without saying why.
            blocking.append(
                "%d feature(s) reference a material that was never configured (its identification "
                "is still PLAXIS's placeholder); PLAXIS rejects an unconfigured material at "
                "calculate. Set its properties or assign a configured material: %s."
                % (
                    materials["placeholder_count"],
                    ", ".join(
                        "%s->%s" % (
                            feature.get("path") or feature.get("name"),
                            feature.get("material", "?"),
                        )
                        for feature in materials["placeholder_features"][:10]
                    ),
                )
            )
        if materials.get("scan_truncated"):
            unknown.append(
                "materials: the feature scan stopped at %d features, so later features were not "
                "checked." % materials["scan_limit"]
            )

        return {
            "mesh": mesh,
            "phase_count": None if phases is None else len(phases),
            "phases": phases,
            "unassigned_materials": materials,
            "blocking": blocking,
            "unknown": unknown,
            # A caveat is a limit of what PLAXIS exposes, not a finding about
            # this model -- kept out of "unknown" so a check that genuinely did
            # not run stays visible, and out of "blocking" so it is not read as
            # a fault. It is always worth showing next to calculable.
            "caveats": caveats,
            "calculable": None if unknown else not blocking,
        }

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
        except Exception as exc:
            raise vendor_error(
                "PLAXIS_ERROR", "PLAXIS could not retrieve the requested results.", exc
            )
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
        except Exception as exc:
            raise vendor_error(
                "PLAXIS_ERROR", "PLAXIS could not retrieve the requested result.", exc
            )
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

    def _error_response(
        self,
        request_id,
        code,
        message,
        retryable=False,
        plaxis_message=None,
        plaxis_message_truncated=False,
        details=None,
    ):
        error = {
            "code": code,
            "message": self._sanitize(message),
            "retryable": bool(retryable),
            "state": self._state,
        }
        if plaxis_message:
            error["plaxis_message"] = self._sanitize(plaxis_message)
            # Only ever present when something really was dropped, and never
            # signalled inside the text itself.
            if plaxis_message_truncated:
                error["plaxis_message_truncated"] = True
        if details:
            # Building an error must not be able to raise: a details payload
            # that cannot be serialised loses the details, never the error.
            try:
                error["details"] = self._json_value(details)
            except Exception:
                pass
        return {
            "protocol": PROTOCOL_VERSION,
            "id": request_id,
            "ok": False,
            "error": error,
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
