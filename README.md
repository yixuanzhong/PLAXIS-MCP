# plaxis-mcp

`plaxis-mcp` is a Model Context Protocol server for interacting with a running
PLAXIS Remote Scripting session.

It uses the PLAXIS Python scripting interface shape published in
[`cemsbv/plxscripting`](https://github.com/cemsbv/plxscripting) and exposes a
small set of generic MCP tools for connecting to PLAXIS, inspecting objects,
reading values, setting properties, and invoking remote methods.

## Features

- Connect to a running PLAXIS Remote Scripting server.
- Create, open, close, recover, and save PLAXIS projects.
- Summarize phases, materials, and common project metadata.
- Inspect global and nested PLAXIS objects through path-based access.
- Read dynamic property values in a JSON-friendly form.
- Set writable properties.
- Call remote methods with positional arguments.
- Expose connection state as both a resource and MCP tools.

## Requirements

- Python 3.10+
- A running PLAXIS instance with Remote Scripting enabled
- Access to the `plxscripting` package

You can provide `plxscripting` in either of these ways:

1. Install the published package dependency from this project.
2. Point `PLAXIS_SCRIPTING_PATH` at the PLAXIS-installed scripting location,
   such as:

```text
C:\ProgramData\Seequent\PLAXIS Python Distribution V2\python\Lib\site-packages
```

That path pattern comes from the upstream `plxscripting` README.

## Installation

```bash
pip install -e .
```

Or build a container image:

```bash
docker build -t plaxis-mcp .
```

## Running

By default the server uses stdio transport:

```bash
python -m plaxis_mcp.server
```

With Docker:

```bash
docker run -i --rm \
  -e PLAXIS_HOST=host.docker.internal \
  -e PLAXIS_PORT=10000 \
  plaxis-mcp
```

Note: the container only runs the MCP server. PLAXIS still needs to run on the
Windows host with Remote Scripting enabled and reachable from the container.

Optional environment variables:

- `PLAXIS_HOST` default: `127.0.0.1`
- `PLAXIS_PORT` default: `10000`
- `PLAXIS_PASSWORD` default: empty
- `PLAXIS_TIMEOUT` default: `5.0`
- `PLAXIS_REQUEST_TIMEOUT` optional per-request timeout
- `PLAXIS_SCRIPTING_PATH` optional extra import path for PLAXIS scripting files
- `PLAXIS_MCP_TRANSPORT` default: `stdio`

## MCP Tools

- `connect`
- `disconnect`
- `connection_status`
- `new_project`
- `open_project`
- `close_project`
- `recover_project`
- `save_project`
- `project_info`
- `list_phases`
- `list_materials`
- `list_members`
- `inspect`
- `set_property`
- `call_method`

## Path Syntax

Paths use dotted access with optional list indexes:

- `Phases[0]`
- `Soils[1].Material`
- `Soil_1.Material.Name`

An empty path refers to the PLAXIS global object.

## Example MCP Client Config

See [examples/mcp-client-config.json](examples/mcp-client-config.json) for a
copy-pasteable client config covering both direct Python and Docker usage.

```json
{
  "mcpServers": {
    "plaxis-mcp": {
      "command": "python",
      "args": ["-m", "plaxis_mcp.server"],
      "env": {
        "PLAXIS_HOST": "127.0.0.1",
        "PLAXIS_PORT": "10000",
        "PLAXIS_PASSWORD": ""
      }
    }
  }
}
```

## Development

Run the unit tests:

```bash
python -m unittest discover -s tests
```

## References

- [`cemsbv/plxscripting`](https://github.com/cemsbv/plxscripting)
- [official MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk)
