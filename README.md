# PLAXIS-MCP

`plaxis-mcp` is a Windows-local, stdio Model Context Protocol (MCP) server for
PLAXIS Remote Scripting. It is client-neutral: Caros, Codex, Claude Code,
Cursor, VS Code/Copilot, and other conforming local stdio MCP clients connect
to the same unprefixed server tools.

## Architecture

PLAXIS-MCP separates the MCP host from PLAXIS's vendor-managed Python runtime:

- The host runs on CPython 3.13 with `mcp==1.26.0`.
- A role-pinned worker runs with the selected PLAXIS installation's bundled
  Python and imports its matching Bentley/Seequent scripting packages.
- The host and worker use a private local pipe. Client traffic is always MCP
  over stdio; normal logs and diagnostics go to stderr.
- PLAXIS-MCP never installs packages into, upgrades, or redistributes the
  PLAXIS Python bundle.

This split is required because the MCP host needs a current Python runtime,
while PLAXIS ships version-specific scripting environments.

| PLAXIS generation | Bundled Python | Runtime profile | Release status |
| --- | --- | --- | --- |
| PLAXIS 2024.2 and newer | 3.12.3 | `current-312` | Enable after live certification |
| PLAXIS CONNECT Edition V22 through early 2024 | 3.8.10 | `legacy-38` | Initial production target |
| PLAXIS CONNECT Edition V21 | 3.7.4 | `legacy-37` | Enable after live and security certification |

An unknown or uncertified PLAXIS/Python pairing fails closed. V21 is a legacy
profile: its end-of-life Python is isolated inside the loopback worker and
requires organizational security acceptance.

## Requirements

- Windows with a supported local PLAXIS 2D installation and Remote Scripting
  enabled.
- CPython 3.13 for the MCP host. The supported host range is `>=3.13,<3.14`.
- One PLAXIS Input server and/or one PLAXIS Output server listening only on
  loopback. The defaults are Input `127.0.0.1:10000` and Output
  `127.0.0.1:10001`.
- A selected, certified PLAXIS bundled-Python profile. Do not install
  `plxscripting` into the MCP host environment.

Historical PLAXIS scripting paths may be under
`C:\ProgramData\Seequent\PLAXIS Python Distribution V2\python\Lib\site-packages`
or, for older installs,
`C:\ProgramData\Bentley\Geotechnical\PLAXIS Python Distribution V2\python\Lib\site-packages`.
They belong to the worker only; do not add either path to the host's
`PYTHONPATH`.

Docker, WSL, remote MCP transports, and PLAXIS bundles copied into a Python
environment are not supported deployment paths for v0.2.0. The former Docker
artifacts have intentionally been removed.

## Install and run

The supported deployment is the signed package. Install it, generate the two
profiles, then store each role password in Windows Credential Manager:

```powershell
plaxis-mcp.exe profiles --output-dir "$env:LOCALAPPDATA\Caros\PLAXIS-MCP\profiles"
plaxis-mcp.exe credentials set --profile "$env:LOCALAPPDATA\Caros\PLAXIS-MCP\profiles\input.toml"
plaxis-mcp.exe credentials set --profile "$env:LOCALAPPDATA\Caros\PLAXIS-MCP\profiles\output.toml"
```

`credentials set` reads the password with a hidden prompt. It has no password
argument, so the value never reaches command history, agent configuration, or
logs.

Run each role as a separate process. The host never connects at startup; use
the `connect` tool after PLAXIS is running.

```powershell
plaxis-mcp.exe serve --config "$env:LOCALAPPDATA\Caros\PLAXIS-MCP\profiles\input.toml"
plaxis-mcp.exe serve --config "$env:LOCALAPPDATA\Caros\PLAXIS-MCP\profiles\output.toml"
```

`serve --config` takes every endpoint setting from the profile and the password
from Windows Credential Manager. It **fails to start** if any `PLAXIS_*`
endpoint environment override is present, so a client cannot silently redirect
an endpoint or inject a password. See `examples/` for each certified client.

A profile is bound to one installation: `credential_target` is derived from
`installation_root`, and `worker_python` must be an interpreter that discovery
links to that same root. Editing any of the three by hand makes the profile
fail to load. This is what stops a profile from handing a stored PLAXIS
password to an arbitrary executable.

### Source/developer launch only

A clean CPython 3.13 environment can run the module directly. This path is
**not** supported for deployment: it carries the password in the environment
and performs no installation binding.

```powershell
py -3.13 -m pip install .
$env:PLAXIS_ROLE = "input"
$env:PLAXIS_INPUT_HOST = "127.0.0.1"
$env:PLAXIS_INPUT_PORT = "10000"
$env:PLAXIS_BUNDLE_PYTHON = "C:\Path\To\PLAXIS\python.exe"
py -3.13 -I -u -m plaxis_mcp.server
```

