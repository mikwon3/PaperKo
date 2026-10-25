#!/usr/bin/env bash
# Download a relocatable standalone CPython (python-build-standalone) into
# resources/python and install the translate_engine + deps into it, so the app
# bundles its own Python runtime (isolated from any system Python).
#
# Usage:  cd desktop/paperko && bash scripts/fetch-python-macos.sh
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$APP_DIR"
ENGINE="$(cd ../../engine-py && pwd)"

# Pin a python-build-standalone release + CPython version. Update as needed from
# https://github.com/astral-sh/python-build-standalone/releases
REL="20260610"
VER="3.12.13"
ARCH="$(uname -m)"           # arm64 -> aarch64, x86_64 -> x86_64
case "$ARCH" in
  arm64)  TRIPLE="aarch64-apple-darwin" ;;
  x86_64) TRIPLE="x86_64-apple-darwin" ;;
  *) echo "unsupported arch: $ARCH" >&2; exit 1 ;;
esac
URL="https://github.com/astral-sh/python-build-standalone/releases/download/${REL}/cpython-${VER}+${REL}-${TRIPLE}-install_only.tar.gz"

echo "==> downloading $URL"
rm -rf resources/python
mkdir -p resources
curl -fSL "$URL" -o /tmp/paperko_pbs.tar.gz
tar -xzf /tmp/paperko_pbs.tar.gz -C resources     # creates resources/python/

PY=resources/python/bin/python3
echo "==> installing engine + deps into the bundled runtime"
"$PY" -m pip install --upgrade pip
# deps first (safely upgradeable), then the engine with --ignore-installed so a
# prior RECORD-less install (pip "uninstall-no-record-file" error) is overwritten
# instead of uninstalled — keeps re-runs idempotent.
"$PY" -m pip install --upgrade pymupdf httpx pysbd pyahocorasick python-docx \
    latex2mathml mathml2omml \
    rapid_latex_ocr requests pillow
"$PY" -m pip install --ignore-installed --no-deps --no-cache-dir "$ENGINE"

# Pre-fetch the offline math-OCR models so the equation fallback works without
# internet. Non-fatal: if it can't download now it fetches on first use.
"$PY" -s -c "from rapid_latex_ocr import LaTeXOCR; LaTeXOCR(); print('math-OCR models cached')" \
    || echo "   (math-OCR models will download on first use)"

# pip 이 만든 콘솔 스크립트(pip·pytest·translate-engine …)를 지운다.
#
# 이 스크립트들의 shebang 에는 **빌드한 컴퓨터의 절대 경로**가 박힌다. 다른 컴퓨터에서는
# 그 경로가 없으므로 어차피 실행되지 않고, 설치본에 빌드한 사람의 폴더 이름이 그대로
# 실려 나간다. 앱은 엔진을 `python3 -s -m translate_engine` 으로 띄우므로(main.go)
# 이것들을 쓰지 않는다. python 실행 파일만 남긴다.
find resources/python/bin -type f ! -name 'python*' -delete

echo "==> verifying"
"$PY" -s -c "import translate_engine, pymupdf, httpx, pysbd, ahocorasick, docx, latex2mathml, mathml2omml; print('bundled python OK', translate_engine.__version__)"
echo "done -> resources/python"
