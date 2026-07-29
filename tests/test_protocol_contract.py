"""Protocol-level checks using the pinned MCP SDK's in-memory transport."""

import anyio
import unittest

from mcp import types
from mcp.client.session import ClientSession
from mcp.shared.memory import create_client_server_memory_streams
from mcp.shared.version import SUPPORTED_PROTOCOL_VERSIONS

from plaxis_mcp.server import PlaxisRole, ServerConfig, create_server


class ReadOnlySession:
    def status(self):
        return {"state": "disconnected", "connected": False}


async def _exercise_protocol(protocol_version):
    server = create_server(
        ServerConfig(role=PlaxisRole.OUTPUT),
        session_factory=ReadOnlySession,
    )
    async with create_client_server_memory_streams() as (client_streams, server_streams):
        client_read, client_write = client_streams
        server_read, server_write = server_streams
        async with anyio.create_task_group() as task_group:
            task_group.start_soon(
                server._mcp_server.run,
                server_read,
                server_write,
                server._mcp_server.create_initialization_options(),
            )
            async with ClientSession(client_read, client_write) as client:
                initialized = await client.send_request(
                    types.ClientRequest(
                        types.InitializeRequest(
                            params=types.InitializeRequestParams(
                                protocolVersion=protocol_version,
                                capabilities=types.ClientCapabilities(),
                                clientInfo=types.Implementation(
                                    name="contract-client",
                                    version="1.0",
                                ),
                            )
                        )
                    ),
                    types.InitializeResult,
                )
                await client.send_notification(
                    types.ClientNotification(types.InitializedNotification())
                )
                await client.send_ping()
                tools = await client.list_tools()
                resources = await client.list_resources()
                status = await client.call_tool("connection_status", {})
                unknown = await client.call_tool("does_not_exist", {})
                invalid = await client.call_tool(
                    "get_results",
                    {"phase": 1, "result_type_path": "ResultTypes.Ux", "limit": 0},
                )

                assert initialized.protocolVersion == protocol_version
                assert initialized.serverInfo.name == "plaxis-mcp"
                assert initialized.serverInfo.version == "0.3.5"
                assert {tool.name for tool in tools.tools} >= {
                    "connection_status",
                    "get_results",
                }
                assert {str(resource.uri) for resource in resources.resources} == {
                    "plaxis://output/status",
                    "plaxis://status",
                }
                assert not status.isError
                assert unknown.isError
                assert invalid.isError
            task_group.cancel_scope.cancel()


class ProtocolContractTests(unittest.TestCase):
    def test_pinned_sdk_protocol_matrix_including_caros(self):
        self.assertEqual(
            SUPPORTED_PROTOCOL_VERSIONS,
            ["2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"],
        )
        for protocol_version in SUPPORTED_PROTOCOL_VERSIONS:
            anyio.run(_exercise_protocol, protocol_version)


if __name__ == "__main__":
    unittest.main()
