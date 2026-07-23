[CmdletBinding()]
param(
    [string]$CarosDesktopDir = "..\..\caros\caros\ui\desktop",
    [string]$CertificateThumbprint = "3B069415B5234C8F2CCF981396FC97AB37CFAE0C",
    [switch]$SkipBuild
)

# Build the signed one-directory artifact and stage it where electron-forge
# picks it up (extraResource: ['src/bin', ...] -> resources\bin\plaxis-mcp).
# Verification is delegated to the Caros-side preflight so the staged bundle is
# checked by exactly the same code that gates `pnpm run package`.

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Desktop = [IO.Path]::GetFullPath((Join-Path $Root $CarosDesktopDir))
$Artifact = Join-Path $Root "dist\plaxis-mcp"
$Staged = Join-Path $Desktop "src\bin\plaxis-mcp"
$Preflight = Join-Path $Desktop "scripts\verify-plaxis-mcp.js"

if (-not (Test-Path -LiteralPath $Preflight -PathType Leaf)) {
    throw "The Caros preflight was not found: $Preflight. Check -CarosDesktopDir."
}

if (-not $SkipBuild) {
    & (Join-Path $PSScriptRoot "build_windows.ps1") -CertificateThumbprint $CertificateThumbprint
    if ($LASTEXITCODE -ne 0) {
        throw "build_windows.ps1 failed with exit code $LASTEXITCODE"
    }
}

if (-not (Test-Path -LiteralPath (Join-Path $Artifact "plaxis-mcp.exe") -PathType Leaf)) {
    throw "No built artifact at $Artifact. Run without -SkipBuild."
}

# Replace rather than merge: a stale file left behind from an earlier build
# would fail the manifest's no-extra-files check at package time, and the
# manifest is exhaustive by construction.
if (Test-Path -LiteralPath $Staged) {
    Remove-Item -LiteralPath $Staged -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $Staged | Out-Null
Copy-Item -Path (Join-Path $Artifact "*") -Destination $Staged -Recurse -Force

Push-Location $Desktop
try {
    $Version = & node $Preflight
    if ($LASTEXITCODE -ne 0) {
        throw "The Caros PLAXIS-MCP preflight rejected the staged bundle."
    }
} finally {
    Pop-Location
}

Write-Output "Staged $Version for Caros at $Staged"
