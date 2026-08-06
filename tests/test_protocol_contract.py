"""Protocol-level checks using the pinned MCP SDK's in-memory transport."""

import anyio
import unittest

from mcp import types
from mcp.client.session import ClientSession
from mcp.shared.memory import create_client_server_memory_streams
from mcp_types.version import (
    HANDSHAKE_PROTOCOL_VERSIONS,
    LATEST_HANDSHAKE_VERSION,
    SUPPORTED_PROTOCOL_VERSIONS,
)

from plaxis_mcp.server import PlaxisRole, ServerConfig, create_server


class ReadOnlySession:
    def status(self):
        return {"state": "disconnected", "connected": False}


async def _exercise_protocol(protocol_version, expected_negotiated):
    server = create_server(
        ServerConfig(role=PlaxisRole.OUTPUT),
        session_factory=ReadOnlySession,
    )
    async with create_client_server_memory_streams() as (client_streams, server_streams):
        client_read, client_write = client_streams
        server_read, server_write = server_streams
        async with anyio.create_task_group() as task_group:
            # mcp 2.0 renamed FastMCP's private low-level server from
            # `_mcp_server` to `_lowlevel_server`.
            task_group.start_soon(
                server._lowlevel_server.run,
                server_read,
                server_write,
                server._lowlevel_server.create_initialization_options(),
            )
            async with ClientSession(client_read, client_write) as client:
                # mcp 2.0 send_request takes the request object directly; the
                # `ClientRequest`/`ClientNotification` wrappers are now bare
                # unions and no longer callable.
                initialized = await client.send_request(
                    types.InitializeRequest(
                        params=types.InitializeRequestParams(
                            protocol_version=protocol_version,
                            capabilities=types.ClientCapabilities(),
                            clientInfo=types.Implementation(
                                name="contract-client",
                                version="1.0",
                            ),
                        )
                    ),
                    types.InitializeResult,
                )
                await client.send_notification(types.InitializedNotification())
                await client.send_ping()
                tools = await client.list_tools()
                resources = await client.list_resources()
                status = await client.call_tool("connection_status", {})
                unknown = await client.call_tool("does_not_exist", {})
                invalid = await client.call_tool(
                    "get_results",
                    {"phase": 1, "result_type_path": "ResultTypes.Ux", "limit": 0},
                )

                # A handshake client that offers a modern (2026-07-28) version
                # is answered with the server's latest handshake version: the
                # 2026-07-28 stateless protocol is negotiated over Streamable
                # HTTP, never over the stdio/handshake path this server uses.
                assert initialized.protocol_version == expected_negotiated
                assert initialized.server_info.name == "plaxis-mcp"
                assert initialized.server_info.version == "0.4.0"
                assert {tool.name for tool in tools.tools} >= {
                    "connection_status",
                    "get_results",
                }
                assert {str(resource.uri) for resource in resources.resources} == {
                    "plaxis://output/status",
                    "plaxis://status",
                }
                assert not status.is_error
                assert unknown.is_error
                assert invalid.is_error
            task_group.cancel_scope.cancel()


class ProtocolContractTests(unittest.TestCase):
    def test_pinned_sdk_hosts_the_2026_07_28_edition(self):
        # mcp 2.0 exposes the version set from mcp_types.version (the old
        # mcp.shared.version module is gone) and now advertises the
        # 2026-07-28 edition alongside the legacy handshake revisions.
        self.assertEqual(
            tuple(SUPPORTED_PROTOCOL_VERSIONS),
            ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25", "2026-07-28"),
        )
        self.assertEqual(
            tuple(HANDSHAKE_PROTOCOL_VERSIONS),
            ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"),
        )
        self.assertEqual(LATEST_HANDSHAKE_VERSION, "2025-11-25")

    def test_handshake_versions_are_echoed_exactly(self):
        for protocol_version in HANDSHAKE_PROTOCOL_VERSIONS:
            anyio.run(_exercise_protocol, protocol_version, protocol_version)

    def test_modern_edition_negotiates_down_over_stdio_handshake(self):
        anyio.run(_exercise_protocol, "2026-07-28", LATEST_HANDSHAKE_VERSION)


if __name__ == "__main__":
    unittest.main()
