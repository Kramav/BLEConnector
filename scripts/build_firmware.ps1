<#
.SYNOPSIS
    Builds OLA_Accel_BLE.ino.bin in Docker.

.DESCRIPTION
    Clones (or reuses) the SparkFun OpenLog_Artemis repo for its
    Firmware/Extras/UartPower3.zip core patch, stages this repo's sketch and
    Dockerfile into that Firmware/ directory, builds the image, and copies the
    compiled binary back out to build/OLA_Accel_BLE.ino.bin.

.PARAMETER OlaRepo
    Where to clone / find the OpenLog_Artemis checkout.
    Default: build\OpenLog_Artemis under this repo.

.PARAMETER Tag
    Docker image tag. Default: ola_accel_ble.

.PARAMETER NoCache
    Force a full rebuild, including the slow Apollo3 core install.

.EXAMPLE
    .\scripts\build_firmware.ps1
#>
[CmdletBinding()]
param(
    [string] $OlaRepo,
    [string] $Tag = 'ola_accel_ble',
    [switch] $NoCache
)

$ErrorActionPreference = 'Stop'

$repoRoot = Split-Path -Parent $PSScriptRoot
$buildDir = Join-Path $repoRoot 'build'
if (-not $OlaRepo) { $OlaRepo = Join-Path $buildDir 'OpenLog_Artemis' }

$container = 'ola_accel_container'
$binName   = 'OLA_Accel_BLE.ino.bin'

# Windows PowerShell 5.1 wraps a native command's stderr in ErrorRecords when
# its output is redirected, which under $ErrorActionPreference='Stop' aborts on
# perfectly normal output -- `docker build --progress=plain` writes everything
# to stderr. Run native tools with the preference relaxed and judge them by
# their exit code instead, which is the only thing that actually means failure.
function Invoke-Native {
    param(
        [Parameter(Mandatory)][string] $Exe,
        [string[]] $Arguments = @(),
        [switch] $Quiet
    )
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        if ($Quiet) {
            & $Exe @Arguments 2>&1 | Out-Null
        } else {
            & $Exe @Arguments 2>&1 | ForEach-Object { Write-Host $_ }
        }
    } finally {
        $ErrorActionPreference = $previous
    }
    return $LASTEXITCODE
}

function Invoke-NativeChecked {
    param(
        [Parameter(Mandatory)][string] $Exe,
        [string[]] $Arguments = @(),
        [switch] $Quiet,
        [string] $What
    )
    $code = Invoke-Native -Exe $Exe -Arguments $Arguments -Quiet:$Quiet
    if ($code -ne 0) {
        if (-not $What) { $What = "$Exe $($Arguments -join ' ')" }
        throw "$What failed (exit $code)"
    }
}

# --- prerequisites ----------------------------------------------------
foreach ($exe in @('docker', 'git')) {
    if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) {
        throw "$exe was not found on PATH. Install it and try again."
    }
}

if ((Invoke-Native -Exe 'docker' -Arguments @('info') -Quiet) -ne 0) {
    throw 'Docker is installed but its daemon is not responding. Start Docker Desktop, wait for the whale icon to settle, and retry.'
}

if (-not (Test-Path $buildDir)) { New-Item -ItemType Directory -Path $buildDir | Out-Null }

# --- OpenLog_Artemis checkout (we need Extras/UartPower3.zip) ----------
if (-not (Test-Path $OlaRepo)) {
    Write-Host "Cloning OpenLog_Artemis into $OlaRepo ..." -ForegroundColor Cyan
    Invoke-NativeChecked -Exe 'git' -Arguments @(
        'clone', '--depth', '1',
        'https://github.com/sparkfun/OpenLog_Artemis.git', $OlaRepo
    ) -What 'git clone'
} else {
    Write-Host "Reusing existing checkout at $OlaRepo" -ForegroundColor DarkGray
}

$fwDir = Join-Path $OlaRepo 'Firmware'
$patch = Join-Path $fwDir 'Extras\UartPower3.zip'
if (-not (Test-Path $patch)) {
    throw "Missing $patch -- is $OlaRepo really an OpenLog_Artemis checkout?"
}

# --- stage our sketch and Dockerfile into the build context ------------
$stagedSketch = Join-Path $fwDir 'OLA_Accel_BLE'
if (Test-Path $stagedSketch) { Remove-Item -Recurse -Force $stagedSketch }
New-Item -ItemType Directory -Path $stagedSketch | Out-Null
Copy-Item (Join-Path $repoRoot 'firmware\OLA_Accel_BLE\*') $stagedSketch -Recurse
Copy-Item (Join-Path $repoRoot 'firmware\Dockerfile.accel') $fwDir -Force

# --- build -------------------------------------------------------------
Write-Host "Building image '$Tag' (first run installs the Apollo3 core -- slow) ..." -ForegroundColor Cyan
$buildArgs = @('build', '-f', 'Dockerfile.accel', '-t', $Tag, '--progress=plain')
if ($NoCache) { $buildArgs += '--no-cache' }
$buildArgs += '.'

Push-Location $fwDir
try {
    Invoke-NativeChecked -Exe 'docker' -Arguments $buildArgs -What 'docker build'
} finally {
    Pop-Location
}

# --- extract the binary ------------------------------------------------
Invoke-Native -Exe 'docker' -Arguments @('rm', '-f', $container) -Quiet | Out-Null
Invoke-NativeChecked -Exe 'docker' -Arguments @('create', "--name=$container", "${Tag}:latest") -Quiet -What 'docker create'

$outPath = Join-Path $buildDir $binName
try {
    Invoke-NativeChecked -Exe 'docker' -Arguments @('cp', "${container}:/$binName", $outPath) -What 'docker cp'
} finally {
    Invoke-Native -Exe 'docker' -Arguments @('rm', $container) -Quiet | Out-Null
}

$size = (Get-Item $outPath).Length
Write-Host ''
Write-Host "OK  $outPath  ($size bytes)" -ForegroundColor Green
Write-Host 'Flash it with the Artemis Firmware Upload GUI (see README section 3).'
