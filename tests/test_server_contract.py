import asyncio
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from plaxis_mcp import __version__
from plaxis_mcp.bridge import WorkerOperationError
from plaxis_mcp.deployment import (
    EndpointProfile,
    credential_target_for,
    default_profile_directory,
)
from mcp.server.fastmcp.exceptions import ToolError
from plaxis_mcp.server import (
    PlaxisRole,
    ServerConfig,
    ServerConfigurationError,
    create_server,
    load_server_config,
    main,
    server_config_from_profile,
)


class FakeLegacySession:
    """A direct-style fake confirms the host never needs a vendor import."""

    def __init__(self):
        self.connected = False
        self.connect_kwargs = None
        self.calls = []

    def connect(self, **kwargs):
        self.connect_kwargs = kwargs
        self.connected = True
        return self.status()

    def disconnect(self):
        self.connected = False
        return self.status()

    def status(self):
        return {
            "connected": self.connected,
            "state": "connected" if self.connected else "disconnected",
            "server_full_name": "PLAXIS Fake",
            "product_version": "test",
        }

    def __getattr__(self, name):
        if name == "invoke":
            raise AttributeError(name)

        def operation(**kwargs):
            self.calls.append((name, kwargs))
            return {"operation": name, "arguments": kwargs}

        return operation


async def _tool_names(server):
    return {tool.name: tool for tool in await server.list_tools()}


