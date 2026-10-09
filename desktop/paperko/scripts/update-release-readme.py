#!/usr/bin/env python3
"""릴리스 저장소 README 의 "변경 이력 · Releases" 섹션에 한 판을 추가/갱신한다.

release.sh 가 `gh release create` 뒤에 호출한다. 릴리스 노트 파일에서 "변경점"
부분(맨 위 제목 줄 다음부터 '내려받기'/'Download' 헤딩 앞까지)을 뽑아,
`### [<판>](<태그 URL>)` 블록으로 만들어 섹션 맨 위(최신순)에 끼운다. 같은 판이
이미 있으면 그 블록을 교체하므로 여러 번 돌려도 중복되지 않는다.

사용:
    update-release-readme.py <README.md> <version> <notes.md> <tag_url>

바뀐 것이 있으면 파일을 쓰고 0, 섹션을 못 찾으면 1 을 돌려준다.
"""
from __future__ import annotations

import re
import sys

SECTION = "## 변경 이력"          # 섹션 헤딩(접두)
STOP = re.compile(r"^#+\s*(내려받기|Download)\b")   # 노트에서 여기부터는 버린다


def summarize(notes: str) -> str:
    lines = notes.splitlines()
    out: list[str] = []
    started = False
    for ln in lines:
        if not started:
            if ln.strip() == "":
                continue
            started = True
            if ln.startswith("## "):   # 노트 맨 위 제목(## PaperKo x.y.z) 은 버린다
                continue
        if STOP.match(ln):
            break
        out.append(ln)
    while out and out[-1].strip() == "":
        out.pop()
    # 헤딩을 한 단계씩 내려서(## → ###) 섹션 구조와 겹치지 않게 한다
    demoted = ["#" + ln if re.match(r"^#+\s", ln) else ln for ln in out]
    return "\n".join(demoted).strip()


def main() -> int:
    if len(sys.argv) not in (5, 6):
        sys.exit("usage: update-release-readme.py <README.md> <version> <notes.md> <tag_url> [keep]")
    readme_path, version, notes_path, tag_url = sys.argv[1:5]
    keep = int(sys.argv[5]) if len(sys.argv) == 6 else 2   # 최신 몇 판만 남길지

    text = open(readme_path, encoding="utf-8").read()
    notes = open(notes_path, encoding="utf-8").read()
    entry = f"### [{version}]({tag_url})\n{summarize(notes)}\n"

    lines = text.split("\n")
    si = next((i for i, l in enumerate(lines) if l.startswith(SECTION)), None)
    if si is None:
        print(f"  ! README 에 '{SECTION}' 섹션이 없어 건너뜁니다", file=sys.stderr)
        return 1
    ei = next((i for i in range(si + 1, len(lines)) if lines[i].startswith("## ")), len(lines))

    head = lines[: si + 1]
    body = lines[si + 1 : ei]
    tail = lines[ei:]

    # body = 인트로(첫 '### [' 이전) + 기존 항목들
    fe = next((i for i, l in enumerate(body) if l.startswith("### [")), len(body))
    intro = "\n".join(body[:fe]).strip()
    entries = "\n".join(body[fe:]).strip()

    # 같은 판 블록 제거(그 헤딩부터 다음 '### [' 또는 끝까지)
    entries = re.sub(
        r"(?ms)^### \[" + re.escape(version) + r"\]\(.*?(?=^### \[|\Z)", "", entries
    ).strip()

    # 새 항목을 맨 위에 두고, 버전 블록(### [..]) 기준으로 최신 keep 개만 남긴다.
    combined = entry.strip() + ("\n\n" + entries if entries else "")
    blocks = [b.strip() for b in re.split(r"(?m)(?=^### \[)", combined) if b.strip()]
    combined = "\n\n".join(blocks[:keep])
    new_body = intro + "\n\n" + combined
    new_text = "\n".join(head) + "\n\n" + new_body + "\n\n" + "\n".join(tail)
    # 과한 빈 줄 정리
    new_text = re.sub(r"\n{3,}", "\n\n", new_text).rstrip() + "\n"

    if new_text == text:
        print("  (README 변경 이력에 바뀐 내용 없음)")
        return 0
    open(readme_path, "w", encoding="utf-8").write(new_text)
    print(f"  ✓ README 변경 이력에 {version} 반영")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
