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

function Assert-LastExitCode {
    param([string] $What)
    if ($LASTEXITCODE -ne 0) { throw "$What failed (exit $LASTEXITCODE)" }
}

# --- prerequisites ----------------------------------------------------
foreach ($exe in @('docker', 'git')) {
    if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) {
        throw "$exe was not found on PATH. Install it and try again."
    }
}

docker info *> $null
if ($LASTEXITCODE -ne 0) { throw 'Docker is installed but not running. Start Docker Desktop and retry.' }

if (-not (Test-Path $buildDir)) { New-Item -ItemType Directory -Path $buildDir | Out-Null }

# --- OpenLog_Artemis checkout (we need Extras/UartPower3.zip) ----------
if (-not (Test-Path $OlaRepo)) {
    Write-Host "Cloning OpenLog_Artemis into $OlaRepo ..." -ForegroundColor Cyan
    git clone --depth 1 https://github.com/sparkfun/OpenLog_Artemis.git $OlaRepo
    Assert-LastExitCode 'git clone'
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
Write-Host "Building image '$Tag' ..." -ForegroundColor Cyan
$buildArgs = @('build', '-f', 'Dockerfile.accel', '-t', $Tag, '--progress=plain')
if ($NoCache) { $buildArgs += '--no-cache' }
$buildArgs += '.'

Push-Location $fwDir
try {
    & docker @buildArgs
    Assert-LastExitCode 'docker build'
} finally {
    Pop-Location
}

# --- extract the binary ------------------------------------------------
docker rm -f $container *> $null   # ignore failure: it usually does not exist
$LASTEXITCODE = 0

docker create --name=$container "${Tag}:latest" | Out-Null
Assert-LastExitCode 'docker create'

$outPath = Join-Path $buildDir $binName
try {
    docker cp "${container}:/$binName" $outPath
    Assert-LastExitCode 'docker cp'
} finally {
    docker rm $container *> $null
    $LASTEXITCODE = 0
}

$size = (Get-Item $outPath).Length
Write-Host ''
Write-Host "OK  $outPath  ($size bytes)" -ForegroundColor Green
Write-Host 'Flash it with the Artemis Firmware Upload GUI (see README section 3).'
