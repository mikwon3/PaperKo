#!/usr/bin/env bash
set -euo pipefail

# Mac → Windows 소스 밀어넣기 (PaperKo).
#
# Dropbox 로 작업 폴더를 통째로 동기화하면 두 가지가 깨진다 — 플랫폼별 바이너리가
# 서로 덮어쓰고(번들 파이썬·llama·frontend/node_modules), 빌드 중 파일이 바뀌어
# 산출물이 손상된다. 그래서 Windows 쪽은 Dropbox 밖(C:\App-windows)에 두고,
# **필요할 때 Mac 에서 한 방향으로만** 민다. 방향은 언제나 Mac → Windows 다.
#
#   ./push-to-windows.sh          소스만 보낸다
#   ./push-to-windows.sh --build  보내고 Windows 에서 빌드까지 한다 (bin\paperko.exe)
#
# SSH 대상은 ~/.ssh/config 의 Host 별칭(기본 nablamd-win: 203.255.40.88:60, 키인증).

HOST="${PDF_WIN_HOST:-nablamd-win}"
DEST="${PDF_WIN_DEST:-C:/App-windows/PDF-translator}"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# macOS tar 는 확장 속성을 `._이름`(AppleDouble)로 함께 넣는다. 그 이진 파일이
# Windows 에서 `*.ts` 같은 글롭에 걸리면 esbuild 가 NUL(`\x00`)을 만나 죽는다.
export COPYFILE_DISABLE=1

# 보내지 않는 것 — 플랫폼마다 다르거나 저쪽에서 새로 만드는 것.
EXCLUDES=(
  --exclude ./.git
  --exclude ./.venv                 # macOS 개발용 venv
  --exclude ./.claude               # 세션 데이터
  --exclude node_modules            # 플랫폼별 네이티브 바이너리 (esbuild 등)
  --exclude dist                    # vite 산출물. npm run build 가 다시 만든다
  --exclude bin                     # 빌드 산출물(dmg/exe/app). Windows 에서 새로 만든다
  --exclude ./engine-py/build       # pip 빌드 잔재(폰트 중복). 설치 시 새로 생성
  --exclude ./engine-py/dist        # sdist/wheel 잔재
  --exclude build/darwin            # macOS 전용 wails 빌드 자산
  --exclude "*/resources/python"    # macOS 번들 CPython (Windows 는 fetch-python 으로 받음)
  --exclude "*/resources/llama"     # macOS llama.cpp 바이너리
  --exclude paperko_app_bundled     # macOS 실행 바이너리
  --exclude "*.exe"
  --exclude "*.dmg"
  --exclude "*.app"
  --exclude "*.mp4"
  --exclude "*.hwpx"                # 시험 산출물
  --exclude .DS_Store
  --exclude "._*"                   # AppleDouble 잔재
  --exclude __pycache__
  --exclude "*.egg-info"
  --exclude "*.pyc"
)

echo "▶ 대상   : $HOST:$DEST"
printf "▶ 보낼 것: "
tar czf - -C "$REPO" "${EXCLUDES[@]}" . 2>/dev/null | wc -c | awk '{printf "%.1f MB\n", $1/1048576}'

tar czf - -C "$REPO" "${EXCLUDES[@]}" . | ssh "$HOST" "tar -xzf - -C \"$DEST\""
echo "✓ 소스 전송 완료"

if [ "${1:-}" = "--build" ]; then
  echo
  echo "▶ Windows 에서 빌드 (bin\\paperko.exe + bin\\resources)"
  # powershell 세션은 시스템 PATH(Go/Node/wails3/npm 포함)를 그대로 물려받는다.
  ssh "$HOST" "powershell -NoProfile -ExecutionPolicy Bypass -File \"$DEST/desktop/paperko/scripts/build-windows.ps1\""
fi
