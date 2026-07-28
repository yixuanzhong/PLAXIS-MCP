import io
import json
import os
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
    call_plaxis_command,
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
    plxscripting's own param_to_string). call_plaxis_command must therefore
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

    def test_call_plaxis_command_receives_resolved_objects_end_to_end(self) -> None:
        """activate(["Plate_1"], ["Phase_1"]) must reach PLAXIS with real
        objects in the argument lists, never the strings "Plate_1"/"Phase_1"
        -- a raw string there would be sent to PLAXIS as quoted text, which is
        not a reference and would silently activate nothing."""

        root = self._root()
        root.Phase_1 = type("Phase", (object,), {"Name": "Phase_1"})()
        objects = resolve_reference_list(root, ["Plate_1"], "paths")
        phases = resolve_reference_list(root, ["Phase_1"], "phases")
        call_plaxis_command(root, "activate", objects, phases)

        received_objects, received_phases = root.received
        self.assertIs(received_objects[0], root.Plate_1)
        self.assertIs(received_phases[0], root.Phase_1)
        self.assertNotIsInstance(received_objects[0], str)

    def test_call_plaxis_command_wraps_a_missing_command(self) -> None:
        root = self._root()
        with self.assertRaises(WorkerError):
            call_plaxis_command(root, "not_a_real_command")

    def test_call_plaxis_command_wraps_a_plaxis_side_failure(self) -> None:
        root = self._root()

        def failing():
            raise RuntimeError("boom")

        root.failing_command = failing
        with self.assertRaises(WorkerError):
            call_plaxis_command(root, "failing_command")


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

    def setproperties(self, property_name, phase, value):
        self.last_setproperties_call = (property_name, phase, value)
        return True


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

    def getsingleresult(self, *args):
        self.last_getsingleresult_call = args
        return 1.23

    def phase(self, after_phase):
        self.last_phase_call = ("phase", after_phase)
        return FakePhase("NewPhase")

    def insertphase(self, before_phase):
        self.last_phase_call = ("insertphase", before_phase)
        return FakePhase("NewPhase")

    def setcurrentphase(self, phase):
        self.last_setcurrentphase_call = phase
        return True

    def activate(self, objects, phases):
        self.last_activate_call = (objects, phases)
        return True

    def deactivate(self, objects, phases):
        self.last_deactivate_call = (objects, phases)
        return True

    def calculate(self, *phases):
        self.last_calculate_call = phases
        return True

    def gotosoil(self):
        self.last_mode_call = ("gotosoil",)
        return True

    def gotostructures(self):
        self.last_mode_call = ("gotostructures",)
        return True

    def gotomesh(self, *args):
        self.last_mode_call = ("gotomesh",) + args
        return True

    def gotowater(self, *args):
        self.last_mode_call = ("gotowater",) + args
        return True

    def gotostages(self, *args):
        self.last_mode_call = ("gotostages",) + args
        return True

    def mesh(self, *args):
        self.last_mesh_call = args
        return True

    def point(self, *coordinates):
        self.last_point_call = coordinates
        return True

    def line(self, *vertices):
        self.last_line_call = vertices
        return True

    def polygon(self, *vertices):
        self.last_polygon_call = vertices
        return True

    def borehole(self, x):
        self.last_borehole_call = x
        return True

    def soillayer(self, height):
        self.last_soillayer_call = height
        return True

    def soilmat(self, *args):
        self.last_material_call = ("soilmat", args)
        return True

    def platemat(self, *args):
        self.last_material_call = ("platemat", args)
        return True

    def geogridmat(self, *args):
        self.last_material_call = ("geogridmat", args)
        return True

    def anchormat(self, *args):
        self.last_material_call = ("anchormat", args)
        return True

    def embeddedbeammat(self, *args):
        self.last_material_call = ("embeddedbeammat", args)
        return True

    def setmaterial(self, features, material):
        self.last_setmaterial_call = (features, material)
        return True

    def plate(self, *lines):
        self.last_structural_call = ("plate", lines)
        return True

    def geogrid(self, *lines):
        self.last_structural_call = ("geogrid", lines)
        return True

    def embeddedbeam(self, *lines):
        self.last_structural_call = ("embeddedbeam", lines)
        return True

    def n2nanchor(self, *lines):
        self.last_structural_call = ("n2nanchor", lines)
        return True


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

    def test_worker_environment_defaults_project_dir_to_launch_cwd(self) -> None:
        session = self.make_session()
        self.addCleanup(session.close)
        environment = session._bridge._worker_environment()
        self.assertEqual(environment["PLAXIS_PROJECT_DIR"], os.getcwd())

    def test_worker_environment_keeps_an_explicit_project_dir_override(self) -> None:
        patcher = patch.object(
            BridgeSessionConfig,
            "trusted_script_roots",
            lambda _self: (Path(__file__).parent.resolve(),),
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        session = BridgeSession(
            BridgeSessionConfig(
                worker_python=sys.executable,
                worker_script=str(Path(__file__).resolve()),
                worker_args=("--fake-worker",),
                role="input",
                password="pw",
                startup_timeout=2.0,
                request_timeout=2.0,
                environment={"PLAXIS_PROJECT_DIR": r"D:\Projects\Site"},
            )
        )
        self.addCleanup(session.close)
        self.assertEqual(session._bridge._worker_environment()["PLAXIS_PROJECT_DIR"], r"D:\Projects\Site")

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
        # 29 before the error-fidelity work; list_objects and model_state are
        # the two operations it adds to the input role (WP-G, WP-H), and
        # view_results is the one the live Output test showed was missing.
        self.assertEqual(len(handshake["result"]["operations"]), 32)

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
        # 31/11 before list_objects, model_state and view_results, all
        # input-only: the first two answer questions about a model being
        # built, and the third hands a calculated phase to Output, which is
        # the only route from a calculated model to the Output endpoint.
        self.assertEqual(len(ROLE_TOOL_ALLOWLISTS["input"]), 34)
        self.assertEqual(len(ROLE_TOOL_ALLOWLISTS["output"]), 11)

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

    def test_get_single_result_by_point_or_coords_not_both_or_neither(self) -> None:
        roots: list = []

        def factory(**kwargs):
            root = FakeRoot()
            roots.append(root)
            return FakeServer(), root

        output = WorkerRuntime(role="output", new_server_factory=factory)
        output.handle_request(
            {"protocol": 1, "id": 1, "operation": "connect", "params": {"host": "127.0.0.1", "port": 10002, "password": "secret"}}
        )

        neither = output.handle_request(
            {"protocol": 1, "id": 2, "operation": "call", "params": {"method": "get_single_result", "params": {
                "phase": 0, "result_type_path": "ResultTypes.Soil.Utot",
            }}}
        )
        self.assertFalse(neither["ok"])
        self.assertEqual(neither["error"]["code"], "INVALID_ARGUMENT")

        both = output.handle_request(
            {"protocol": 1, "id": 3, "operation": "call", "params": {"method": "get_single_result", "params": {
                "phase": 0, "result_type_path": "ResultTypes.Soil.Utot", "point": "Phases[0]", "coords": [1, 2],
            }}}
        )
        self.assertFalse(both["ok"])
        self.assertEqual(both["error"]["code"], "INVALID_ARGUMENT")

        by_point = output.handle_request(
            {"protocol": 1, "id": 4, "operation": "call", "params": {"method": "get_single_result", "params": {
                "phase": 0, "result_type_path": "ResultTypes.Soil.Utot", "point": "Phases[1]", "smoothing": True,
            }}}
        )
        self.assertTrue(by_point["ok"], by_point)
        self.assertEqual(by_point["result"]["result"], 1.23)
        phase, result_type, point, smoothing = roots[-1].last_getsingleresult_call
        self.assertIs(point, roots[-1].Phases[1])
        self.assertNotIsInstance(point, str)
        self.assertTrue(smoothing)

        by_coords = output.handle_request(
            {"protocol": 1, "id": 5, "operation": "call", "params": {"method": "get_single_result", "params": {
                "phase": 0, "result_type_path": "ResultTypes.Soil.Utot", "coords": [3.0, 5.0],
            }}}
        )
        self.assertTrue(by_coords["ok"], by_coords)
        # Omitted smoothing means no trailing argument at all, matching
        # get_results's same "don't forward an explicit None" rule.
        self.assertEqual(roots[-1].last_getsingleresult_call, (roots[-1].Phases[0], roots[-1].ResultTypes.Soil.Utot, 3.0, 5.0))

    def test_create_phase_dispatches_to_phase_or_insertphase_by_position(self) -> None:
        root = FakeRoot()
        runtime = self._input_runtime(root)

        after = self._call(runtime, "create_phase", reference_phase="Phases[0]")
        self.assertTrue(after["ok"], after)
        self.assertEqual(after["result"]["position"], "after")
        self.assertEqual(root.last_phase_call, ("phase", root.Phases[0]))

        before = self._call(runtime, "create_phase", reference_phase="Phases[1]", position="before")
        self.assertTrue(before["ok"], before)
        self.assertEqual(root.last_phase_call, ("insertphase", root.Phases[1]))

    def test_set_current_phase_resolves_the_reference(self) -> None:
        root = FakeRoot()
        runtime = self._input_runtime(root)
        result = self._call(runtime, "set_current_phase", phase="Phases[1]")
        self.assertTrue(result["ok"], result)
        self.assertIs(root.last_setcurrentphase_call, root.Phases[1])

    def test_activate_and_deactivate_pass_resolved_lists_not_strings(self) -> None:
        """activate's sequence-form signature -- "activate (obj...) (phase...)"
        -- means the two resolved lists must reach PLAXIS as two arguments,
        each a list of real objects, never the raw path strings."""

        root = FakeRoot()
        runtime = self._input_runtime(root)
        result = self._call(runtime, "activate", paths=["Phases[0]"], phases=["Phases[1]"])
        self.assertTrue(result["ok"], result)
        objects, phases = root.last_activate_call
        self.assertEqual(objects, [root.Phases[0]])
        self.assertEqual(phases, [root.Phases[1]])
        self.assertNotIsInstance(objects[0], str)

        deactivated = self._call(runtime, "deactivate", paths=["Phases[0]"], phases=["Phases[1]"])
        self.assertTrue(deactivated["ok"], deactivated)
        self.assertEqual(root.last_deactivate_call, ([root.Phases[0]], [root.Phases[1]]))

    def test_calculate_star_unpacks_phases_unlike_activate(self) -> None:
        """calculate's explicit-phases signature is variadic ("calculate
        Phase_1 Phase_6"), not a parenthesised sequence -- the resolved
        phases must arrive as separate positional arguments, not one list
        argument, which is the opposite calling convention from activate."""

        root = FakeRoot()
        runtime = self._input_runtime(root)

        no_args = self._call(runtime, "calculate")
        self.assertTrue(no_args["ok"], no_args)
        self.assertEqual(root.last_calculate_call, ())

        explicit = self._call(runtime, "calculate", phases=["Phases[0]", "Phases[1]"])
        self.assertTrue(explicit["ok"], explicit)
        self.assertEqual(root.last_calculate_call, (root.Phases[0], root.Phases[1]))

    def test_set_phase_property_calls_setproperties_on_the_target_not_the_global_object(self) -> None:
        root = FakeRoot()
        runtime = self._input_runtime(root)
        result = self._call(
            runtime, "set_phase_property",
            path="Phases[0]", property="Adjustprestress", phase="Phases[1]", value=True,
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(root.Phases[0].last_setproperties_call, ("Adjustprestress", root.Phases[1], True))

    def test_set_mode_dispatches_and_gates_force_new_intersection(self) -> None:
        root = FakeRoot()
        runtime = self._input_runtime(root)

        soil = self._call(runtime, "set_mode", mode="soil")
        self.assertTrue(soil["ok"], soil)
        self.assertEqual(root.last_mode_call, ("gotosoil",))

        # gotosoil/gotostructures take no parameters; force_new_intersection
        # only applies to mesh/water/stages.
        rejected = self._call(runtime, "set_mode", mode="soil", force_new_intersection=True)
        self.assertFalse(rejected["ok"])
        self.assertEqual(rejected["error"]["code"], "INVALID_ARGUMENT")

        mesh_mode = self._call(runtime, "set_mode", mode="mesh", force_new_intersection=True)
        self.assertTrue(mesh_mode["ok"], mesh_mode)
        self.assertEqual(root.last_mode_call, ("gotomesh", True))

    def test_generate_mesh_requires_size_before_enhanced_refinement(self) -> None:
        root = FakeRoot()
        runtime = self._input_runtime(root)

        bare = self._call(runtime, "generate_mesh")
        self.assertTrue(bare["ok"], bare)
        self.assertEqual(root.last_mesh_call, ())

        rejected = self._call(runtime, "generate_mesh", enhanced_refinement=True)
        self.assertFalse(rejected["ok"])
        self.assertEqual(rejected["error"]["code"], "INVALID_ARGUMENT")

        full = self._call(runtime, "generate_mesh", relative_element_size=0.1, enhanced_refinement=True)
        self.assertTrue(full["ok"], full)
        self.assertEqual(root.last_mesh_call, (0.1, True))
        # F5: "mesh" only *executes* in mesh mode (it is attribute-visible but
        # not runnable in stages mode), so generate_mesh enters mesh mode first.
        self.assertEqual(root.last_mode_call[0], "gotomesh")

    def test_create_point_passes_literal_coordinate_tuples(self) -> None:
        root = FakeRoot()
        runtime = self._input_runtime(root)
        result = self._call(runtime, "create_point", coordinates=[[5, 6], [8, 9]])
        self.assertTrue(result["ok"], result)
        self.assertEqual(root.last_point_call, ((5, 6), (8, 9)))

    def test_create_line_and_polygon_accept_mixed_reference_and_literal_vertices(self) -> None:
        """Each vertex is either a [x, y] literal or a path to an existing
        point -- both line and polygon must resolve string vertices to the
        real object while passing literal coordinates through unchanged."""

        root = FakeRoot()
        runtime = self._input_runtime(root)

        line_result = self._call(runtime, "create_line", vertices=["Phases[0]", [1, 2]])
        self.assertTrue(line_result["ok"], line_result)
        self.assertEqual(root.last_line_call, (root.Phases[0], (1, 2)))
        self.assertNotIsInstance(root.last_line_call[0], str)

        polygon_result = self._call(runtime, "create_polygon", vertices=[[0, 0], [4, 0], "Phases[1]"])
        self.assertTrue(polygon_result["ok"], polygon_result)
        self.assertEqual(root.last_polygon_call, ((0, 0), (4, 0), root.Phases[1]))

    def test_create_line_and_polygon_enforce_minimum_vertex_counts(self) -> None:
        root = FakeRoot()
        runtime = self._input_runtime(root)

        too_few_line = self._call(runtime, "create_line", vertices=[[0, 0]])
        self.assertFalse(too_few_line["ok"])
        self.assertEqual(too_few_line["error"]["code"], "INVALID_ARGUMENT")

        too_few_polygon = self._call(runtime, "create_polygon", vertices=[[0, 0], [1, 1]])
        self.assertFalse(too_few_polygon["ok"])
        self.assertEqual(too_few_polygon["error"]["code"], "INVALID_ARGUMENT")

    def test_create_borehole_and_soillayer_accept_any_sign_or_zero(self) -> None:
        """x is a coordinate, not a magnitude: negative and zero are valid,
        unlike _optional_number's existing positive-only callers."""

        root = FakeRoot()
        runtime = self._input_runtime(root)

        for x in (-5.0, 0.0, 5.0):
            result = self._call(runtime, "create_borehole", x=x)
            self.assertTrue(result["ok"], result)
            self.assertEqual(root.last_borehole_call, x)

        soil = self._call(runtime, "create_soillayer", height=3)
        self.assertTrue(soil["ok"], soil)
        self.assertEqual(root.last_soillayer_call, 3)

    def test_create_material_dispatches_by_kind_and_flattens_properties(self) -> None:
        root = FakeRoot()
        runtime = self._input_runtime(root)

        placeholder = self._call(runtime, "create_material", kind="soil")
        self.assertTrue(placeholder["ok"], placeholder)
        self.assertEqual(root.last_material_call, ("soilmat", ()))

        full = self._call(
            runtime, "create_material", kind="plate",
            properties={"Identification": "Footing", "MaterialType": "Elastic"},
        )
        self.assertTrue(full["ok"], full)
        command, args = root.last_material_call
        self.assertEqual(command, "platemat")
        # Property order from a dict is insertion order in Python 3.7+; verify
        # the pairs are flattened correctly rather than assuming a fixed order.
        self.assertEqual(dict(zip(args[0::2], args[1::2])), {"Identification": "Footing", "MaterialType": "Elastic"})

    def test_create_material_rejects_empty_properties_and_unknown_kind(self) -> None:
        root = FakeRoot()
        runtime = self._input_runtime(root)

        empty = self._call(runtime, "create_material", kind="soil", properties={})
        self.assertFalse(empty["ok"])
        self.assertEqual(empty["error"]["code"], "INVALID_ARGUMENT")

        unknown = self._call(runtime, "create_material", kind="not-a-real-kind")
        self.assertFalse(unknown["ok"])
        self.assertEqual(unknown["error"]["code"], "INVALID_ARGUMENT")

    def test_assign_material_resolves_features_and_material_not_strings(self) -> None:
        root = FakeRoot()
        runtime = self._input_runtime(root)
        result = self._call(runtime, "assign_material", paths=["Phases[0]"], material="Phases[1]")
        self.assertTrue(result["ok"], result)
        features, material = root.last_setmaterial_call
        self.assertEqual(features, [root.Phases[0]])
        self.assertIs(material, root.Phases[1])
        self.assertNotIsInstance(features[0], str)
        self.assertNotIsInstance(material, str)

    def test_create_structural_element_dispatches_by_kind_with_resolved_lines(self) -> None:
        root = FakeRoot()
        runtime = self._input_runtime(root)

        for kind, command_name in (
            ("plate", "plate"), ("geogrid", "geogrid"),
            ("embedded_beam", "embeddedbeam"), ("n2n_anchor", "n2nanchor"),
        ):
            result = self._call(runtime, "create_structural_element", kind=kind, paths=["Phases[0]"])
            self.assertTrue(result["ok"], result)
            called_command, lines = root.last_structural_call
            self.assertEqual(called_command, command_name)
            self.assertEqual(lines, (root.Phases[0],))
            self.assertNotIsInstance(lines[0], str)

    def test_create_structural_element_rejects_unknown_kind(self) -> None:
        root = FakeRoot()
        runtime = self._input_runtime(root)
        result = self._call(runtime, "create_structural_element", kind="not-a-real-kind", paths=["Phases[0]"])
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "INVALID_ARGUMENT")

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