For Output, set `PLAXIS_ROLE=output` and use the `PLAXIS_OUTPUT_*` variables.
Role-specific variables take precedence over the deprecated generic
`PLAXIS_HOST`, `PLAXIS_PORT`, and `PLAXIS_PASSWORD` fallback.

The server accepts stdio only. Do not set a non-stdio `PLAXIS_MCP_TRANSPORT`.

## Roles and tools

Each process has one immutable role. A client must register Input and Output as
separate MCP servers when it needs both.

| Role | Default endpoint | Tools |
| --- | --- | --- |
| Input | `127.0.0.1:10000` | `connect`, `disconnect`, `connection_status`, `list_members`, `inspect`, `project_info`, `list_phases`, `list_materials`, `set_property`, `call_method`, `new_project`, `open_project`, `close_project`, `recover_project`, `save_project` |
| Output | `127.0.0.1:10001` | `connect`, `disconnect`, `connection_status`, `list_members`, `inspect`, `project_info`, `list_phases`, `list_materials`, `list_result_types`, `get_results` |

`connect()` has no endpoint or credential arguments. It uses only the pinned
role configuration, so an agent cannot redirect a stored password to an
arbitrary host. The server exposes role-specific status resources at
`plaxis://input/status` and `plaxis://output/status`.

`get_results(phase, result_type_path, fem_type="node", offset=0, limit=200)`
supports lossless pagination. `limit` is between 1 and 5,000 and results report
`count`, `offset`, `limit`, `returned_count`, `has_more`, `next_offset`, and
`results`.

## Client configuration

All shipped examples are secret-free and use two server entries. Replace the
Python command placeholder with the absolute path to the approved CPython 3.13
host runtime. Never add a PLAXIS password to version control, command-line
arguments, logs, or a profile file.

| Client | Example |
| --- | --- |
| Caros | [caros-extensions.yaml](examples/caros-extensions.yaml) |
| Codex | [codex-config.toml](examples/codex-config.toml) |
| Claude Code | [claude-code-mcp.json](examples/claude-code-mcp.json) |
| Cursor | [cursor-mcp.json](examples/cursor-mcp.json) |
| VS Code / Copilot | [vscode-mcp.json](examples/vscode-mcp.json) |

The generic [mcp-client-config.json](examples/mcp-client-config.json) is a
minimal `mcpServers` example for clients that use that conventional JSON shape.

### Credentials and approvals

- Caros: place each password in its keyring-backed `env_keys` entry and run
  Caros with `GOOSE_MODE=smart_approve`. Keep every mutator at Ask Before.
- Codex: use `env_vars` to forward the locally stored password and configure
  `default_tools_approval_mode = "writes"`.
- Claude Code: expand only a user-level environment variable in `.mcp.json`
  and retain server trust/tool approval prompts.
- Cursor: start Cursor with the role password in its user environment and keep
  Auto-run off.
- VS Code: use a password `inputs` entry; VS Code stores the entered value
  securely for reuse. Keep Default Approvals, not Bypass or Autopilot.

All clients should ask before `call_method` and project/file mutators. Do not
choose an Always Allow-style rule for `call_method`, `open_project`, or
`save_project`: client approval is generally tool-wide, not argument-scoped.

Absolute local and UNC `.p2dx` paths accessible to PLAXIS are allowed only
after user approval. Client approvals and Caros checkpoints cannot restore
in-memory PLAXIS state; use disposable projects and backups for all mutation
testing.

## Development and verification

Run the local unit suite from the CPython 3.13 host environment:

```powershell
py -3.13 -m unittest discover -s tests
```

Inspect the split host/worker configuration without starting PLAXIS:

```powershell
py -3.13 scripts/smoke_test.py
```

Before a release, verify the host wheel/installer in a clean environment,
confirm `pip check`, scan the artifact for vendor files, validate the MCP
contract at protocol `2025-03-26`, and complete live certification with a
disposable calculated project for every enabled PLAXIS profile.

## References

- [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk)
- [Codex MCP configuration](https://learn.chatgpt.com/docs/extend/mcp)
- [Claude Code MCP configuration](https://code.claude.com/docs/en/mcp)
- [Cursor MCP configuration](https://docs.cursor.com/context/model-context-protocol)
- [VS Code MCP configuration](https://code.visualstudio.com/docs/agents/reference/mcp-configuration)

## Trademark notice

PLAXIS is a trademark of Seequent and related companies. This independent
integration is not affiliated with, endorsed by, or sponsored by Seequent or
Bentley Systems.
