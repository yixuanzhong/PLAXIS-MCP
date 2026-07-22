import io
import json
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

def _fake_worker() -> None:
    state = {"connected": False, "password": ""}
    for raw in sys.stdin:
        request = json.loads(raw)
        operation = request["operation"]
        if operation == "malformed":
            sys.stdout.write("this is not JSON\n")
            sys.stdout.flush()
            continue
        if operation == "handshake":
            result = {
                "protocol": 1,
                "role": "input",
                "python_version": "3.8.10",
                "python_executable": "fake",
                "plxscripting_version": "fake",
                "plxscripting_path": "fake",
                "product_version": "fake",
                "capabilities": ["handshake", "status", "connect", "disconnect", "call"],
            }
            response = {"protocol": 1, "id": request["id"], "ok": True, "result": result}
        elif operation == "connect":
            state["connected"] = True
            state["password"] = request["params"]["password"]
            response = {
                "protocol": 1,
                "id": request["id"],
                "ok": True,
                "result": {"connected": True, "role": "input", "state": "connected"},
            }
        elif operation == "status":
            response = {
                "protocol": 1,
                "id": request["id"],
                "ok": True,
                "result": {"connected": state["connected"], "role": "input", "state": "connected" if state["connected"] else "disconnected"},
            }
        elif operation == "disconnect" or operation == "shutdown":
            state["connected"] = False
            response = {
                "protocol": 1,
                "id": request["id"],
                "ok": True,
                "result": {"connected": False, "role": "input", "state": "disconnected"},
            }
        elif operation == "call":
            method = request["params"]["method"]
            if method == "hang":
                time.sleep(2.0)
                response = {"protocol": 1, "id": request["id"], "ok": True, "result": {"late": True}}
            elif method == "leak":
                response = {
                    "protocol": 1,
                    "id": request["id"],
                    "ok": False,
                    "error": {"code": "PLAXIS_ERROR", "message": "password=" + state["password"], "retryable": False},
                }
            else:
                response = {"protocol": 1, "id": request["id"], "ok": True, "result": request["params"]}
        else:
            response = {
                "protocol": 1,
                "id": request["id"],
                "ok": False,
                "error": {"code": "UNKNOWN_OPERATION", "message": "unknown", "retryable": False},
            }
        sys.stdout.write(json.dumps(response, separators=(",", ":")) + "\n")
        sys.stdout.flush()
        if operation == "shutdown":
            return


if "--fake-worker" in sys.argv:
    # ``WorkerBridge`` deliberately starts the process in isolated mode.  Run
    # this stdlib-only fake before importing the package under test so it can
    # serve as a realistic stand-in for a bundled PLAXIS interpreter.
    _fake_worker()
    raise SystemExit(0)


from plaxis_mcp.bridge import (
    BridgeSession,
    BridgeSessionConfig,
    WorkerOperationError,
    WorkerProtocolError,
    WorkerTimeoutError,
)
from plaxis_mcp.worker_runtime import (
    CallDispatcher,
    ROLE_TOOL_ALLOWLISTS,
    WorkerError,
    WorkerRuntime,
    call_global_command,
    parse_path,
    resolve_reference,
    resolve_reference_list,
)


