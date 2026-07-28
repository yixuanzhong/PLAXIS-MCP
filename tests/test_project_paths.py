"""Relative save/open paths resolve against the session working directory.

A client that launches this server (goose/Caros, and any other stdio MCP
client) sets its working directory as the server's cwd; the host passes that on
to the worker as PLAXIS_PROJECT_DIR. A bare "model.p2dx" then lands where the
user is working rather than in PLAXIS's own cwd, while an absolute path is used
verbatim.
"""

import os
import tempfile
import unittest
from unittest.mock import patch

from plaxis_mcp.worker_runtime import WorkerRuntime


class FakeProjectServer:
    """Records the path PLAXIS is handed and simulates PLAXIS writing there."""

    def __init__(self):
        self.active = True
        self.server_full_name = "PLAXIS 2D V22"
        self.opened = None
        self.saved = None

    def close(self):
        return None

    def open(self, path):
        self.opened = path
        return "OK"

    def save(self, path=None):
        self.saved = path
        if path is not None:
            with open(path, "w"):
                pass
        return "OK"


def _runtime(target):
    runtime = WorkerRuntime(
        role="input", new_server_factory=lambda **kwargs: (target, target)
    )
    runtime.handle_request(
        {
            "protocol": 1,
            "id": 1,
            "operation": "connect",
            "params": {"host": "127.0.0.1", "port": 10000, "password": "pw", "timeout": 5},
        }
    )
    return runtime


def _call(runtime, operation, **params):
    return runtime.handle_request(
        {
            "protocol": 1,
            "id": 9,
            "operation": "call",
            "params": {"method": operation, "params": params},
        }
    )


class ResolveProjectPathTests(unittest.TestCase):
    def test_absolute_path_passes_through(self):
        absolute = os.path.join(tempfile.gettempdir(), "already-absolute.p2dx")
        with patch.dict(os.environ, {"PLAXIS_PROJECT_DIR": tempfile.gettempdir()}):
            self.assertEqual(WorkerRuntime._resolve_project_path(absolute), absolute)

    def test_relative_resolves_against_base(self):
        with tempfile.TemporaryDirectory() as base:
            with patch.dict(os.environ, {"PLAXIS_PROJECT_DIR": base}):
                self.assertEqual(
                    WorkerRuntime._resolve_project_path("model.p2dx"),
                    os.path.normpath(os.path.join(base, "model.p2dx")),
                )

    def test_relative_without_base_is_unchanged(self):
        environment = dict(os.environ)
        environment.pop("PLAXIS_PROJECT_DIR", None)
        with patch.dict(os.environ, environment, clear=True):
            self.assertEqual(WorkerRuntime._resolve_project_path("model.p2dx"), "model.p2dx")


class SaveOpenResolutionTests(unittest.TestCase):
    def test_relative_save_lands_in_base_and_is_reported_absolute(self):
        with tempfile.TemporaryDirectory() as base:
            with patch.dict(os.environ, {"PLAXIS_PROJECT_DIR": base}):
                target = FakeProjectServer()
                response = _call(_runtime(target), "save_project", filename="demo.p2dx")
                expected = os.path.normpath(os.path.join(base, "demo.p2dx"))
                # PLAXIS is handed the resolved absolute path, not the bare name.
                self.assertEqual(target.saved, expected)
                # The caller is told exactly where it landed.
                self.assertEqual(response["result"]["filename"], expected)
                self.assertTrue(os.path.isfile(expected))

    def test_absolute_save_is_used_verbatim(self):
        with tempfile.TemporaryDirectory() as base, tempfile.TemporaryDirectory() as elsewhere:
            with patch.dict(os.environ, {"PLAXIS_PROJECT_DIR": base}):
                target = FakeProjectServer()
                absolute = os.path.join(elsewhere, "explicit.p2dx")
                _call(_runtime(target), "save_project", filename=absolute)
                self.assertEqual(target.saved, absolute)

    def test_save_in_place_needs_no_path(self):
        with tempfile.TemporaryDirectory() as base:
            with patch.dict(os.environ, {"PLAXIS_PROJECT_DIR": base}):
                target = FakeProjectServer()
                response = _call(_runtime(target), "save_project")
                self.assertEqual(target.saved, None)
                self.assertEqual(response["result"]["action"], "save_project")

    def test_relative_open_resolves_against_base(self):
        with tempfile.TemporaryDirectory() as base:
            existing = os.path.join(base, "existing.p2dx")
            with open(existing, "w"):
                pass
            with patch.dict(os.environ, {"PLAXIS_PROJECT_DIR": base}):
                target = FakeProjectServer()
                response = _call(_runtime(target), "open_project", filename="existing.p2dx")
                self.assertEqual(target.opened, os.path.normpath(existing))
                self.assertEqual(response["result"]["filename"], os.path.normpath(existing))

    def test_relative_open_of_missing_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as base:
            with patch.dict(os.environ, {"PLAXIS_PROJECT_DIR": base}):
                response = _call(_runtime(FakeProjectServer()), "open_project", filename="ghost.p2dx")
                self.assertEqual(response["error"]["code"], "INVALID_PATH")


if __name__ == "__main__":
    unittest.main()
