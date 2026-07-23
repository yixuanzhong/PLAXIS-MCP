# Windows package layout

The release build creates a signed PyInstaller one-directory artifact:

```text
plaxis-mcp/
  plaxis-mcp.exe
  artifact-manifest.json
  licenses.json
  _internal/
    worker/worker.py
    worker/worker_runtime.py
    profiles/input.toml
    profiles/output.toml
    ... (Python runtime and dependencies)
```

PyInstaller 6 one-directory builds place every `datas` entry under `_internal/`,
not beside the executable. `sys._MEIPASS` points at `_internal/` in a frozen
build, so `BridgeSessionConfig.resolved_worker_script` finds `worker/worker.py`
there without any path assumption; the installed profiles are generated into
`profiles/` next to the executable, not read from the bundled templates.

`worker/` contains source only. It is started by the selected PLAXIS bundled
Python and must never be compiled against, copied from, or installed into a
Bentley/Seequent runtime.

The bundled `profiles/` templates are non-loadable placeholders and exist only
as documentation: their blank `worker_python` and `installation_root` fail the
profile binding check by design. Installation generates real role profiles with
the selected certified interpreter and a Credential Manager target.

`scripts/install_windows.ps1` calls the packaged `profiles` command. It
selects the sole registry-discovered certified installation automatically; if
more than one is found, the installer stops until `-InstallationRoot` names
one of those roots. V21/Python 3.7.4 additionally requires the installer
switch `-AcceptLegacy37`, which records the explicit organizational security
acceptance in both generated profiles.

Before replacing an existing installation, the installer verifies every
SHA-256 in `artifact-manifest.json`, the vendor-redistribution gate, and the
`plaxis-mcp.exe` Authenticode signature. `-DevelopmentUnsigned` is an
explicitly non-production exception to the signature check; it never skips
manifest validation.

Clients launch the same stable host path against a generated profile, named
either by role or by absolute path:

```powershell
& "$env:LOCALAPPDATA\Caros\PLAXIS-MCP\plaxis-mcp.exe" serve --role input
& "$env:LOCALAPPDATA\Caros\PLAXIS-MCP\plaxis-mcp.exe" serve --config `
  "$env:LOCALAPPDATA\Caros\PLAXIS-MCP\profiles\input.toml"
```

`--role` resolves the profiles directory that `install_windows.ps1` populates,
so a client configuration carries no per-account absolute path. Either form
must reject environment endpoint/password overrides.

A single artifact exposes hidden-input `setup`, `credentials` and `profiles`
subcommands by delegating to `plaxis_mcp.deployment`; source installs also
provide `plaxis-mcp-setup`, `plaxis-mcp-credentials` and `plaxis-mcp-profiles`
console scripts. `plaxis-mcp.exe --version` reports the package version without
starting a server, which is what the Caros packaging preflight reads.
