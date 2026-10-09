#!/usr/bin/env bash
set -euo pipefail

# PaperKo 릴리스 — 새 판을 빌드·서명해 공개 저장소(update.Repo)의 GitHub 릴리스에 올린다.
# 앱은 그 릴리스의 manifest.json + 서명을 읽어 스스로 업데이트한다(internal/update).
#
#   ./scripts/release.sh 1.8.4 notes.md             판 올림·커밋·태그, 두 설치본 빌드·서명·업로드
#   ./scripts/release.sh 1.8.4 notes.md --dry-run   올리지 않는다(판 커밋·태그·push 도 안 함)
#   SKIP_BUILD=1 ./scripts/release.sh 1.8.4 notes.md  이미 bin/ 에 있는 설치본을 그대로 쓴다
#
# 서명 개인키는 저장소 밖(~/Library/Application Support/PaperKo/release-key/)에 있어야 한다.
# 없으면: (cd desktop/paperko && go run ./cmd/releasetool keygen) 로 만들고 출력된 공개키를
# internal/update/key.go 의 publicKey 에 넣는다. (이미 심어져 있으면 그대로 두면 된다.)

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"   # desktop/paperko
ROOT="$(cd "$APP_DIR/../.." && pwd)"                          # repo root
cd "$APP_DIR"
if command -v go >/dev/null 2>&1; then export PATH="$PATH:$(go env GOPATH)/bin"; fi

VERSION="${1:-}"; NOTES="${2:-}"; DRY=""
[ "${3:-}" = "--dry-run" ] && DRY=1
[ -n "$VERSION" ] && [ -f "$NOTES" ] || { echo "쓰는 법: ./scripts/release.sh 1.8.4 notes.md [--dry-run]"; exit 1; }
NOTES="$(cd "$(dirname "$NOTES")" && pwd)/$(basename "$NOTES")"
VERSION="${VERSION#v}"

RELEASES="$(sed -n 's/^const Repo = "\(.*\)"$/\1/p' internal/update/update.go)"
[ -n "$RELEASES" ] || { echo "릴리스 저장소(update.Repo)를 읽지 못했습니다"; exit 1; }

# 커밋되지 않은 변경이 있으면 멈춘다 — 설치본이 어느 커밋에서 나왔는지 말할 수 없게 된다.
if [ -z "$DRY" ] && [ -n "$(git -C "$ROOT" status --porcelain --untracked-files=no)" ]; then
  echo "커밋하지 않은 변경이 있습니다. 먼저 커밋하십시오."; git -C "$ROOT" status --short --untracked-files=no; exit 1
fi

# 판 번호를 올리고 커밋한다(이미 그 판이면 넘어간다).
CURRENT="$(grep -oE '[0-9]+\.[0-9]+\.[0-9]+' services/version.go | head -1)"
if [ "$CURRENT" != "$VERSION" ]; then
  if [ -z "$DRY" ]; then
    resources/python/bin/python3 "$ROOT/scripts/bump-version.py" "$VERSION"
    git -C "$ROOT" commit -q -am "Release v$VERSION"
  else
    echo "(시험) 판 번호는 바꾸지 않고 지금 판 $CURRENT 으로 만듭니다"; VERSION="$CURRENT"
  fi
fi

echo "▶ PaperKo $VERSION  (릴리스 저장소: $RELEASES)"
ARCH="$(uname -m)"   # arm64 (Apple Silicon)
OUT="$APP_DIR/bin/release-$VERSION"
rm -rf "$OUT" && mkdir -p "$OUT"
DMG="$OUT/PaperKo-$VERSION-$ARCH.dmg"
EXE="$OUT/PaperKo-$VERSION-amd64-installer.exe"

