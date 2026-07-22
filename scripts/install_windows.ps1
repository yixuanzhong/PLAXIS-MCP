[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$SourceDir,
    [string]$InstallationRoot,
    [switch]$AcceptLegacy37,
    [switch]$DevelopmentUnsigned,
    [string]$Destination = (Join-Path $env:LOCALAPPDATA "Caros\PLAXIS-MCP"),
    [string]$InputHost = "127.0.0.1",
    [ValidateRange(1, 65535)]
    [int]$InputPort = 10000,
    [string]$OutputHost = "127.0.0.1",
    [ValidateRange(1, 65535)]
    [int]$OutputPort = 10001
)

$ErrorActionPreference = "Stop"
$ResolvedSource = (Resolve-Path -LiteralPath $SourceDir).Path
if (-not (Test-Path -LiteralPath (Join-Path $ResolvedSource "plaxis-mcp.exe") -PathType Leaf)) {
    throw "SourceDir must be a PyInstaller one-directory PLAXIS-MCP artifact."
}
$ManifestPath = Join-Path $ResolvedSource "artifact-manifest.json"
if (-not (Test-Path -LiteralPath $ManifestPath -PathType Leaf)) {
    throw "SourceDir is missing its required artifact-manifest.json."
}
try {
    $Manifest = Get-Content -LiteralPath $ManifestPath -Raw | ConvertFrom-Json
} catch {
    throw "artifact-manifest.json is invalid."
}
if ($null -eq $Manifest.files -or $Manifest.files.Count -eq 0 -or $null -eq $Manifest.vendor_redistribution_check -or -not $Manifest.vendor_redistribution_check.passed) {
    throw "artifact-manifest.json did not pass its vendor redistribution gate."
}
$ExpectedPaths = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::OrdinalIgnoreCase)
foreach ($Entry in $Manifest.files) {
    $Relative = [string]$Entry.path
    if ([System.IO.Path]::IsPathRooted($Relative) -or $Relative -match '(^|[\\/])\.\.([\\/]|$)') {
        throw "artifact-manifest.json contains an unsafe path."
    }
    $ArtifactFile = Join-Path $ResolvedSource $Relative
    if (-not (Test-Path -LiteralPath $ArtifactFile -PathType Leaf)) {
        throw "Artifact manifest references a missing file: $Relative"
    }
    $ActualHash = (Get-FileHash -LiteralPath $ArtifactFile -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($ActualHash -ne ([string]$Entry.sha256).ToLowerInvariant()) {
        throw "Artifact hash mismatch: $Relative"
    }
    [void]$ExpectedPaths.Add($Relative)
}

# Hashing only the listed files proves nothing about files that are NOT listed.
# A PyInstaller one-directory bundle loads its DLLs from this directory, so an
# unlisted binary planted here would be copied to the install location and
# loaded by the signed executable while every listed hash still matched. The
# manifest is exhaustive by construction (write_artifact_manifest.py walks the
# tree with rglob), so anything extra is unexpected by definition.
# Manifest paths are forward-slash relative (Path.as_posix) and the manifest
# excludes itself, so normalise separators and skip it here too.
$ManifestName = Split-Path -Leaf $ManifestPath
foreach ($Present in Get-ChildItem -LiteralPath $ResolvedSource -Recurse -File -Force) {
    $Relative = $Present.FullName.Substring($ResolvedSource.Length).TrimStart('\', '/').Replace('\', '/')
    if ($Relative -eq $ManifestName) {
        continue
    }
    if (-not $ExpectedPaths.Contains($Relative)) {
        throw "Artifact contains a file absent from artifact-manifest.json: $Relative"
    }
}
if (-not $DevelopmentUnsigned) {
    $Signature = Get-AuthenticodeSignature -LiteralPath (Join-Path $ResolvedSource "plaxis-mcp.exe")
    if ([string]$Signature.Status -ne "Valid") {
        throw "Production PLAXIS-MCP executable must have a valid Authenticode signature. Use -DevelopmentUnsigned only for an explicitly non-production build."
    }
}
foreach ($endpointHost in @($InputHost, $OutputHost)) {
    if ($endpointHost.ToLowerInvariant() -notin @("127.0.0.1", "::1")) {
        throw "PLAXIS endpoints must remain loopback-only."
    }
}

$DestinationParent = Split-Path -Parent $Destination
New-Item -ItemType Directory -Force -Path $DestinationParent | Out-Null
if (Test-Path -LiteralPath $Destination) {
    $Backup = "$Destination.backup-$(Get-Date -Format yyyyMMdd-HHmmss)"
    Move-Item -LiteralPath $Destination -Destination $Backup
    Write-Output "Existing installation moved to $Backup"
}
New-Item -ItemType Directory -Force -Path $Destination | Out-Null
Get-ChildItem -LiteralPath $ResolvedSource -Force | Copy-Item -Destination $Destination -Recurse -Force

$Profiles = Join-Path $Destination "profiles"
New-Item -ItemType Directory -Force -Path $Profiles | Out-Null

$Executable = Join-Path $Destination "plaxis-mcp.exe"
$ProfileArgs = @(
    "profiles", "--output-dir", $Profiles,
    "--input-host", $InputHost, "--input-port", $InputPort,
    "--output-host", $OutputHost, "--output-port", $OutputPort
)
if (-not [string]::IsNullOrWhiteSpace($InstallationRoot)) {
    $ProfileArgs += @("--installation-root", (Resolve-Path -LiteralPath $InstallationRoot).Path)
}
if ($AcceptLegacy37) {
    $ProfileArgs += "--accept-legacy-37"
}
& $Executable @ProfileArgs
if ($LASTEXITCODE -ne 0) {
    throw "PLAXIS installation discovery/profile generation failed. If several supported installations exist, rerun with -InstallationRoot."
}

Write-Output "Installed PLAXIS-MCP at $Destination"
Write-Output "Set passwords with $Executable credentials set --profile $Profiles\input.toml and ...\output.toml"
