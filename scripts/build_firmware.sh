#!/usr/bin/env bash
# Build OLA_Accel_BLE.ino.bin in Docker. macOS / Linux / Git Bash.
# Mirror of scripts/build_firmware.ps1 -- see that file for the commentary.
#
#   ./scripts/build_firmware.sh [--no-cache] [--ola-repo PATH]

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD_DIR="$REPO_ROOT/build"
OLA_REPO="$BUILD_DIR/OpenLog_Artemis"
TAG="ola_accel_ble"
CONTAINER="ola_accel_container"
BIN_NAME="OLA_Accel_BLE.ino.bin"
NO_CACHE=""

while [ $# -gt 0 ]; do
  case "$1" in
    --no-cache)  NO_CACHE="--no-cache"; shift ;;
    --ola-repo)  OLA_REPO="$2"; shift 2 ;;
    --tag)       TAG="$2"; shift 2 ;;
    -h|--help)   sed -n '2,7p' "$0"; exit 0 ;;
    *)           echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

for exe in docker git; do
  command -v "$exe" >/dev/null 2>&1 || { echo "$exe not found on PATH" >&2; exit 1; }
done
docker info >/dev/null 2>&1 || { echo "Docker is not running." >&2; exit 1; }

mkdir -p "$BUILD_DIR"

if [ ! -d "$OLA_REPO" ]; then
  echo "Cloning OpenLog_Artemis into $OLA_REPO ..."
  git clone --depth 1 https://github.com/sparkfun/OpenLog_Artemis.git "$OLA_REPO"
else
  echo "Reusing existing checkout at $OLA_REPO"
fi

FW_DIR="$OLA_REPO/Firmware"
[ -f "$FW_DIR/Extras/UartPower3.zip" ] || {
  echo "Missing $FW_DIR/Extras/UartPower3.zip -- not an OpenLog_Artemis checkout?" >&2
  exit 1
}

rm -rf "$FW_DIR/OLA_Accel_BLE"
mkdir -p "$FW_DIR/OLA_Accel_BLE"
cp "$REPO_ROOT/firmware/OLA_Accel_BLE/"* "$FW_DIR/OLA_Accel_BLE/"
cp "$REPO_ROOT/firmware/Dockerfile.accel" "$FW_DIR/"

echo "Building image '$TAG' ..."
( cd "$FW_DIR" && docker build -f Dockerfile.accel -t "$TAG" --progress=plain $NO_CACHE . )

docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
docker create --name="$CONTAINER" "$TAG:latest" >/dev/null
trap 'docker rm "$CONTAINER" >/dev/null 2>&1 || true' EXIT
docker cp "$CONTAINER:/$BIN_NAME" "$BUILD_DIR/$BIN_NAME"

echo
echo "OK  $BUILD_DIR/$BIN_NAME  ($(wc -c < "$BUILD_DIR/$BIN_NAME") bytes)"
echo "Flash it with the Artemis Firmware Upload GUI (see README section 3)."