if [ -z "${SKIP_BUILD:-}" ]; then
  echo "== macOS 빌드 =="
  bash scripts/build-macos-dmg.sh "$DMG"
  echo "== Windows 빌드 (SSH) =="
  ( cd "$ROOT" && ./push-to-windows.sh --build )
  ssh "${PAPERKO_WIN_HOST:-nablamd-win}" \
    "powershell -NoProfile -ExecutionPolicy Bypass -File \"${PAPERKO_WIN_DEST:-C:/App-windows/PaperKo}/desktop/paperko/scripts/package-windows.ps1\""
  scp "${PAPERKO_WIN_HOST:-nablamd-win}:${PAPERKO_WIN_DEST:-C:/App-windows/PaperKo}/desktop/paperko/bin/PaperKo-$VERSION-amd64-installer.exe" "$EXE"
else
  echo "(SKIP_BUILD) bin/ 의 설치본을 사용합니다"
  cp "$APP_DIR/bin/PaperKo-$VERSION-$ARCH.dmg" "$DMG"
  cp "$APP_DIR/bin/PaperKo-$VERSION-amd64-installer.exe" "$EXE"
fi

echo "== manifest 서명 =="
TOOL="$(mktemp -t releasetool)"; trap 'rm -f "$TOOL"' EXIT
go build -o "$TOOL" ./cmd/releasetool 2>&1 | grep -v 'ld: warning' || true
"$TOOL" manifest -version "$VERSION" -notes "$NOTES" -mac-arch "$ARCH" -out "$OUT/manifest.json" "$DMG" "$EXE"
"$TOOL" sign "$OUT/manifest.json"
"$TOOL" verify "$OUT/manifest.json"
echo "릴리스 파일:"; ls -lh "$OUT"

if [ -n "$DRY" ]; then echo "(시험) 올리지 않았습니다."; exit 0; fi

git -C "$ROOT" tag -a "v$VERSION" -m "PaperKo $VERSION" 2>/dev/null || true
gh release create "v$VERSION" --repo "$RELEASES" --title "PaperKo $VERSION" --notes-file "$NOTES" \
  "$DMG" "$EXE" "$OUT/manifest.json" "$OUT/manifest.json.sig"
git -C "$ROOT" push -q origin HEAD --tags

# 오래된 릴리스 정리 — 배포 저장소에 최신 KEEP_RELEASES 판만 남긴다(설치본·태그 함께 삭제).
# 판이 무한정 쌓이지 않게 한다. KEEP_RELEASES 로 개수를 바꿀 수 있다(기본 2).
KEEP_RELEASES="${KEEP_RELEASES:-2}"
echo "== 오래된 릴리스 정리 (최신 $KEEP_RELEASES 판만 유지) =="
gh release list --repo "$RELEASES" --json tagName,createdAt \
  --jq 'sort_by(.createdAt)|reverse|.[].tagName' \
  | tail -n +$((KEEP_RELEASES + 1)) | while IFS= read -r oldtag; do
    [ -n "$oldtag" ] || continue
    echo "  삭제: $oldtag"
    gh release delete "$oldtag" --repo "$RELEASES" --cleanup-tag --yes || true
  done

# 릴리스 저장소 README 의 "변경 이력" 섹션에 이번 판을 넣는다(최신순, 중복 시 교체,
# 최신 KEEP_RELEASES 판만 유지해 릴리스 목록과 맞춘다).
echo "== 릴리스 저장소 README 변경 이력 갱신 =="
TAG_URL="https://github.com/$RELEASES/releases/tag/v$VERSION"
RELDIR="$(mktemp -d)"; trap 'rm -f "$TOOL"; rm -rf "$RELDIR"' EXIT
if gh repo clone "$RELEASES" "$RELDIR" -- -q --depth 1; then
  if python3 "$APP_DIR/scripts/update-release-readme.py" "$RELDIR/README.md" "$VERSION" "$NOTES" "$TAG_URL" "$KEEP_RELEASES" \
     && [ -n "$(git -C "$RELDIR" status --porcelain -- README.md)" ]; then
    git -C "$RELDIR" add README.md
    git -C "$RELDIR" commit -q -m "README: add $VERSION to release history"
    git -C "$RELDIR" push -q origin HEAD && echo "  ✓ README 변경 이력 푸시 완료"
  fi
else
  echo "  ! 릴리스 저장소를 클론하지 못해 README 갱신을 건너뜁니다(수동 반영 필요)"
fi

echo "완료: $TAG_URL"
