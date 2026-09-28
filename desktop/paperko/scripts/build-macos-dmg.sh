#!/usr/bin/env bash
# Build a self-contained PaperKo.app + PaperKo.dmg for macOS (Apple Silicon).
#
# Bundles the standalone Python engine into the .app so the installed app needs
# no system Python.  Produces an ad-hoc-signed .dmg — for public distribution,
# re-sign and notarize with your Apple Developer ID (see NOTES at the bottom).
#
# Usage:  cd desktop/paperko && bash scripts/build-macos-dmg.sh [output.dmg]
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"          # desktop/paperko
cd "$APP_DIR"

# `go install` puts wails3 in $(go env GOPATH)/bin, which isn't always on PATH
# (esp. in non-login shells). Add it so `wails3` resolves here and in sub-tasks.
if command -v go >/dev/null 2>&1; then
  export PATH="$PATH:$(go env GOPATH)/bin"
fi
if ! command -v wails3 >/dev/null 2>&1; then
  echo "ERROR: wails3 CLI not found. Install it, then re-run:" >&2
  echo "  go install github.com/wailsapp/wails/v3/cmd/wails3@v3.0.0-beta.4" >&2
  echo "(it installs to \$(go env GOPATH)/bin — this script adds that to PATH automatically)" >&2
  exit 1
fi
# Version from the single source of truth (services/version.go) + CPU arch, so
# the .dmg filename carries them, e.g. PaperKo-1.0.0-arm64.dmg
VERSION="$(grep -oE '[0-9]+\.[0-9]+\.[0-9]+' services/version.go | head -1)"
VERSION="${VERSION:-0.0.0}"
ARCH="$(uname -m)"   # arm64 (Apple Silicon) or x86_64
OUT_DMG="${1:-$HOME/Downloads/PaperKo-${VERSION}-${ARCH}.dmg}"
STAGE="$(mktemp -d /tmp/paperko_dmg.XXXXXX)"

echo "==> generating Wails bindings (frontend imports them)"
wails3 generate bindings >/dev/null 2>&1 || echo "    (bindings step skipped)"

echo "==> 1/6  building frontend"
( cd frontend && npm run build )

echo "==> 2/6  ensuring bundled Python runtime exists (resources/python)"
if [ ! -x resources/python/bin/python3 ]; then
  echo "    resources/python missing — run scripts/fetch-python-macos.sh first" >&2
  exit 1
fi

echo "==> 3/6  building + packaging .app (wails3)"
wails3 task package                                   # -> bin/paperko.app

echo "==> 4/6  injecting the Python engine (+ optional local llama.cpp) into the .app"
mkdir -p "bin/paperko.app/Contents/Resources/resources"
rm -rf "bin/paperko.app/Contents/Resources/resources/python"
cp -R resources/python "bin/paperko.app/Contents/Resources/resources/python"
# Local (offline) inference runtime — present only if fetch-llama-macos.sh was run.
if [ -d resources/llama ]; then
  rm -rf "bin/paperko.app/Contents/Resources/resources/llama"
  cp -R resources/llama "bin/paperko.app/Contents/Resources/resources/llama"
  echo "    bundled llama.cpp (offline mode enabled)"
else
  echo "    (no resources/llama — offline mode NOT bundled; run scripts/fetch-llama-macos.sh to include it)"
fi

echo "==> 5/6  ad-hoc re-signing the bundle"
codesign --force --deep --sign - "bin/paperko.app"

echo "==> 6/6  building .dmg (drag-to-Applications)"
rm -rf "$STAGE"/* ; mkdir -p "$STAGE"
cp -R "bin/paperko.app" "$STAGE/PaperKo.app"
ln -s /Applications "$STAGE/Applications"
codesign --force --deep --sign - "$STAGE/PaperKo.app"
rm -f "$OUT_DMG"
hdiutil create -volname "PaperKo" -srcfolder "$STAGE" -ov -format UDZO "$OUT_DMG"
rm -rf "$STAGE"

echo "done -> $OUT_DMG"

# NOTES — distribution to other Macs:
#   The .app is arm64-only and ad-hoc signed, so Gatekeeper warns on first open
#   (right-click > Open to bypass).  For clean distribution:
#     codesign --deep --options runtime --sign "Developer ID Application: <NAME> (<TEAMID>)" bin/paperko.app
#     xcrun notarytool submit PaperKo.dmg --apple-id <id> --team-id <TEAMID> --password <app-pw> --wait
#     xcrun stapler staple PaperKo.dmg