class ParsePathTests(unittest.TestCase):
    """worker_runtime.parse_path is the path parser that actually runs.

    plaxis_mcp.core has a separate, older parse_path, but core.py is not
    imported by anything outside its own tests -- it predates the
    split-runtime architecture and is not on any execution path.
    """

    def test_accepts_negative_indices(self) -> None:
        # PLAXIS's own command reference uses Plates[-1] for "last item"
        # (commands_activate.html); a bare "-" digit check rejected it.
        self.assertEqual(parse_path("Plates[-1]"), ["Plates", -1])
        self.assertEqual(parse_path("Foo[-2][-3]"), ["Foo", -2, -3])

    def test_accepts_a_member_immediately_after_a_closed_index(self) -> None:
        # Foo[i].member is the natural shape for "index into a collection,
        # then read a property". The "." handler treated the empty token
        # buffer right after a "]" as an empty-member-name error.
        self.assertEqual(parse_path("Plates[-1].Name"), ["Plates", -1, "Name"])
        self.assertEqual(parse_path("Plates[0].Name"), ["Plates", 0, "Name"])
        self.assertEqual(parse_path("Foo.Bar[2].Baz"), ["Foo", "Bar", 2, "Baz"])

    def test_rejects_malformed_indices(self) -> None:
        for path in ("Plates[-]", "Plates[--1]", "Plates[1.5]", "Plates[]"):
            with self.assertRaises(WorkerError):
                parse_path(path)

    def test_rejects_genuinely_empty_member_names(self) -> None:
        for path in (".Foo", "Foo..Bar", "Foo[1]..Bar"):
            with self.assertRaises(WorkerError):
                parse_path(path)


class ReferenceResolutionTests(unittest.TestCase):
    """The central WP-C fix: a JSON name must resolve to a real object.

    plxscripting sends a plain Python string as a quoted TEXT LITERAL, not a
    reference, even when it names a real PLAXIS object (verified by reading
    plxscripting's own param_to_string). call_global_command must therefore
    always receive already-resolved objects for reference-typed arguments,
    never the raw client-supplied string.
    """

    class Plate(object):
        def __init__(self, name):
            self.Name = name

    def _root(self):
        root = type("Root", (object,), {})()
        root.Plate_1 = self.Plate("Plate_1")
        root.Plate_2 = self.Plate("Plate_2")
        root.received = None

        def activate(objects, phases):
            root.received = (objects, phases)
            return True

        root.activate = activate
        return root

    def test_resolve_reference_returns_the_real_object_not_the_name(self) -> None:
        root = self._root()
        resolved = resolve_reference(root, "Plate_1", "path")
        self.assertIs(resolved, root.Plate_1)
        self.assertIsInstance(resolved, self.Plate)

    def test_resolve_reference_rejects_non_string_and_empty(self) -> None:
        root = self._root()
        for bad in (None, 3, "", ["Plate_1"]):
            with self.assertRaises(WorkerError):
                resolve_reference(root, bad, "path")

    def test_resolve_reference_list_returns_real_objects_for_sequence_form(self) -> None:
        root = self._root()
        resolved = resolve_reference_list(root, ["Plate_1", "Plate_2"], "paths")
        self.assertEqual(resolved, [root.Plate_1, root.Plate_2])
        self.assertIsInstance(resolved[0], self.Plate)

    def test_resolve_reference_list_rejects_non_list_and_empty(self) -> None:
        root = self._root()
        for bad in (None, "Plate_1", []):
            with self.assertRaises(WorkerError):
                resolve_reference_list(root, bad, "paths")

    def test_call_global_command_receives_resolved_objects_end_to_end(self) -> None:
        """activate(["Plate_1"], ["Phase_1"]) must reach PLAXIS with real
        objects in the argument lists, never the strings "Plate_1"/"Phase_1"
        -- a raw string there would be sent to PLAXIS as quoted text, which is
        not a reference and would silently activate nothing."""

        root = self._root()
        root.Phase_1 = type("Phase", (object,), {"Name": "Phase_1"})()
        objects = resolve_reference_list(root, ["Plate_1"], "paths")
        phases = resolve_reference_list(root, ["Phase_1"], "phases")
        call_global_command(root, "activate", objects, phases)

        received_objects, received_phases = root.received
        self.assertIs(received_objects[0], root.Plate_1)
        self.assertIs(received_phases[0], root.Phase_1)
        self.assertNotIsInstance(received_objects[0], str)

    def test_call_global_command_wraps_a_missing_command(self) -> None:
        root = self._root()
        with self.assertRaises(WorkerError):
            call_global_command(root, "not_a_real_command")

    def test_call_global_command_wraps_a_plaxis_side_failure(self) -> None:
        root = self._root()

        def failing():
            raise RuntimeError("boom")

        root.failing_command = failing
        with self.assertRaises(WorkerError):
            call_global_command(root, "failing_command")


