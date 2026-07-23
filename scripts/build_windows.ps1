[CmdletBinding()]
param(
    [string]$PythonExe = "py",
    [string[]]$PythonArgs = @("-3.13"),
    [string]$DistPath = "dist",
    [string]$WorkPath = "build\pyinstaller",
    [string]$CertificateThumbprint,
    [string]$TimestampUrl = "http://timestamp.digicert.com",
    # The Windows SDK is not on PATH by default, so accept its usual location
    # rather than failing the release build on an environment detail.
    [string]$SignTool = "C:\Program Files (x86)\Windows Kits\10\bin\10.0.22621.0\x64\signtool.exe",
    [switch]$DevelopmentUnsigned
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Spec = Join-Path $Root "packaging\plaxis-mcp.spec"
$BuildLock = Join-Path $Root "requirements-build.lock"
$OutRoot = Join-Path $Root $DistPath
$WorkRoot = Join-Path $Root $WorkPath

if (-not $DevelopmentUnsigned -and [string]::IsNullOrWhiteSpace($CertificateThumbprint)) {
    throw "A production artifact must be signed. Supply -CertificateThumbprint or use -DevelopmentUnsigned explicitly."
}

# --require-hashes makes pip refuse any artifact whose SHA-256 is not the one
# recorded in the lock, so a compromised or substituted wheel cannot enter a
# release build. It also forces every dependency to be pinned and hashed.
& $PythonExe @PythonArgs -m pip install --require-hashes --requirement $BuildLock
& $PythonExe @PythonArgs (Join-Path $PSScriptRoot "verify_dependencies.py") --requirements $BuildLock
& $PythonExe @PythonArgs -m PyInstaller --noconfirm --clean --distpath $OutRoot --workpath $WorkRoot $Spec

$Artifact = Join-Path $OutRoot "plaxis-mcp"
$Executable = Join-Path $Artifact "plaxis-mcp.exe"
if (-not (Test-Path -LiteralPath $Executable -PathType Leaf)) {
    throw "PyInstaller did not create $Executable"
}

if (-not $DevelopmentUnsigned) {
    if (-not (Test-Path -LiteralPath $SignTool -PathType Leaf)) {
        $SignTool = (Get-Command signtool.exe -ErrorAction Stop).Source
    }
    & $SignTool sign /fd SHA256 /sha1 $CertificateThumbprint /tr $TimestampUrl /td SHA256 $Executable
    if ($LASTEXITCODE -ne 0) { throw "signtool failed" }
    $Signature = Get-AuthenticodeSignature -LiteralPath $Executable
    if ([string]$Signature.Status -ne "Valid") {
        throw "The signed executable did not pass Authenticode verification."
    }
}

& $PythonExe @PythonArgs (Join-Path $PSScriptRoot "license_inventory.py") --output (Join-Path $Artifact "licenses.json")
& $PythonExe @PythonArgs (Join-Path $PSScriptRoot "write_artifact_manifest.py") --artifact-root $Artifact
Write-Output "Built $Artifact"
