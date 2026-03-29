from __future__ import annotations

import os
from typing import Any

from mcp.server.fastmcp import FastMCP

from .core import PlaxisSession


mcp = FastMCP("plaxis-mcp", json_response=True)
session = PlaxisSession()


@mcp.resource("plaxis://status")
def plaxis_status_resource() -> dict[str, Any]:
    """Return current connection state for the PLAXIS session."""
    return session.status()


@mcp.tool()
def connect(
    host: str | None = None,
    port: int | None = None,
    password: str | None = None,
    timeout: float | None = None,
    request_timeout: float | None = None,
) -> dict[str, Any]:
    """Connect to a running PLAXIS Remote Scripting server."""
    return session.connect(
        host=host,
        port=port,
        password=password,
        timeout=timeout,
        request_timeout=request_timeout,
    )


@mcp.tool()
def disconnect() -> dict[str, Any]:
    """Clear the current PLAXIS session held by this MCP server."""
    return session.disconnect()


@mcp.tool()
def connection_status() -> dict[str, Any]:
    """Get the current PLAXIS connection status."""
    return session.status()


@mcp.tool()
def list_members(path: str = "") -> dict[str, Any]:
    """List available attributes on a PLAXIS object path."""
    return session.list_members(path)


@mcp.tool()
def inspect(path: str = "") -> dict[str, Any]:
    """Inspect a PLAXIS object or property and serialize it for MCP clients."""
    return session.inspect(path)


@mcp.tool()
def set_property(path: str, value: Any) -> dict[str, Any]:
    """Set a writable PLAXIS property using a dotted path."""
    return session.set_property(path, value)


@mcp.tool()
def call_method(path: str, method: str, args: list[Any] | None = None) -> dict[str, Any]:
    """Call a remote PLAXIS method with positional arguments."""
    return session.call_method(path, method, args)


@mcp.tool()
def new_project() -> dict[str, Any]:
    """Create a new PLAXIS project."""
    return session.new_project()


@mcp.tool()
def open_project(filename: str) -> dict[str, Any]:
    """Open a PLAXIS project file."""
    return session.open_project(filename)


@mcp.tool()
def close_project() -> dict[str, Any]:
    """Close the active PLAXIS project."""
    return session.close_project()


@mcp.tool()
def recover_project() -> dict[str, Any]:
    """Recover the current PLAXIS project."""
    return session.recover_project()


@mcp.tool()
def save_project(filename: str | None = None) -> dict[str, Any]:
    """Save the active PLAXIS project, or save it to a new path when a filename is supplied."""
    return session.save_project(filename)


@mcp.tool()
def list_phases() -> dict[str, Any]:
    """Return a compact summary of model phases."""
    return session.list_phases()


@mcp.tool()
def list_materials() -> dict[str, Any]:
    """Return a compact summary of model materials."""
    return session.list_materials()


@mcp.tool()
def project_info() -> dict[str, Any]:
    """Return a compact summary of common project metadata and units."""
    return session.project_info()


def main() -> None:
    transport = os.getenv("PLAXIS_MCP_TRANSPORT", "stdio")
    mcp.run(transport=transport)


if __name__ == "__main__":
    main()