class FakeServer:
    def __init__(self) -> None:
        self._session = self
        self.closed = False
        self.active = True
        self.server_full_name = "PLAXIS 2D V22"

    def close(self) -> None:
        self.closed = True

    def new(self):
        return True

    def open(self, filename):
        return filename

    def recover(self):
        return True


class FakeProperty:
    def __init__(self, value):
        self.value = value


class FakePhase:
    def __init__(self, name):
        self.Name = FakeProperty(name)
        self.Identification = FakeProperty(name)
        self.TypeName = FakeProperty("Phase")


class FakeResultTypes:
    class SoilValues:
        Utot = "soil-utot"

    def __init__(self):
        self.Soil = self.SoilValues()


class FakeRoot:
    def __init__(self):
        self.Phases = [FakePhase("Phase_1"), FakePhase("Phase_2")]
        self.Materials = []
        self.ResultTypes = FakeResultTypes()
        self.ProjectTitle = FakeProperty("Demo")
        self.ProjectDescription = FakeProperty("Bridge test")
        self.Filename = FakeProperty("demo.p2dx")
        self.UnitForce = FakeProperty("kN")
        self.UnitLength = FakeProperty("m")
        self.UnitTime = FakeProperty("day")
        self.Value = "before"

    def save(self):
        return True

    def saveas(self, filename):
        return filename

    def getresults(self, phase, result_type, fem_type, smoothing=None):
        self.last_getresults_call = (phase, result_type, fem_type, smoothing)
        return list(range(250))