class ServerContractTests(unittest.TestCase):
    def make_server(self, role):
        session = FakeLegacySession()
        server = create_server(
            ServerConfig(
                role=role,
                host="127.0.0.1",
                password="not-for-client",
                runtime_profile="test-profile",
            ),
            session_factory=lambda: session,
        )
        return server, session

    def test_input_and_output_expose_exact_role_toolsets(self):
        input_server, _ = self.make_server(PlaxisRole.INPUT)
        output_server, _ = self.make_server(PlaxisRole.OUTPUT)

        input_tools = asyncio.run(_tool_names(input_server))
        output_tools = asyncio.run(_tool_names(output_server))

        self.assertEqual(
            set(input_tools),
            {
                "connect",
                "disconnect",
                "connection_status",
                "list_members",
                "inspect",
                "project_info",
                "list_phases",
                "list_materials",
                "set_property",
                "call_method",
                "new_project",
                "open_project",
                "close_project",
                "recover_project",
                "save_project",
                "create_phase",
                "set_current_phase",
                "activate",
                "deactivate",
                "calculate",
                "set_phase_property",
                "set_mode",
                "generate_mesh",
                "create_point",
                "create_line",
                "create_polygon",
                "create_borehole",
                "create_soillayer",
                "create_material",
                "assign_material",
                "create_structural_element",
                "list_objects",
                "model_state",
                "view_results",
            },
        )
        self.assertEqual(
            set(output_tools),
            {
                "connect",
                "disconnect",
                "connection_status",
                "list_members",
                "inspect",
                "project_info",
                "list_phases",
                "list_materials",
                "list_result_types",
                "get_results",
                "get_single_result",
            },
        )
        self.assertTrue(input_tools["inspect"].annotations.readOnlyHint)
        self.assertTrue(input_tools["set_property"].annotations.destructiveHint)
        self.assertTrue(input_tools["call_method"].annotations.openWorldHint)
        self.assertEqual(input_tools["connect"].inputSchema["properties"], {})
        self.assertTrue(all(tool.inputSchema["additionalProperties"] is False for tool in input_tools.values()))
        self.assertTrue(input_tools["close_project"].annotations.idempotentHint)
        self.assertTrue(input_tools["save_project"].annotations.idempotentHint)

    def test_role_resource_and_pinned_legacy_connect(self):
        server, session = self.make_server(PlaxisRole.INPUT)

        _, result = asyncio.run(server.call_tool("connect", {}))
        resource_uris = {str(resource.uri) for resource in asyncio.run(server.list_resources())}

        self.assertTrue(result["connected"])
        self.assertEqual(
            session.connect_kwargs,
            {
                "host": "127.0.0.1",
                "port": 10000,
                "password": "not-for-client",
                # Was 5.0. PLAXIS stays busy for several seconds after
                # new_project, and connects landing in that window failed
                # outright (WP-I).
                "timeout": 15.0,
                "request_timeout": 240.0,
            },
        )
        self.assertEqual(result["runtime_profile"], "test-profile")
        self.assertNotIn("password", result)
        self.assertEqual(resource_uris, {"plaxis://input/status", "plaxis://status"})

    def test_output_result_pagination_is_forwarded(self):
        server, session = self.make_server(PlaxisRole.OUTPUT)

        _, result = asyncio.run(
            server.call_tool(
                "get_results",
                {
                    "phase": 3,
                    "result_type_path": "ResultTypes.Soil.Ux",
                    "fem_type": "node",
                    "offset": 200,
                    "limit": 500,
                },
            )
        )

        self.assertEqual(result["operation"], "get_results")
        self.assertEqual(
            session.calls,
            [
                (
                    "get_results",
                    {
                        "phase": 3,
                        "result_type_path": "ResultTypes.Soil.Ux",
                        "fem_type": "node",
                        "smoothing": None,
                        "offset": 200,
                        "limit": 500,
                    },
                )
            ],
        )

    def test_role_specific_settings_win_over_generic_and_unsafe_host_fails(self):
        config = load_server_config(
            {
                "PLAXIS_ROLE": "output",
                "PLAXIS_HOST": "127.0.0.1",
                "PLAXIS_PORT": "19999",
                "PLAXIS_OUTPUT_PORT": "10001",
                "PLAXIS_PASSWORD": "generic",
                "PLAXIS_OUTPUT_PASSWORD": "specific",
            }
        )
        self.assertEqual(config.role, PlaxisRole.OUTPUT)
        self.assertEqual(config.port, 10001)
        self.assertEqual(config.password, "specific")

        with self.assertRaises(ServerConfigurationError):
            load_server_config({"PLAXIS_ROLE": "input", "PLAXIS_INPUT_HOST": "10.0.0.10"})
        with self.assertRaises(ServerConfigurationError):
            ServerConfig(role=PlaxisRole.INPUT, host="localhost")

    def test_worker_operation_failures_are_sanitized_mcp_tool_errors(self):
        class FailingBridgeSession:
            def status(self):
                return {"state": "connected", "connected": True}

            def invoke(self, operation, **params):
                raise WorkerOperationError("WRONG_ROLE", "safe worker message", state="connected")

        server = create_server(
            ServerConfig(role=PlaxisRole.OUTPUT),
            session_factory=FailingBridgeSession,
        )
        result = asyncio.run(server.call_tool("list_phases", {}))

        self.assertTrue(result.isError)
        self.assertEqual(result.structuredContent["code"], "WRONG_ROLE")
        self.assertEqual(result.structuredContent["state"], "connected")

    def test_error_handler_binds_each_tools_own_function_not_a_shared_closure(self):
        """_install_bridge_error_handlers wraps every tool's fn in the same
        loop. A late-binding closure bug there (capturing the loop variable
        itself rather than its value at each iteration) would make every
        wrapped tool call whichever tool was defined last -- worth a direct
        check now that the Input role alone wraps 34 tools, not a handful."""

        class SelectiveFailureSession:
            def status(self):
                return {"state": "connected", "connected": True}

            def invoke(self, operation, **params):
                # Every mutator this session receives fails with an error
                # naming the operation that was actually called.
                raise WorkerOperationError("PLAXIS_ERROR", "failed:" + operation, state="connected")

        server = create_server(
            ServerConfig(role=PlaxisRole.INPUT),
            session_factory=SelectiveFailureSession,
        )

        activate_result = asyncio.run(
            server.call_tool("activate", {"paths": ["Line_1"], "phases": ["Phase_1"]})
        )
        calculate_result = asyncio.run(server.call_tool("calculate", {}))

        self.assertTrue(activate_result.isError)
        self.assertEqual(activate_result.structuredContent["message"], "failed:activate")
        self.assertTrue(calculate_result.isError)
        self.assertEqual(calculate_result.structuredContent["message"], "failed:calculate")

    def test_generated_profile_uses_credential_store_not_environment(self):
        installation_root = Path(sys.executable).parent
        profile = EndpointProfile(
            role="input",
            runtime_profile="legacy-38",
            worker_python=Path(sys.executable),
            installation_root=installation_root,
            host="127.0.0.1",
            port=10000,
            credential_target=credential_target_for(installation_root, "input"),
        )

        class Store:
            def read(self, target):
                self.target = target
                return "credential-manager-only"

        store = Store()
        config = server_config_from_profile(profile, credential_store=store)

        self.assertEqual(store.target, profile.credential_target)
        self.assertEqual(config.password, "credential-manager-only")
        self.assertEqual(config.runtime_profile, "legacy-38")
        self.assertNotIn("credential-manager-only", repr(config))

    def test_serve_profile_rejects_endpoint_environment_overrides(self):
        with patch.dict("os.environ", {"PLAXIS_ROLE": "input"}, clear=True):
            with self.assertRaises(ServerConfigurationError):
                main(["serve", "--config", "unused.toml"])

    def test_cli_delegates_subcommands_with_option_arguments(self):
        """The packaged exe must pass option-like args through to the delegates.

        A subparser using nargs=REMAINDER silently failed on the very form the
        installer uses, so `plaxis-mcp profiles --output-dir X` aborted with
        "unrecognized arguments" instead of delegating.
        """

        with patch.dict("os.environ", {}, clear=True):
            with patch("plaxis_mcp.server.profiles_main", return_value=0) as profiles:
                self.assertEqual(main(["profiles", "--output-dir", r"C:\tmp\p"]), 0)
            profiles.assert_called_once_with(["--output-dir", r"C:\tmp\p"])

            with patch("plaxis_mcp.server.credentials_main", return_value=0) as credentials:
                self.assertEqual(main(["credentials", "set", "--profile", r"C:\tmp\i.toml"]), 0)
            credentials.assert_called_once_with(["set", "--profile", r"C:\tmp\i.toml"])

            with patch("plaxis_mcp.server.setup_main", return_value=0) as setup:
                self.assertEqual(main(["setup", "--accept-legacy-37"]), 0)
            setup.assert_called_once_with(["--accept-legacy-37"])

    def test_version_reports_the_package_version_without_serving(self):
        # The Caros packaging preflight reads this to catch a stale staged
        # bundle, so it must work even where the transport is misconfigured.
        with patch.dict("os.environ", {"PLAXIS_MCP_TRANSPORT": "sse"}, clear=True):
            with patch("builtins.print") as printed:
                self.assertEqual(main(["--version"]), 0)
        printed.assert_called_once_with("plaxis-mcp %s" % __version__)

    def test_serve_role_resolves_the_profile_from_the_profiles_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            profile_path = Path(directory) / "input.toml"
            profile_path.write_text("", encoding="utf-8")

            with patch.dict("os.environ", {}, clear=True):
                with patch("plaxis_mcp.server.load_profile") as load:
                    with patch("plaxis_mcp.server.server_config_from_profile") as build:
                        with patch("plaxis_mcp.server._run_stdio", return_value=0) as run:
                            self.assertEqual(
                                main(["serve", "--role", "input", "--profile-dir", directory]), 0
                            )
            load.assert_called_once_with(profile_path)
            build.assert_called_once_with(load.return_value)
            run.assert_called_once_with(build.return_value)

    def test_serve_role_defaults_to_the_shared_profiles_directory(self):
        with tempfile.TemporaryDirectory() as local_app_data:
            expected = default_profile_directory({"LOCALAPPDATA": local_app_data}) / "output.toml"
            expected.parent.mkdir(parents=True)
            expected.write_text("", encoding="utf-8")

            with patch.dict("os.environ", {"LOCALAPPDATA": local_app_data}, clear=True):
                with patch("plaxis_mcp.server.load_profile") as load:
                    with patch("plaxis_mcp.server.server_config_from_profile"):
                        with patch("plaxis_mcp.server._run_stdio", return_value=0):
                            self.assertEqual(main(["serve", "--role", "output"]), 0)
            load.assert_called_once_with(expected)

    def test_serve_role_without_a_generated_profile_names_the_setup_command(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict("os.environ", {}, clear=True):
                with self.assertRaises(ServerConfigurationError) as raised:
                    main(["serve", "--role", "input", "--profile-dir", directory])
        self.assertIn("plaxis-mcp setup", str(raised.exception))

    def test_serve_requires_exactly_one_profile_source(self):
        for arguments in (
            ["serve"],
            ["serve", "--config", "i.toml", "--role", "input"],
            ["serve", "--config", "i.toml", "--profile-dir", "."],
        ):
            with patch.dict("os.environ", {}, clear=True):
                with self.assertRaises(SystemExit):
                    main(arguments)

    def test_tool_arguments_are_strict_not_coerced(self):
        server, _ = self.make_server(PlaxisRole.OUTPUT)

        for arguments in (
            {"phase": True, "result_type_path": "ResultTypes.Soil.Ux"},
            {"phase": 1, "result_type_path": "ResultTypes.Soil.Ux", "offset": "1"},
            {"phase": 1, "result_type_path": "ResultTypes.Soil.Ux", "limit": "2"},
            {"phase": 1, "result_type_path": "ResultTypes.Soil.Ux", "unrecognized": True},
        ):
            with self.assertRaises(ToolError):
                asyncio.run(server.call_tool("get_results", arguments))


if __name__ == "__main__":
    unittest.main()
