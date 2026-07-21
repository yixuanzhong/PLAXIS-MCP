import io
import json
import sys
import time
import unittest
from pathlib import Path

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
    WorkerRuntime,
)


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

    def getresults(self, phase, result_type, fem_type):
        return list(range(250))


class BridgeTests(unittest.TestCase):
    def make_session(self, password: str = "top-secret") -> BridgeSession:
        return BridgeSession(
            BridgeSessionConfig(
                worker_python=sys.executable,
                worker_script=str(Path(__file__).resolve()),
                worker_args=("--fake-worker",),
                role="input",
                password=password,
                startup_timeout=2.0,
                request_timeout=2.0,
                # This suite's worker is this test file itself, which is outside
                # the package directory the integrity gate normally requires.
                trust_worker_script_path=True,
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