class BridgeTests(unittest.TestCase):
    def make_session(self, password: str = "top-secret") -> BridgeSession:
        # This suite's worker is this test file itself, which sits outside the
        # package directory the integrity gate requires. The gate has no opt-out
        # by design -- production must not be able to weaken it -- so the trusted
        # roots are patched here instead, which only a test can do.
        patcher = patch.object(
            BridgeSessionConfig,
            "trusted_script_roots",
            lambda _self: (Path(__file__).parent.resolve(),),
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        return BridgeSession(
            BridgeSessionConfig(
                worker_python=sys.executable,
                worker_script=str(Path(__file__).resolve()),
                worker_args=("--fake-worker",),
                role="input",
                password=password,
                startup_timeout=2.0,
                request_timeout=2.0,
            )
        )

    def test_bridge_launches_isolated_worker_and_dispatches(self) -> None:
        session = self.make_session()
        self.addCleanup(session.close)

        connected = session.connect()
        self.assertTrue(connected["connected"])
        result = session.invoke("echo", value="ok")

        self.assertEqual(result["method"], "echo")
        self.assertEqual(result["params"], {"value": "ok"})
        self.assertEqual(session._bridge.command[1:3], ("-I", "-u"))
        self.assertEqual(session.status()["state"], "connected")

    def test_bridge_redacts_password_from_worker_errors(self) -> None:
        session = self.make_session("do-not-log-me")
        self.addCleanup(session.close)
        session.connect()

        with self.assertRaises(WorkerOperationError) as raised:
            session.invoke("leak")

        self.assertNotIn("do-not-log-me", str(raised.exception))
        self.assertIn("[REDACTED]", str(raised.exception))

    def test_timeout_marks_session_uncertain_and_stops_worker(self) -> None:
        session = self.make_session()
        self.addCleanup(session.close)
        session.connect()

        with self.assertRaises(WorkerTimeoutError):
            session.invoke("hang", timeout=0.05)

        self.assertTrue(session.uncertain)
        self.assertEqual(session.status()["state"], "uncertain")

    def test_uncertainty_survives_worker_replacement_until_reconnect(self) -> None:
        """A fresh worker does not make a half-applied mutation certain."""

        session = self.make_session()
        self.addCleanup(session.close)
        session.connect()

        with self.assertRaises(WorkerTimeoutError):
            session.invoke("hang", timeout=0.05)
        self.assertTrue(session.uncertain)

        # Any later call revives the worker. Restarting a process says nothing
        # about whether the interrupted PLAXIS operation landed, so the session
        # must stay uncertain and keep reporting it.
        session._bridge.start()
        self.assertTrue(session._bridge.started)
        self.assertFalse(session._bridge.worker_faulted)
        self.assertTrue(session.uncertain)
        self.assertEqual(session.status()["state"], "uncertain")

        # Only an explicit, successful reconnect clears it -- and says so.
        self.assertTrue(session.connect()["recovered_from_uncertain"])
        self.assertFalse(session.uncertain)
        self.assertNotEqual(session.status()["state"], "uncertain")
        # A routine reconnect must not claim a recovery that did not happen.
        self.assertFalse(session.connect()["recovered_from_uncertain"])

    def test_malformed_worker_record_is_a_protocol_failure(self) -> None:
        session = self.make_session()
        self.addCleanup(session.close)
        session._bridge.start()

        with self.assertRaises(WorkerProtocolError):
            session._bridge.request("malformed")

        self.assertTrue(session.uncertain)


class WorkerRuntimeTests(unittest.TestCase):
    def test_runtime_handshake_connect_call_disconnect(self) -> None:
        calls = []

        def factory(**kwargs):
            calls.append(kwargs)
            return FakeServer(), FakeRoot()

        dispatcher = CallDispatcher(
            {"inspect": lambda server, global_object, params: {"params": params, "title": global_object.ProjectTitle.value}}
        )
        runtime = WorkerRuntime(role="input", new_server_factory=factory, dispatcher=dispatcher)

        handshake = runtime.handle_request({"protocol": 1, "id": 1, "operation": "handshake", "params": {}})
        self.assertTrue(handshake["ok"])
        self.assertEqual(handshake["result"]["role"], "input")
        self.assertEqual(len(handshake["result"]["operations"]), 13)

        connected = runtime.handle_request(
            {
                "protocol": 1,
                "id": 2,
                "operation": "connect",
                "params": {"host": "127.0.0.1", "port": 10000, "password": "secret", "timeout": 5, "request_timeout": 60},
            }
        )
        self.assertTrue(connected["result"]["connected"])
        self.assertEqual(calls[0]["port"], 10000)
        self.assertEqual(connected["result"]["server_full_name"], "PLAXIS 2D V22")
        self.assertEqual(connected["result"]["generation"], 1)

        called = runtime.handle_request(
            {"protocol": 1, "id": 3, "operation": "call", "params": {"method": "inspect", "params": {"x": 1}}}
        )
        self.assertEqual(called["result"], {"params": {"x": 1}, "title": "Demo"})

        disconnected = runtime.handle_request({"protocol": 1, "id": 4, "operation": "disconnect", "params": {}})
        self.assertFalse(disconnected["result"]["connected"])

    def test_connect_refuses_an_endpoint_that_is_not_plaxis(self) -> None:
        """Any local service can occupy the configured port.

        Observed live: port 10001 was held by a security agent answering as
        "Qualys-Correlation", and connect reported a healthy PLAXIS session
        because it only checked server.active.
        """

        class Impostor(FakeServer):
            def __init__(self) -> None:
                super().__init__()
                self.server_full_name = "Qualys-Correlation"

        runtime = WorkerRuntime(role="input", new_server_factory=lambda **k: (Impostor(), object()))
        response = runtime.handle_request(
            {"protocol": 1, "id": 1, "operation": "connect",
             "params": {"host": "127.0.0.1", "port": 10001, "password": "pw", "timeout": 5}}
        )
        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["code"], "WRONG_ENDPOINT")
        self.assertIn("rotate", response["error"]["message"].lower())

        status = runtime.handle_request({"protocol": 1, "id": 2, "operation": "status", "params": {}})
        self.assertFalse(status["result"]["connected"])

    def test_connect_refuses_a_rejected_password(self) -> None:
        """new_server succeeds with any password.

        server.active and server_full_name are served unauthenticated, so
        without a probe an endpoint reported "connected" with a wrong or empty
        credential and only later queries failed. PLAXIS words the rejection
        two ways, and both must be caught.
        """

        for message in (
            "The server failed to decrypt the request. This probably means the password "
            "on the client does not match the one on the server.",
            'Request is missing "Code" key needed for decryption.',
        ):
            class Unauthenticated:
                def __getattr__(self, name):
                    raise RuntimeError(message)

            runtime = WorkerRuntime(
                role="input",
                new_server_factory=lambda **k: (FakeServer(), Unauthenticated()),
            )
            response = runtime.handle_request(
                {"protocol": 1, "id": 1, "operation": "connect",
                 "params": {"host": "127.0.0.1", "port": 10000, "password": "wrong", "timeout": 5}}
            )
            self.assertFalse(response["ok"], message)
            self.assertEqual(response["error"]["code"], "AUTHENTICATION_FAILED")
            status = runtime.handle_request({"protocol": 1, "id": 2, "operation": "status", "params": {}})
            self.assertFalse(status["result"]["connected"])

    def test_connect_allows_a_probe_that_fails_for_a_non_auth_reason(self) -> None:
        """A missing member is not an authentication failure.

        PLAXIS Output exposes no Project object even when authenticated, so a
        probe error unrelated to decryption must not block the connection.
        """

        class OnlyPhases:
            Phases = ["InitialPhase"]

            def __getattr__(self, name):
                raise AttributeError(name)

        runtime = WorkerRuntime(role="output",
                                new_server_factory=lambda **k: (FakeServer(), OnlyPhases()))
        response = runtime.handle_request(
            {"protocol": 1, "id": 1, "operation": "connect",
             "params": {"host": "127.0.0.1", "port": 10002, "password": "pw", "timeout": 5}}
        )
        self.assertTrue(response["ok"], response)
        self.assertTrue(response["result"]["connected"])

    def test_connect_refuses_the_other_plaxis_role(self) -> None:
        class OutputServer(FakeServer):
            def __init__(self) -> None:
                super().__init__()
                self.server_full_name = "PLAXIS 2D Output/22.2.0.1078"

        runtime = WorkerRuntime(role="input", new_server_factory=lambda **k: (OutputServer(), object()))
        response = runtime.handle_request(
            {"protocol": 1, "id": 1, "operation": "connect",
             "params": {"host": "127.0.0.1", "port": 10001, "password": "pw", "timeout": 5}}
        )
        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["code"], "WRONG_ENDPOINT")

    def _input_runtime(self, global_object):
        runtime = WorkerRuntime(role="input",
                                new_server_factory=lambda **k: (FakeServer(), global_object))
        runtime.handle_request({"protocol": 1, "id": 1, "operation": "connect",
                                "params": {"host": "127.0.0.1", "port": 10000, "password": "pw", "timeout": 5}})
        return runtime

    def _call(self, runtime, method, **params):
        return runtime.handle_request({"protocol": 1, "id": 9, "operation": "call",
                                       "params": {"method": method, "params": params}})

    def test_open_project_rejects_a_path_with_no_project_file(self) -> None:
        """Observed live: opening a missing path reported a successful open."""

        runtime = self._input_runtime(object())
        response = self._call(runtime, "open_project", filename=r"C:\tmp\definitely-not-here.p2dx")
        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["code"], "INVALID_PATH")

    def test_set_property_refuses_a_path_naming_an_object(self) -> None:
        """A proxy accepts setattr for anything, so an object assignment
        silently landed on the local proxy and read back as success."""

        class Proxy:
            _guid = "{OBJECT}"

        class Global:
            def __init__(self):
                self.Project = Proxy()

        runtime = self._input_runtime(Global())
        response = self._call(runtime, "set_property", path="Project", value=1)
        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["code"], "INVALID_PATH")

    def test_set_property_allows_a_writable_property_and_reports_the_old_value(self) -> None:
        class Prop:
            _guid = "{PROP}"

            def __init__(self, value):
                self.value = value

        class Project:
            def __init__(self):
                self.Title = Prop("before")

        class Global:
            def __init__(self):
                self.Project = Project()

        runtime = self._input_runtime(Global())
        response = self._call(runtime, "set_property", path="Project.Title", value="after")
        self.assertTrue(response["ok"], response)
        # The snapshot must survive the mutation: a live proxy described after
        # the set would report the new value as the previous one.
        self.assertEqual(response["result"]["previous"], {"kind": "property", "value": "before"})
        self.assertEqual(response["result"]["value"], "after")

    def test_runtime_masks_connection_secret_and_rejects_unregistered_calls(self) -> None:
        def failing_factory(**kwargs):
            raise RuntimeError("could not connect using " + kwargs["password"])

        runtime = WorkerRuntime(role="input", new_server_factory=failing_factory)
        failed = runtime.handle_request(
            {
                "protocol": 1,
                "id": 1,
                "operation": "connect",
                "params": {"host": "127.0.0.1", "port": 10000, "password": "hidden", "timeout": 5},
            }
        )
        self.assertFalse(failed["ok"])
        self.assertNotIn("hidden", json.dumps(failed))
        self.assertEqual(failed["error"]["code"], "CONNECTION_FAILED")

        not_connected = runtime.handle_request(
            {"protocol": 1, "id": 2, "operation": "call", "params": {"method": "anything", "params": {}}}
        )
        self.assertEqual(not_connected["error"]["code"], "NOT_CONNECTED")

    def test_role_allowlists_and_paginated_output_results(self) -> None:
        self.assertEqual(len(ROLE_TOOL_ALLOWLISTS["input"]), 15)
        self.assertEqual(len(ROLE_TOOL_ALLOWLISTS["output"]), 10)

        def factory(**kwargs):
            return FakeServer(), FakeRoot()

        output = WorkerRuntime(role="output", new_server_factory=factory)
        output.handle_request(
            {"protocol": 1, "id": 1, "operation": "connect", "params": {"host": "127.0.0.1", "port": 10001, "password": "secret"}}
        )
        first = output.handle_request(
            {
                "protocol": 1,
                "id": 2,
                "operation": "call",
                "params": {"method": "get_results", "params": {"phase": 0, "result_type_path": "ResultTypes.Soil.Utot", "limit": 200}},
            }
        )
        self.assertTrue(first["ok"])
        self.assertEqual(first["result"]["count"], 250)
        self.assertEqual(first["result"]["returned_count"], 200)
        self.assertTrue(first["result"]["has_more"])
        self.assertEqual(first["result"]["next_offset"], 200)

        second = output.handle_request(
            {
                "protocol": 1,
                "id": 3,
                "operation": "call",
                "params": {"method": "get_results", "params": {"phase": 0, "result_type_path": "ResultTypes.Soil.Utot", "offset": 200, "limit": 200}},
            }
        )
        self.assertEqual(second["result"]["returned_count"], 50)
        self.assertFalse(second["result"]["has_more"])
        self.assertIsNone(second["result"]["next_offset"])

        wrong_role = output.handle_request(
            {"protocol": 1, "id": 4, "operation": "call", "params": {"method": "new_project", "params": {}}}
        )
        self.assertEqual(wrong_role["error"]["code"], "WRONG_ROLE")

        input_runtime = WorkerRuntime(role="input", new_server_factory=factory)
        input_runtime.handle_request(
            {"protocol": 1, "id": 5, "operation": "connect", "params": {"host": "127.0.0.1", "port": 10000, "password": "secret"}}
        )
        no_results = input_runtime.handle_request(
            {"protocol": 1, "id": 6, "operation": "call", "params": {"method": "get_results", "params": {}}}
        )
        self.assertEqual(no_results["error"]["code"], "WRONG_ROLE")

    def test_get_results_validates_fem_type_and_forwards_smoothing(self) -> None:
        """fem_type has exactly four valid values (commands_getresults.html);
        anything else must be rejected client-side, not sent to PLAXIS. The
        trailing smoothing Boolean is a real, optional getresults parameter
        that must only be forwarded when the caller actually asked for it."""

        roots: list = []

        def factory(**kwargs):
            root = FakeRoot()
            roots.append(root)
            return FakeServer(), root

        output = WorkerRuntime(role="output", new_server_factory=factory)
        output.handle_request(
            {"protocol": 1, "id": 1, "operation": "connect", "params": {"host": "127.0.0.1", "port": 10001, "password": "secret"}}
        )

        bad = output.handle_request(
            {
                "protocol": 1, "id": 2, "operation": "call",
                "params": {"method": "get_results", "params": {
                    "phase": 0, "result_type_path": "ResultTypes.Soil.Utot", "fem_type": "not-a-real-choice",
                }},
            }
        )
        self.assertFalse(bad["ok"])
        self.assertEqual(bad["error"]["code"], "INVALID_ARGUMENT")

        smoothed = output.handle_request(
            {
                "protocol": 1, "id": 3, "operation": "call",
                "params": {"method": "get_results", "params": {
                    "phase": 0, "result_type_path": "ResultTypes.Soil.Utot",
                    "fem_type": "stresspoint", "smoothing": True, "limit": 5,
                }},
            }
        )
        self.assertTrue(smoothed["ok"], smoothed)
        self.assertEqual(smoothed["result"]["fem_type"], "stresspoint")
        self.assertTrue(smoothed["result"]["smoothing"])
        self.assertEqual(roots[-1].last_getresults_call[2:], ("stresspoint", True))

        unsmoothed = output.handle_request(
            {
                "protocol": 1, "id": 4, "operation": "call",
                "params": {"method": "get_results", "params": {
                    "phase": 0, "result_type_path": "ResultTypes.Soil.Utot", "limit": 5,
                }},
            }
        )
        self.assertTrue(unsmoothed["ok"], unsmoothed)
        self.assertIsNone(unsmoothed["result"]["smoothing"])
        # Omitted smoothing must not be forwarded as an explicit None argument.
        # get_results calls resolve_phase first, so the worker's own call
        # receives the resolved phase OBJECT, not the raw index the client sent.
        self.assertEqual(
            roots[-1].last_getresults_call,
            (roots[-1].Phases[0], roots[-1].ResultTypes.Soil.Utot, "node", None),
        )

    def test_private_path_or_method_is_rejected(self) -> None:
        def factory(**kwargs):
            return FakeServer(), FakeRoot()

        runtime = WorkerRuntime(role="input", new_server_factory=factory)
        runtime.handle_request(
            {
                "protocol": 1,
                "id": 1,
                "operation": "connect",
                "params": {"host": "127.0.0.1", "port": 10000, "password": "secret"},
            }
        )
        private_path = runtime.handle_request(
            {
                "protocol": 1,
                "id": 2,
                "operation": "call",
                "params": {"method": "inspect", "params": {"path": "__class__"}},
            }
        )
        private_method = runtime.handle_request(
            {
                "protocol": 1,
                "id": 3,
                "operation": "call",
                "params": {"method": "call_method", "params": {"method": "__getattribute__"}},
            }
        )
        self.assertEqual(private_path["error"]["code"], "INVALID_PATH")
        self.assertEqual(private_method["error"]["code"], "INVALID_PATH")


if __name__ == "__main__":
    unittest.main()
