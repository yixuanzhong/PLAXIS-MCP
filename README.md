<div align="center">

# PLAXIS **·** MCP

### Drive PLAXIS 2D from any MCP client.

Geometry, staged construction, meshing, calculation and results —
exposed as tools, over a hardened Windows-local stdio server.

<br>

[![release](https://img.shields.io/badge/release-v0.3.0-00E5A0?style=flat-square&labelColor=0D1117)](https://github.com/yixuanzhong/PLAXIS-MCP/releases)
[![python](https://img.shields.io/badge/host-CPython%203.13-3776AB?style=flat-square&logo=python&logoColor=white&labelColor=0D1117)](#requirements)
[![mcp](https://img.shields.io/badge/MCP-1.26.0-5A45FF?style=flat-square&labelColor=0D1117)](https://github.com/modelcontextprotocol/python-sdk)
[![transport](https://img.shields.io/badge/transport-stdio-8B949E?style=flat-square&labelColor=0D1117)](#security-model)
[![platform](https://img.shields.io/badge/platform-Windows-0078D4?style=flat-square&logo=windows&logoColor=white&labelColor=0D1117)](#requirements)
[![tools](https://img.shields.io/badge/tools-31%20input%20%C2%B7%2011%20output-00E5A0?style=flat-square&labelColor=0D1117)](#tools)
[![license](https://img.shields.io/badge/license-MIT-C9D1D9?style=flat-square&labelColor=0D1117)](LICENSE)

**[Quickstart](#quickstart)** · **[Tools](#tools)** · **[Clients](#clients)** · **[Security model](#security-model)** · **[Architecture](#architecture)**

`mcp-name: io.github.yixuanzhong/plaxis-mcp`

</div>

---

## ▸ v0.3.0 — set up once, launch from anywhere

| | |
| --- | --- |
| **One-shot machine setup** | `plaxis-mcp setup` discovers the PLAXIS installation, writes both role profiles, and stores each role password in Windows Credential Manager — with hidden prompts, no password argument. |
| **Shared profile store** | Every client on the machine reads the same profiles. Claude Code, Codex, Cursor and VS Code all bind to one installation and one credential set. |
| **`serve --role`** | No per-client config surgery. `--role input` / `--role output` resolves its own profile. |
| **Mode-aware operations** | `generate_mesh`, `create_phase` and `calculate` enter the PLAXIS mode they need, so a workflow never has to interleave `set_mode` calls. |
| **Listed on the MCP Registry** | Published as `io.github.yixuanzhong/plaxis-mcp`, installable with `uvx`. |

Client-neutral by design: Codex, Claude Code, Cursor, VS Code/Copilot and any other
conforming local stdio MCP client connect to the same **unprefixed** server tools.

---

## Quickstart

**1 — Set up the machine, once.**

```powershell
plaxis-mcp.exe setup
```

Profiles land in `%LOCALAPPDATA%\Caros\PLAXIS-MCP\profiles` (override with `--profile-dir`).
Add `--skip-credentials` to generate profiles now and store passwords later with
`plaxis-mcp.exe credentials set --profile <profile.toml>`.

> The directory name is historical — it is where the first supported installer wrote —
> and is kept because `credential_target` is derived from it.

**2 — Run each role as its own process.**

```powershell
plaxis-mcp.exe serve --role input
plaxis-mcp.exe serve --role output
```

`--config <profile.toml>` names a profile explicitly and is equivalent. Either way `serve`
takes every endpoint setting from the profile and the password from Windows Credential
Manager, and it **fails to start** if any `PLAXIS_*` endpoint environment override is
present — so a client cannot silently redirect an endpoint or inject a password.

**3 — Point a client at it.** See [Clients](#clients); the host never connects at startup,
so call the `connect` tool once PLAXIS is running.

### Getting the host

| Distribution | Command | Assurances |
| --- | --- | --- |
| **Signed Windows package** | `plaxis-mcp.exe` | Authenticode-signed, hash-manifested, installer-verified. **The supported production deployment.** |
| **PyPI** | `uvx plaxis-mcp serve --role input`<br>`py -3.13 -m pip install plaxis-mcp` | Ordinary Python source distribution. **No** code signature, **no** artifact manifest. |

Both expose the same `setup`, `serve`, `credentials` and `profiles` commands and enforce the
same profile binding and environment-override rejection — so PyPI is not a weaker *runtime*
posture. It simply carries no supply-chain attestation of its own beyond PyPI's, and it is not
what an organization requiring signed binaries should deploy.

Do not install `plxscripting` into the host environment under either.

<details>
<summary><b>Source / developer launch only</b> — not a supported deployment path</summary>

<br>

A clean CPython 3.13 environment can run the module directly with endpoint environment
variables. This path carries the password in the environment and performs **no** installation
binding.

```powershell
py -3.13 -m pip install .
$env:PLAXIS_ROLE = "input"
$env:PLAXIS_INPUT_HOST = "127.0.0.1"
$env:PLAXIS_INPUT_PORT = "10000"
$env:PLAXIS_BUNDLE_PYTHON = "C:\Path\To\PLAXIS\python.exe"
py -3.13 -I -u -m plaxis_mcp.server
```

For Output, set `PLAXIS_ROLE=output` and use the `PLAXIS_OUTPUT_*` variables. Role-specific
variables take precedence over the deprecated generic `PLAXIS_HOST`, `PLAXIS_PORT` and
`PLAXIS_PASSWORD` fallback. The server accepts stdio only — do not set a non-stdio
`PLAXIS_MCP_TRANSPORT`.

</details>

---

## Tools

Each process has **one immutable role**. A client registers Input and Output as separate MCP
servers when it needs both.

| Role | Endpoint | Tools |
| --- | --- | --- |
| **Both** | — | `connect` · `disconnect` · `connection_status` · `list_members` · `inspect` · `project_info` · `list_phases` · `list_materials` |
| **Input** | `127.0.0.1:10000` | `set_property` · `call_method` · `new_project` · `open_project` · `close_project` · `recover_project` · `save_project` · `create_phase` · `set_current_phase` · `set_phase_property` · `activate` · `deactivate` · `calculate` · `set_mode` · `generate_mesh` · `create_point` · `create_line` · `create_polygon` · `create_borehole` · `create_soillayer` · `create_material` · `assign_material` · `create_structural_element` |
| **Output** | `127.0.0.1:10001` | `list_result_types` · `get_results` · `get_single_result` |

**31 Input tools · 11 Output tools.**

- `connect()` takes no endpoint or credential arguments. It uses only the pinned role
  configuration, so an agent cannot redirect a stored password to an arbitrary host.
- Role status resources live at `plaxis://input/status` and `plaxis://output/status`.
- `get_results(phase, result_type_path, fem_type="node", offset=0, limit=200)` paginates
  losslessly. `limit` is 1–5,000; responses report `count`, `offset`, `limit`,
  `returned_count`, `has_more`, `next_offset` and `results`.

---

## Clients

All shipped examples are secret-free and use two server entries, one per role. Replace the
command with wherever `plaxis-mcp.exe` lives, or with `uvx plaxis-mcp` for a PyPI install.
Nothing else needs editing — `--role` finds the shared profiles by itself.

| Client | Example | Credentials & approvals |
| --- | --- | --- |
| **Codex** | [`codex-config.toml`](examples/codex-config.toml) | `env_vars` forwards the locally stored password; `default_tools_approval_mode = "writes"`. |
| **Claude Code** | [`claude-code-mcp.json`](examples/claude-code-mcp.json) | Expand only a user-level environment variable in `.mcp.json`; retain server trust and tool approval prompts. |
| **Cursor** | [`cursor-mcp.json`](examples/cursor-mcp.json) | Role password in the user environment at launch; keep Auto-run **off**. |
| **VS Code / Copilot** | [`vscode-mcp.json`](examples/vscode-mcp.json) | Use a password `inputs` entry — VS Code stores it securely for reuse. Keep Default Approvals, not Bypass or Autopilot. |

The generic [`mcp-client-config.json`](examples/mcp-client-config.json) is a minimal
`mcpServers` example for clients using that conventional JSON shape.

> **Never** add a PLAXIS password to version control, command-line arguments, logs, or a
> profile file.

---

## Security model

- **Profiles are bound to one installation.** `credential_target` is derived from
  `installation_root`, and `worker_python` must be an interpreter that discovery links to
  that same root. Editing any of the three by hand makes the profile fail to load. This is
  what stops a profile from handing a stored PLAXIS password to an arbitrary executable.
- **Environment overrides are rejected**, not merged: `serve` refuses to start when a
  `PLAXIS_*` endpoint variable is set.
- **Uncertified pairings fail closed.** The Python match is exact — a 3.7-series interpreter
  at any other patch level is a different, uncertified ABI, so it is rejected rather than
  assumed compatible.
- **Ask before mutation.** All clients should prompt before `call_method` and project/file
  mutators. Do not set an Always Allow-style rule for `call_method`, `open_project` or
  `save_project`: client approval is generally tool-wide, not argument-scoped.
- **Mutation is not undoable.** Absolute local and UNC `.p2dx` paths reachable by PLAXIS are
  allowed only after user approval. Client approvals and client-side checkpointing cannot
  restore in-memory PLAXIS state — use disposable projects and backups for all mutation
  testing.

---

## Architecture

PLAXIS-MCP separates the MCP host from PLAXIS's vendor-managed Python runtime:

```
  MCP client  ──stdio──▶  host (CPython 3.13, mcp 1.26.0)
                              │
                              ├─ private local pipe
                              ▼
                          role-pinned worker (PLAXIS bundled Python)
                              │
                              ▼
                          PLAXIS Remote Scripting @ 127.0.0.1
```

- Client traffic is always MCP over stdio; normal logs and diagnostics go to stderr.
- PLAXIS-MCP **never** installs packages into, upgrades, or redistributes the PLAXIS Python
  bundle.

The split is required because the MCP host needs a current Python runtime while PLAXIS ships
version-specific scripting environments.

| PLAXIS generation | Bundled Python | Runtime profile | Release status |
| --- | --- | --- | --- |
| PLAXIS 2024.2 and newer | 3.12.3 | `current-312` | Enable after live certification |
| PLAXIS CONNECT Edition V22 → early 2024 | 3.8.10 | `legacy-38` | **Initial production target** |
| PLAXIS CONNECT Edition V20 / V21 | 3.7.4 | `legacy-37` | Enable after live *and* security certification |

V20 and V21 share the legacy profile: their end-of-life Python is isolated inside the loopback
worker and requires organizational security acceptance.

### Requirements

- Windows, with a supported local PLAXIS 2D installation and Remote Scripting enabled.
- CPython 3.13 for the MCP host. Supported host range: `>=3.13,<3.14`.
- One PLAXIS Input and/or Output server listening **only** on loopback — defaults
  Input `127.0.0.1:10000`, Output `127.0.0.1:10001`.
- A selected, certified PLAXIS bundled-Python profile. Do not install `plxscripting` into the
  MCP host environment.

<details>
<summary>Historical PLAXIS scripting paths — worker-only, never on the host <code>PYTHONPATH</code></summary>

<br>

```
C:\ProgramData\Seequent\PLAXIS Python Distribution V2\python\Lib\site-packages
C:\ProgramData\Bentley\Geotechnical\PLAXIS Python Distribution V2\python\Lib\site-packages
```

</details>

Docker, WSL, remote MCP transports, and PLAXIS bundles copied into a Python environment are
**not** supported deployment paths for v0.3.0. The former Docker artifacts were removed
intentionally.

---

## Development & verification

```powershell
# unit suite, from the CPython 3.13 host environment
py -3.13 -m unittest discover -s tests

# inspect the split host/worker configuration without starting PLAXIS
py -3.13 scripts/smoke_test.py
```

Before a release: verify the host wheel/installer in a clean environment, confirm `pip check`,
scan the artifact for vendor files, validate the MCP contract at protocol `2025-03-26`, and
complete live certification with a disposable calculated project for **every** enabled PLAXIS
profile.

---

## References

[MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) ·
[Codex](https://learn.chatgpt.com/docs/extend/mcp) ·
[Claude Code](https://code.claude.com/docs/en/mcp) ·
[Cursor](https://docs.cursor.com/context/model-context-protocol) ·
[VS Code](https://code.visualstudio.com/docs/agents/reference/mcp-configuration)

---

<div align="center">
<sub>

MIT licensed. PLAXIS is a trademark of Seequent and related companies.<br>
This independent integration is not affiliated with, endorsed by, or sponsored by
Seequent or Bentley Systems.

</sub>
</div>
