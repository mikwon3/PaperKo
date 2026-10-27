"""translator.py — Translator + Post Processor (03 §5).

Builds translation units from the IR, masks inline elements behind placeholders,
constructs prompts with context + relevant glossary terms, dispatches them to the
vLLM client with bounded concurrency, then runs the 8-step post-processor and
restores placeholders.  Progress is checkpointed to ``translation.json`` after
every completed TU so a job can resume or be cancelled without losing work.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

try:
    import pysbd

    _SEG = pysbd.Segmenter(language="en", clean=False)
except Exception:  # pragma: no cover
    _SEG = None

from .ir import Block, Document, Placeholder, TermHit, TranslationResult, TU
from .qwen import QwenClient
from .terminology import Glossary

log = logging.getLogger("translate_engine.translator")

PLACEHOLDER_RE = re.compile(r"⟦[A-Z]\d+⟧")
_KIND_PREFIX = {"math": "M", "citation": "R", "url": "U", "code": "C"}
_NUM_RE = re.compile(r"\d+(?:[.,]\d+)*")
_PREFIX_RE = re.compile(r"^\s*(번역\s*[:：]|translation\s*:|translated\s*:|한국어\s*[:：])\s*", re.I)
_CODEFENCE_RE = re.compile(r"^```[a-zA-Z]*\n?|\n?```$")
_SEG_RE = re.compile(r"<<<\s*(\d+)\s*>>>")  # page-batch segment markers

# Language display names (Korean) for building prompts from UI language codes.
_LANG_NAMES = {
    "auto": "원문", "": "원문",
    "en": "영어", "english": "영어", "영어": "영어",
    "ko": "한국어", "korean": "한국어", "한국어": "한국어",
    "ja": "일본어", "japanese": "일본어", "일본어": "일본어",
    "zh": "중국어", "chinese": "중국어", "중국어": "중국어",
    "de": "독일어", "german": "독일어", "독일어": "독일어",
    "fr": "프랑스어", "french": "프랑스어", "프랑스어": "프랑스어",
    "es": "스페인어", "spanish": "스페인어", "스페인어": "스페인어",
    "ru": "러시아어", "russian": "러시아어",
}


_LANG_EN = {
    "auto": "the source language", "": "the source language",
    "en": "English", "english": "English", "영어": "English",
    "ko": "Korean", "korean": "Korean", "한국어": "Korean",
    "ja": "Japanese", "japanese": "Japanese", "일본어": "Japanese",
    "zh": "Chinese", "chinese": "Chinese", "중국어": "Chinese",
    "de": "German", "german": "German", "독일어": "German",
    "fr": "French", "french": "French", "프랑스어": "French",
    "es": "Spanish", "spanish": "Spanish", "스페인어": "Spanish",
    "ru": "Russian", "russian": "Russian",
}


def _lang_ko(name: str) -> str:
    return _LANG_NAMES.get((name or "").strip().lower(), name)


def _lang_en(name: str) -> str:
    return _LANG_EN.get((name or "").strip().lower(), name)


def _is_korean_target(name: str) -> bool:
    return (name or "").strip().lower() in ("ko", "korean", "한국어")


@dataclass
class TranslateOptions:
    style: str = "formal"                 # formal | concise
    enforce_glossary: bool = False
    max_concurrency: int = 4
    page_range: list[int] | None = None
    custom_system_prompt: str | None = None
    source_token_budget: int = 1200       # one block stays one TU up to this (keeps layout)
    max_placeholder_retries: int = 2
    source_lang: str = "auto"             # source language name/code ("auto" = detect)
    target_lang: str = "Korean"           # target language (display name)
    max_batch_chars: int = 4000           # soft cap on source chars per page-batch call
    chunk_chars: int = 700                # chunk size when repairing a truncated/large TU
    #                                        (joined into one target) so small models don't truncate


@dataclass
class TUProgress:
    done: int
    total: int
    tu_id: str
    page: int | None = None


@dataclass
class Translator:
    client: QwenClient
    glossary: Glossary
    opts: TranslateOptions = field(default_factory=TranslateOptions)

    # ------------------------------------------------------------------ #
    # TU construction (03 §5.3)
    # ------------------------------------------------------------------ #
    def build_tus(self, doc: Document) -> list[TU]:
        block_map = doc.block_map()
        blocks = [b for b in doc.all_blocks() if b.translate and b.text().strip()]
        blocks.sort(key=lambda b: (b.page, b.reading_order))

        # group continues-chains together
        chains: list[list[Block]] = []
        consumed: set[str] = set()
        by_id = {b.id: b for b in blocks}
        for b in blocks:
            if b.id in consumed or b.continues in by_id:
                continue  # start only at chain heads
            chain = [b]
            consumed.add(b.id)
            # follow forward links
            nxt = _find_next_in_chain(b.id, blocks, consumed)
            while nxt is not None:
                chain.append(nxt)
                consumed.add(nxt.id)
                nxt = _find_next_in_chain(nxt.id, blocks, consumed)
            chains.append(chain)

        # any block not yet consumed (broken links) becomes its own chain
        for b in blocks:
            if b.id not in consumed:
                chains.append([b])
                consumed.add(b.id)

        chains.sort(key=lambda c: (c[0].page, c[0].reading_order))

        tus: list[TU] = []
        counter = 0
        prev_section = ""
        for chain in chains:
            # Split a continues-chain into logical units so each stays on its own
            # line while wrapped paragraph lines flow together (uniform font, no
            # per-line shrink). A heading / list item ("1.", "-", "①") begins a new
            # unit; following paragraph-continuation lines merge into the current
            # paragraph/list unit. This keeps numbered/bulleted items separated
            # (own region → own line) instead of collapsing them into one blob or
            # fragmenting a paragraph across independently-fitted single lines.
            groups: list[list[Block]] = []
            for b in chain:
                if (groups and b.type == "paragraph"
                        and groups[-1][0].type in ("paragraph", "list")):
                    groups[-1].append(b)
                else:
                    groups.append([b])
            for group in groups:
                text = _join_blocks(group)
                if not text.strip():
                    continue
                section = _current_section(group[0], block_map, doc)
                for piece in self._split_budget(text):
                    counter += 1
                    masked, placeholders = self._mask(piece, group)
                    term_hits = self.glossary.match(piece, max_terms=20)
                    tu = TU(
                        id=f"tu_{counter:04d}",
                        block_ids=[b.id for b in group],
                        source_text=masked,
                        placeholders=placeholders,
                        context={"section": section, "page": group[0].page},
                        term_hits=term_hits,
                    )
                    tus.append(tu)
            prev_section = prev_section  # (section tracking kept in context)
        # link prev_tu references
        for i in range(1, len(tus)):
            tus[i].context["prev_tu"] = tus[i - 1].id
        return tus

    def _split_budget(self, text: str) -> list[str]:
        # crude token estimate: chars / 3.2 for English (03 §4.3)
        if len(text) / 3.2 <= self.opts.source_token_budget:
            return [text]
        sentences = _sentences(text)
        chunks: list[str] = []
        cur = ""
        for s in sentences:
            if cur and (len(cur) + len(s)) / 3.2 > self.opts.source_token_budget:
                chunks.append(cur.strip())
                cur = s
            else:
                cur = f"{cur} {s}".strip()
        if cur.strip():
            chunks.append(cur.strip())
        return chunks or [text]

    def _mask(self, text: str, blocks: list[Block]) -> tuple[str, list[Placeholder]]:
        marks: list[dict] = []
        for b in blocks:
            marks.extend(b.inline_marks)
        latex_by: dict[tuple[str, str], str] = {}
        for m in marks:
            if m.get("raw") and m.get("latex"):
                latex_by.setdefault((m["kind"], m["raw"]), m["latex"])
        # longest raw first so nested spans mask cleanly
        marks = sorted(
            {(m["kind"], m["raw"]) for m in marks if m.get("raw")},
            key=lambda kr: -len(kr[1]),
        )
        placeholders: list[Placeholder] = []
        counters: dict[str, int] = {}
        masked = text
        for kind, raw in marks:
            if raw not in masked:
                continue
            prefix = _KIND_PREFIX.get(kind, "M")
            counters[prefix] = counters.get(prefix, 0) + 1
            key = f"{prefix}{counters[prefix]}"
            token = f"⟦{key}⟧"
            masked = masked.replace(raw, token, 1)
            placeholders.append(Placeholder(key=key, kind=kind, raw=raw,
                                            latex=latex_by.get((kind, raw), "")))
        return masked, placeholders

    # ------------------------------------------------------------------ #
    # Prompting (03 §5.4)
    # ------------------------------------------------------------------ #
    def _system_prompt(self) -> str:
        if self.opts.custom_system_prompt:
            return self.opts.custom_system_prompt
        # Korean target → polished Korean prompt (the primary use case).
        if _is_korean_target(self.opts.target_lang):
            src = self.opts.source_lang
            src_phrase = "원문" if src.strip().lower() in ("auto", "") else f"{_lang_ko(src)} 학술 텍스트"
            tone = (
                "정확하고 자연스러운 한국어 논문 문체(격식체, '-다' 종결)"
                if self.opts.style == "formal" else "간결하고 명료한 한국어"
            )
            return (
                f"당신은 학술 논문 전문 번역가입니다. {src_phrase}를 {tone}(으)로 번역합니다.\n"
                "규칙:\n"
                "1. ⟦A1⟧ 형태의 토큰은 수식·인용·링크 자리표시자입니다. 절대 수정·삭제·번역하지"
                " 말고 원문과 동일한 위치 관계로 유지하세요.\n"
                "2. 용어집이 주어지면 반드시 지정된 역어를 사용하세요.\n"
                "3. 번역문만 출력하세요. 설명, 주석, 원문 반복, 마크다운을 덧붙이지 마세요.\n"
                "4. 숫자, 단위, 고유명사, 약어(예: CNN, BLEU)는 원문 그대로 유지하세요.\n"
                "5. 수식·수학 기호·변수(예: f_y, α, x_i)는 원문 그대로 두고, LaTeX나 `$`, `\\(` 같은"
                " 기호로 감싸지 마세요.\n"
                "6. 원문의 글머리 기호(-, •, ·)·번호(①, 1., 가., a))와 들여쓰기·줄 구조를 그대로"
                " 유지하세요. 목록 항목은 항목대로, 문단은 문단대로 대응시키세요."
            )
        # Any other target → English instructions (avoids Korean-output bias).
        src = _lang_en(self.opts.source_lang)
        tgt = _lang_en(self.opts.target_lang)
        style = "a precise, natural academic style" if self.opts.style == "formal" else "a concise, clear style"
        return (
            f"You are an expert academic translator. Translate {src} academic text into {tgt}, in {style}.\n"
            "Rules:\n"
            "1. Tokens like ⟦A1⟧ are placeholders for math/citations/links — keep them unchanged and in the"
            " same relative position; never translate or delete them.\n"
            "2. If a glossary is provided, use the specified target-language terms.\n"
            f"3. Output ONLY the {tgt} translation — no explanations, notes, source repetition, or markdown.\n"
            "4. Keep numbers, units, proper nouns, and acronyms (e.g. CNN, BLEU) as in the source.\n"
            "5. Keep math symbols/variables (e.g. f_y, α, x_i) as-is; do not wrap them in LaTeX ($ or \\( ).\n"
            "6. Preserve the source's list markers (-, •, ·), numbering (①, 1., a)) and indentation/line"
            " structure: translate each list item as a list item and each paragraph as a paragraph."
        )

    def _in_target_script(self, text: str) -> bool:
        """Whether the text actually contains the target language's script."""
        t = (self.opts.target_lang or "").strip().lower()
        if t in ("ko", "korean", "한국어"):
            return any("가" <= c <= "힣" for c in text)
        if t in ("ja", "japanese", "일본어", "zh", "chinese", "중국어", "zh-cn", "zh_cn"):
            return any("぀" <= c <= "ヿ" or "㐀" <= c <= "鿿" for c in text)
        return bool(re.search(r"[A-Za-z]", text))  # Latin-script targets

    def _looks_truncated(self, tu: TU) -> bool:
        """A substantial block whose translation is far too short — the model stopped
        early (common with small models on long text). Repaired by chunked translation."""
        src = len(_plain_source(tu).strip())
        if src < 500:
            return False
        # A complete Korean translation of dense text runs ~0.40x the source length;
        # genuine truncation is far shorter (observed ~0.1–0.27). 0.30 separates them.
        return len((tu.target_text or "").strip()) < 0.30 * src

    def _verify_translated(self, tu: TU) -> None:
        """Downgrade a 'done' TU to 'failed' when a substantial block of *prose* came
        back NOT in the target language (untranslated / source echo). This surfaces it
        as a "재번역 필요" chip + highlight instead of silently keeping the original.

        Content that legitimately stays as-is — author names, affiliations, journal
        headers, numeric table cells, equations, citations — is NOT flagged (it would
        paint the page with false-failure highlights and hurt the rendered PDF)."""
        if tu.status != "done":
            return
        plain = _plain_source(tu)
        if len(plain.strip()) < 20:
            return  # short blocks (names, numbers, acronyms) may legitimately stay as-is
        if not _is_translatable_prose(plain):
            return  # names / numbers / headers / equations — kept source is correct
        if not self._in_target_script(tu.target_text or ""):
            tu.status = "failed"
            tu.error = {"code": "untranslated", "message": "대상 언어로 번역되지 않음"}

    def _is_echo(self, tu: TU) -> bool:
        """The model returned the source essentially unchanged (e.g. a heading like
        '2. Optimization method' → '2. Optimization method'). Numbers/spacing ignored."""
        tgt = (tu.target_text or "").strip()
        if not tgt or self._in_target_script(tgt):
            return False
        norm = lambda s: re.sub(r"[\d.\s]+", " ", s).strip().lower()
        return norm(tgt) == norm(_plain_source(tu))

    async def _retry_heading_echo(self, tu: TU) -> None:
        """Re-translate a heading the model echoed back untranslated, with an explicit
        heading instruction (common short headings like 'Introduction' get echoed)."""
        src = _plain_source(tu).strip()
        if not src:
            return
        if _is_korean_target(self.opts.target_lang):
            system = ("당신은 학술 논문 번역가입니다. 다음 절 제목을 한국어로 번역하세요. "
                      "번호(예: 2., 3.1)는 그대로 두고 제목만 번역하며, 번역문만 출력하세요.")
        else:
            tgt = _lang_en(self.opts.target_lang)
            system = (f"You are an academic translator. Translate this section heading into {tgt}. "
                      "Keep the number (e.g. 2., 3.1) as-is; output only the translation.")
        try:
            res = await self.client.chat(
                [{"role": "system", "content": system}, {"role": "user", "content": src}],
                max_tokens=128,
            )
        except Exception:  # noqa: BLE001 - keep the echoed text on any error
            return
        out, _kind, _w = self._postprocess(res.text, tu)
        if out and self._in_target_script(out) and not self._is_echo_text(out, src):
            tu.target_text = out
            tu.status = "done"

    def _is_echo_text(self, out: str, src: str) -> bool:
        norm = lambda s: re.sub(r"[\d.\s]+", " ", s).strip().lower()
        return norm(out) == norm(src)

    async def _retry_prose_echo(self, tu: TU) -> bool:
        """Last-chance retry for a *prose* block the model left untranslated — it
        echoed the source back or returned nothing.  This mostly hits short sentence
        fragments and list continuations ("c) longitudinal …", "members, and …;")
        which, handed to the model in isolation, are read as already-fine and echoed,
        or (on a reasoning model under a tight token budget) burn the whole budget on
        hidden reasoning and come back empty.  A forceful "this is a fragment, you
        MUST translate it" instruction plus a generous budget recovers them.

        Returns True when it produced a genuine target-language translation."""
        src = _plain_source(tu).strip()
        if not src:
            return False
        if _is_korean_target(self.opts.target_lang):
            system = (
                "당신은 학술 논문 번역가입니다. 아래 텍스트는 더 긴 문장에서 잘라낸 '조각'"
                "(목록 항목이나 이어지는 절)일 수 있습니다. 불완전해 보여도 반드시 한국어로 "
                "번역하세요. 영어를 그대로 두지 말고, 목록 기호(a), b), c))와 숫자·기호는 "
                "유지한 채 나머지는 모두 한국어로 옮기고, 번역문만 출력하세요."
            )
        else:
            tgt = _lang_en(self.opts.target_lang)
            system = (
                f"You are an academic translator. The text below may be a FRAGMENT cut "
                f"from a longer sentence (a list item or a continuing clause). Even if it "
                f"looks incomplete, you MUST translate it into {tgt}. Do not leave it in "
                f"the source language; keep list markers (a), b), c)) and numbers/symbols "
                f"as-is and translate the rest. Output only the translation."
            )
        try:
            res = await self.client.chat(
                [{"role": "system", "content": system}, {"role": "user", "content": src}],
                max_tokens=_estimate_out_tokens(src),
            )
        except Exception:  # noqa: BLE001 - keep whatever we had on any error
            return False
        out, _kind, _w = self._postprocess(res.text, tu)
        if out and self._in_target_script(out) and not self._is_echo_text(out, src):
            tu.target_text = out
            tu.status = "done"
            tu.error = None
            tu.warnings = tu.warnings + ["recovered by fragment retry"]
            return True
        return False

    def _user_prompt(
        self, tu: TU, doc_title: str, abstract_summary: str, prev_tu: TU | None
    ) -> str:
        lines = ["## 문맥", f"논문: {doc_title}"]
        if abstract_summary:
            lines.append(f"초록 요약: {abstract_summary}")
        if tu.context.get("section"):
            lines.append(f"현재 섹션: {tu.context['section']}")
        if prev_tu is not None:
            src_tail = _tail(_unmask_for_context(prev_tu), 200)
            tgt_tail = _tail(prev_tu.target_text or "", 300)
            if src_tail:
                lines.append(f"직전 문단 원문 끝: {src_tail}")
            if tgt_tail:
                lines.append(f"직전 문단 번역 끝: {tgt_tail}")
        if tu.term_hits:
            lines.append("\n## 용어집")
            for th in tu.term_hits:
                lines.append(f"{th.src} → {th.dst}")
        lines.append("\n## 번역할 텍스트")
        lines.append(tu.source_text)
        return "\n".join(lines)

    # ------------------------------------------------------------------ #
    # Page-batch prompting: translate all blocks of a page in ONE call so the
    # model has full-page context (much higher quality + fewer failures than
    # translating each block in isolation).
    # ------------------------------------------------------------------ #
    def _batch_user_prompt(self, tus: list[TU], doc_title: str, abstract_summary: str) -> str:
        lines = ["## 문맥", f"논문: {doc_title}"]
        if abstract_summary:
            lines.append(f"초록 요약: {abstract_summary}")
        if tus[0].context.get("section"):
            lines.append(f"현재 섹션: {tus[0].context['section']}")
        terms: dict[str, str] = {}
        for tu in tus:
            for th in tu.term_hits:
                terms[th.src] = th.dst
        if terms:
            lines.append("\n## 용어집")
            for s, d in terms.items():
                lines.append(f"{s} → {d}")
        if _is_korean_target(self.opts.target_lang):
            lines.append("\n## 번역 지침")
            lines.append(f"아래는 한 페이지의 문단들입니다. 각 문단을 순서대로 {_lang_ko(self.opts.target_lang)}로 번역하세요.")
            lines.append("각 문단은 <<<번호>>> 표식으로 구분됩니다. 번역도 각 문단 앞에 "
                         "동일한 <<<번호>>> 표식을 그대로 붙여 출력하고, 표식 외의 설명은 쓰지 마세요.")
        else:
            tgt = _lang_en(self.opts.target_lang)
            lines.append("\n## Instructions")
            lines.append(f"Translate each of the following paragraphs into {tgt}, in order.")
            lines.append("Each paragraph is marked with <<<n>>>. Prefix each translation with the SAME "
                         "<<<n>>> marker and output nothing else.")
        lines.append("")
        for i, tu in enumerate(tus, start=1):
            lines.append(f"<<<{i}>>>")
            lines.append(tu.source_text)
            lines.append("")
        return "\n".join(lines)

    def _parse_batch(self, raw: str, n: int) -> dict[int, str]:
        text = re.sub(r"<think>.*?</think>", "", raw, flags=re.S | re.I)
        text = _CODEFENCE_RE.sub("", text)
        parts = _SEG_RE.split(text)  # [preamble, '1', seg1, '2', seg2, ...]
        out: dict[int, str] = {}
        body = parts[1:]
        for j in range(0, len(body) - 1, 2):
            try:
                idx = int(body[j])
            except (ValueError, TypeError):
                continue
            if 1 <= idx <= n:
                out[idx] = body[j + 1].strip()
        return out

    async def _translate_page(
        self, page_tus: list[TU], doc_title: str, abstract_summary: str
    ) -> None:
        """Translate every TU on one page in a single call; fall back to per-TU
        translation for any segment the model omitted or mangled."""
        if len(page_tus) == 1:
            await self._translate_tu(page_tus[0], doc_title, abstract_summary, None)
            return
        for tu in page_tus:
            tu.status = "running"
        system = self._system_prompt()
        user = self._batch_user_prompt(page_tus, doc_title, abstract_summary)
        budget = sum(_estimate_out_tokens(t.source_text) for t in page_tus) + 80 * len(page_tus)
        try:
            res = await self.client.chat(
                [{"role": "system", "content": system}, {"role": "user", "content": user}],
                max_tokens=min(budget, 8192),
            )
        except Exception:  # noqa: BLE001 - whole-page call failed → per-TU fallback
            for tu in page_tus:
                await self._translate_tu(tu, doc_title, abstract_summary, None)
            return

        segments = self._parse_batch(res.text, len(page_tus))
        fallback: list[TU] = []
        for i, tu in enumerate(page_tus, start=1):
            seg = segments.get(i)
            if not seg:
                fallback.append(tu)
                continue
            output, kind, warnings = self._postprocess(seg, tu)
            repeats_source = any("repeats source" in w for w in warnings)
            # accept clean results, and soft ones EXCEPT an untranslated echo of the
            # source — those go to per-TU retry (which may legitimately keep names).
            if kind == "ok" or (kind == "soft" and not repeats_source):
                tu.target_text = output
                tu.status = "done"
                tu.warnings = warnings + ([] if kind == "ok" else ["batch soft-accept"])
            else:
                fallback.append(tu)
        # per-TU retry (with its own retry budget) for anything the batch missed
        for tu in fallback:
            await self._translate_tu(tu, doc_title, abstract_summary, None)

    # ------------------------------------------------------------------ #
    # Orchestration
    # ------------------------------------------------------------------ #
    async def translate_document(
        self,
        doc: Document,
        *,
        checkpoint_path: Path | None = None,
        resume_from: Path | None = None,
        only_pages: set[int] | None = None,
        on_progress: Callable[[TUProgress], None] | None = None,
        on_page_done: Callable[[int], None] | None = None,
        on_tu_failed: Callable[[TU], None] | None = None,
        cancel: asyncio.Event | None = None,
    ) -> TranslationResult:
        tus = self.build_tus(doc)
        doc.translation_units = tus

        # resume: reuse completed TUs from a previous checkpoint
        if resume_from and Path(resume_from).exists():
            prev = TranslationResult.load(resume_from).tu_map()
            for tu in tus:
                old = prev.get(tu.id)
                if old and old.status == "done" and old.target_text is not None:
                    tu.status = "done"
                    tu.target_text = old.target_text
                    tu.attempts = old.attempts
                    tu.edited_by_user = old.edited_by_user

        doc_title = doc.meta.get("title", "")
        abstract_summary = await self._abstract_summary(doc, tus)

        result = TranslationResult(doc_id=doc.doc_id, translation_units=tus)
        tu_by_id = {tu.id: tu for tu in tus}
        pending = [tu for tu in tus if tu.status != "done"]
        # Batch translation: only translate TUs on the requested pages this run; TUs on
        # other pages stay pending and are saved to the checkpoint untouched, so a later
        # batch (resumed from the same checkpoint) fills them in — the whole document
        # accumulates into one translation. `total` stays the whole-doc count so progress
        # reflects overall completion across batches.
        if only_pages is not None:
            pending = [tu for tu in pending if tu.context.get("page", 0) in only_pages]
        total = len(tus)
        done_count = total - len([tu for tu in tus if tu.status != "done"])
        lock = asyncio.Lock()
        pages_seen: dict[int, int] = {}
        pages_total: dict[int, int] = {}
        for tu in tus:
            p = tu.context.get("page", 0)
            pages_total[p] = pages_total.get(p, 0) + 1
            if tu.status == "done":
                pages_seen[p] = pages_seen.get(p, 0) + 1

        # heading block ids — used to retry a heading the model echoed untranslated
        # (title excluded: a masthead journal name is a brand that should stay as-is)
        heading_ids = {b.id for b in doc.all_blocks() if b.type == "heading"}

        # group pending TUs by page (preserve reading order) → one batched call each
        page_order: list[int] = []
        page_groups: dict[int, list[TU]] = {}
        for tu in pending:
            p = tu.context.get("page", 0)
            if p not in page_groups:
                page_groups[p] = []
                page_order.append(p)
            page_groups[p].append(tu)

        # Split each page into sub-batches capped by source size, so a small model
        # isn't asked to produce one very long output (which it truncates). A dense
        # page becomes a few batch calls; a light page stays a single call.
        cap = max(400, self.opts.max_batch_chars)
        batches: list[tuple[int, list[TU]]] = []
        for p in page_order:
            cur: list[TU] = []
            cur_len = 0
            for tu in page_groups[p]:
                L = len(tu.source_text)
                if cur and cur_len + L > cap:
                    batches.append((p, cur))
                    cur, cur_len = [], 0
                cur.append(tu)
                cur_len += L
            if cur:
                batches.append((p, cur))
        page_batches_left = {p: sum(1 for bp, _ in batches if bp == p) for p in page_order}

        # bounded concurrency: the semaphore bounds concurrent batch calls
        sem = asyncio.Semaphore(self.opts.max_concurrency)

        async def gated_batch(page: int, ptus: list[TU]) -> None:
            nonlocal done_count
            async with sem:
                if cancel and cancel.is_set():
                    return
                await self._translate_page(ptus, doc_title, abstract_summary)
                # repair blocks the model truncated (small models stop early on long
                # text) by re-translating them in bounded chunks. No-op for strong
                # models, which return complete translations.
                for tu in ptus:
                    if tu.status == "done" and self._looks_truncated(tu):
                        await self._translate_chunked(tu, doc_title)
                    # a heading the model echoed back untranslated → retry it
                    if (tu.status == "done" and heading_ids.intersection(tu.block_ids)
                            and self._is_echo(tu)):
                        await self._retry_heading_echo(tu)
                # a prose block the model echoed back untranslated (or returned empty
                # for) — common for short sentence fragments / list continuations, and
                # for reasoning models whose hidden budget swallowed the answer. Give it
                # one forceful fragment-aware retry before surfacing it as a failure.
                for tu in ptus:
                    self._verify_translated(tu)  # flag substantial untranslated blocks
                    if (tu.status == "failed"
                            and (tu.error or {}).get("code") == "untranslated"):
                        await self._retry_prose_echo(tu)
                async with lock:
                    for tu in ptus:
                        done_count += 1
                        if tu.status == "failed" and on_tu_failed:
                            on_tu_failed(tu)
                        if on_progress:
                            on_progress(TUProgress(done_count, total, tu.id, tu.context.get("page")))
                    page_batches_left[page] -= 1
                    if page_batches_left[page] <= 0:  # page fully done → fire page_done once
                        pages_seen[page] = pages_total.get(page, 0)
                        if on_page_done:
                            on_page_done(page)
                    if checkpoint_path:
                        result.save(checkpoint_path)

        tasks = [asyncio.create_task(gated_batch(p, ptus)) for p, ptus in batches]

        # Abort promptly on cancel: setting the event alone only stops NEW TUs from
        # starting — in-flight LLM calls would keep running (seconds each). This
        # watcher cancels the running tasks so their HTTP requests are torn down at
        # once. CancelledError is a BaseException, so per-TU `except Exception`
        # handlers won't swallow it.
        async def _abort_on_cancel() -> None:
            assert cancel is not None
            await cancel.wait()
            for t in tasks:
                if not t.done():
                    t.cancel()

        watcher = asyncio.create_task(_abort_on_cancel()) if cancel else None
        try:
            # return_exceptions so a cancelled TU doesn't abort the whole gather;
            # cancellation is detected via the event below.
            await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            if watcher is not None and not watcher.done():
                watcher.cancel()

        cancelled = bool(cancel and cancel.is_set())
        result.cancelled = cancelled
        if cancelled:
            # Keep only fully-translated pages in the checkpoint so the temp output
            # never shows a half-Korean/half-original page. TUs on any page that
            # isn't 100% done are reset to pending and re-done on the next batch.
            self._drop_incomplete_pages(tus)
        result.stats = self._collect_stats(tus, cancelled)
        if checkpoint_path:
            result.save(checkpoint_path)
        return result

    @staticmethod
    def _drop_incomplete_pages(tus: list[TU]) -> None:
        # Only the page(s) actually in flight when cancelled can be left half-Korean.
        # Reset just those (their in-flight 'running' units and any already-done units
        # on the same page) so they re-do cleanly on resume. Every other page is left
        # untouched — resetting pages whose leftover units merely failed or were never
        # started would strand good translations that later batches never re-cover.
        cut_pages = {t.context.get("page", 0) for t in tus if t.status == "running"}
        if not cut_pages:
            return
        for t in tus:
            if t.context.get("page", 0) in cut_pages and t.status in ("done", "running"):
                t.status = "pending"
                t.target_text = None

    async def translate_single(self, doc: Document, tu_id: str) -> TU:
        """UC-03 re-translation. Re-does the WHOLE page containing the unit with
        full-page context (the batch path), so a failed block is retried with the
        same context that makes the initial pass good — not re-run in isolation."""
        tus = doc.translation_units or self.build_tus(doc)
        doc.translation_units = tus
        tu_by_id = {tu.id: tu for tu in tus}
        tu = tu_by_id.get(tu_id)
        if tu is None:
            raise KeyError(tu_id)
        page = tu.context.get("page", 0)
        page_tus = [t for t in tus if t.context.get("page", 0) == page]
        for t in page_tus:
            t.status = "pending"
            t.attempts = 0
            t.target_text = None
            t.edited_by_user = False
        abstract_summary = tu.context.get("abstract_summary", "")
        await self._translate_page(page_tus, doc.meta.get("title", ""), abstract_summary)
        for t in page_tus:
            self._verify_translated(t)
        return tu

    # ------------------------------------------------------------------ #
    async def _abstract_summary(self, doc: Document, tus: list[TU]) -> str:
        """Translate/summarise the abstract once, reused as global context."""
        abstract_tu = None
        for tu in tus:
            sec = (tu.context.get("section") or "").lower()
            if "abstract" in sec or "초록" in sec:
                abstract_tu = tu
                break
        if abstract_tu is None:
            # fall back to the first sizable paragraph
            for tu in tus:
                if len(tu.source_text) > 200:
                    abstract_tu = tu
                    break
        if abstract_tu is None:
            return ""
        try:
            if _is_korean_target(self.opts.target_lang):
                sys_msg = "다음 학술 초록을 한국어 2문장 이내로 요약하라. 요약문만 출력."
            else:
                sys_msg = f"Summarize the following academic abstract in {_lang_en(self.opts.target_lang)} in at most 2 sentences. Output only the summary."
            messages = [
                {"role": "system", "content": sys_msg},
                {"role": "user", "content": abstract_tu.source_text[:1500]},
            ]
            res = await self.client.chat(messages, max_tokens=256)
            return _tail(res.text.strip().replace("\n", " "), 300)
        except Exception as exc:  # noqa: BLE001 - summary is best-effort
            log.info("abstract summary skipped: %s", exc)
            return ""

    def _chunk_text(self, text: str, max_chars: int) -> list[str]:
        sents = _sentences(text)
        chunks: list[str] = []
        cur = ""
        for s in sents:
            if cur and len(cur) + len(s) > max_chars:
                chunks.append(cur.strip())
                cur = s
            else:
                cur = f"{cur} {s}".strip()
        if cur.strip():
            chunks.append(cur.strip())
        return chunks or [text]

    async def _translate_chunked(self, tu: TU, doc_title: str) -> None:
        """Translate a large block in bounded chunks, joined into ONE target_text, so
        a small model doesn't truncate a single long output. The block stays one TU
        (one render region)."""
        system = self._system_prompt()
        sect = tu.context.get("section", "")
        outs: list[str] = []
        for ch in self._chunk_text(tu.source_text, self.opts.chunk_chars):
            lines = ["## 문맥", f"논문: {doc_title}"]
            if sect:
                lines.append(f"현재 섹션: {sect}")
            lines.append("\n## 번역할 텍스트")
            lines.append(ch)
            try:
                res = await self.client.chat(
                    [{"role": "system", "content": system}, {"role": "user", "content": "\n".join(lines)}],
                    max_tokens=_estimate_out_tokens(ch),
                )
            except Exception:  # noqa: BLE001 - a bad chunk shouldn't kill the whole TU
                continue
            text = re.sub(r"<think>.*?</think>", "", res.text, flags=re.S | re.I)
            text = _CODEFENCE_RE.sub("", text)
            text = _PREFIX_RE.sub("", text).strip()
            if text:
                outs.append(text)
        joined = " ".join(outs).strip()
        present = {m.strip("⟦⟧") for m in PLACEHOLDER_RE.findall(joined)}
        for p in tu.placeholders:
            joined = joined.replace(f"⟦{p.key}⟧", p.raw)
        joined = PLACEHOLDER_RE.sub("", joined).strip()
        missing = [p for p in tu.placeholders if p.key not in present]
        if missing:
            joined = (joined + " " + " ".join(p.raw for p in missing)).strip()
        if joined:
            tu.target_text = joined
            tu.status = "done"
            tu.warnings = (tu.warnings or []) + ["chunked"]
        else:
            tu.status = "failed"
            tu.error = {"code": "empty", "message": "empty chunked output"}
            tu.target_text = _plain_source(tu)

    async def _translate_tu(
        self, tu: TU, doc_title: str, abstract_summary: str, prev_tu: TU | None
    ) -> None:
        tu.status = "running"
        # large blocks: translate in bounded chunks (small models truncate long outputs)
        if len(tu.source_text) > int(self.opts.chunk_chars * 1.5):
            await self._translate_chunked(tu, doc_title)
            if tu.status == "done":
                return
        system = self._system_prompt()
        user = self._user_prompt(tu, doc_title, abstract_summary, prev_tu)
        max_attempts = self.opts.max_placeholder_retries + 1
        last_soft: str | None = None

        for attempt in range(1, max_attempts + 1):
            tu.attempts = attempt
            try:
                res = await self.client.chat(
                    [{"role": "system", "content": system}, {"role": "user", "content": user}],
                    max_tokens=_estimate_out_tokens(tu.source_text),
                )
            except Exception as exc:  # noqa: BLE001 - record and stop retrying this TU
                from .errors import EngineError

                code = exc.code if isinstance(exc, EngineError) else "internal"
                tu.status = "failed"
                tu.error = {"code": code, "message": str(exc)}
                tu.target_text = _plain_source(tu)
                return

            output, kind, warnings = self._postprocess(res.text, tu)
            tu.warnings = warnings
            if kind == "ok":
                tu.target_text = output
                tu.status = "done"
                return
            if kind == "soft":
                # a usable, placeholder-restored translation that merely tripped a
                # heuristic (repetition/length) — keep it as a fallback candidate
                last_soft = output

        # retries exhausted.  A soft-flagged candidate (e.g. author names that stay
        # Latin, or a short term whose Korean is compact) is still a real translation
        # — accept it rather than reverting to English and flagging failure.
        if last_soft is not None:
            tu.target_text = last_soft
            tu.status = "done"
            tu.warnings = tu.warnings + ["accepted despite soft quality check"]
            return
        # only hard failures (empty / broken placeholders / errors) keep source
        tu.status = "failed"
        tu.error = {"code": "postprocess", "message": "; ".join(tu.warnings) or "quality check failed"}
        tu.target_text = _plain_source(tu)

    # ------------------------------------------------------------------ #
    # Post-processing pipeline (03 §5.5)
    # ------------------------------------------------------------------ #
    def _postprocess(self, raw: str, tu: TU) -> tuple[str, str, list[str]]:
        """Run the 8-step post-processor.

        Returns ``(text, kind, warnings)`` where ``kind`` is ``"ok"`` (clean),
        ``"hard"`` (unusable — empty/broken placeholders; retry then fail) or
        ``"soft"`` (restored but tripped a heuristic; retry, else accept).
        """
        warnings: list[str] = []
        text = raw

        # 1. strip think blocks / code fences / whitespace
        text = re.sub(r"<think>.*?</think>", "", text, flags=re.S | re.I).strip()
        text = _CODEFENCE_RE.sub("", text).strip()
        # 2. strip leading "번역:" / "Translation:" prefixes
        text = _PREFIX_RE.sub("", text).strip()

        # 3. placeholder reconciliation.  Small models routinely DROP math/citation
        #    tokens (⟦R1⟧ …). Hard-failing here reverts the whole block to English and
        #    floods the doc with "재번역 필요". Instead: restore the tokens that are
        #    present, drop hallucinated extras, and re-append any the model omitted so
        #    nothing is lost — flag it soft (one retry, then accept) rather than fail.
        want_keys = [p.key for p in tu.placeholders]
        got = {m.strip("⟦⟧") for m in PLACEHOLDER_RE.findall(text)}
        missing = [k for k in want_keys if k not in got]

        # 4. restore present placeholders → raw, then strip any leftover ⟦…⟧ tokens
        for p in tu.placeholders:
            text = text.replace(f"⟦{p.key}⟧", p.raw)
        text = PLACEHOLDER_RE.sub("", text).strip()
        if missing:
            tail = " ".join(p.raw for p in tu.placeholders if p.key in missing)
            text = (text + " " + tail).strip() if text else tail
            warnings.append(f"restored {len(missing)} dropped placeholder(s)")

        # 6a. empty output (HARD)
        if not text.strip():
            return text, "hard", ["empty output"]

        # 6b. source-repetition (SOFT — proper nouns/acronyms legitimately stay Latin)
        if _looks_like_source(text, _plain_source(tu)):
            return text, "soft", ["output repeats source"]

        # 8. length anomaly (SOFT — compact technical terms trip this legitimately;
        #    also skipped for short sources like 초록↔Abstract)
        plain_src = _plain_source(tu)
        src_len = max(len(plain_src), 1)
        ratio = len(text) / src_len
        if src_len >= 24 and (ratio < 0.3 or ratio > 3.0):
            return text, "soft", [f"length ratio {ratio:.2f} out of bounds"]

        # 5. number multiset check (non-blocking warning)
        if not _multiset_subset(_NUM_RE.findall(plain_src), _NUM_RE.findall(text)):
            warnings.append("number mismatch vs source")

        # 7. enforced glossary (optional): substitute or warn
        if self.opts.enforce_glossary:
            for v in self.glossary.verify(plain_src, text):
                if v.src in text:
                    text = re.sub(rf"\b{re.escape(v.src)}\b", v.expected, text)
                else:
                    warnings.append(f"glossary term not applied: {v.src}→{v.expected}")

        # a restored-placeholder result is usable but imperfect → soft (batch accepts
        # it directly; per-TU gives it one retry for a cleaner placement, then accepts)
        return text, ("soft" if missing else "ok"), warnings

    # ------------------------------------------------------------------ #
    def _collect_stats(self, tus: list[TU], cancelled: bool) -> dict:
        done = sum(1 for t in tus if t.status == "done")
        failed = sum(1 for t in tus if t.status == "failed")
        # page-level progress: a page is "done" once all of its TUs are translated,
        # so the app can report progress in pages rather than translation units.
        page_total: dict[int, int] = {}
        page_done: dict[int, int] = {}
        for t in tus:
            p = t.context.get("page", 0)
            page_total[p] = page_total.get(p, 0) + 1
            if t.status == "done":
                page_done[p] = page_done.get(p, 0) + 1
        done_pages = sorted(p for p, n in page_total.items() if page_done.get(p, 0) == n)
        return {
            "tu_total": len(tus),
            "tu_done": done,
            "tu_failed": failed,
            "pages_total": len(page_total),
            "pages_done": len(done_pages),
            "done_page_list": done_pages,   # 0-based pages fully translated (for resume)
            "retries": self.client.total_retries,
            "tokens_prompt": self.client.total_prompt_tokens,
            "tokens_completion": self.client.total_completion_tokens,
            "cancelled": cancelled,
        }


# --------------------------------------------------------------------------- #
# module helpers
# --------------------------------------------------------------------------- #
def _find_next_in_chain(block_id: str, blocks: list[Block], consumed: set[str]) -> Block | None:
    for b in blocks:
        if b.continues == block_id and b.id not in consumed:
            return b
    return None


def _is_cjk(ch: str) -> bool:
    return (
        "가" <= ch <= "힣"      # Hangul syllables
        or "぀" <= ch <= "ヿ"   # Hiragana/Katakana
        or "㐀" <= ch <= "鿿"   # CJK ideographs
    )


def _join_blocks(blocks: list[Block]) -> str:
    """Join line-fragmented blocks. A PDF line-wrap between two CJK characters
    has no real space (e.g. "논리 자" + "체가" → "논리 자체가"), so only insert a
    space when at least one side of the boundary is not CJK."""
    parts = [b.text() for b in blocks if b.text().strip()]
    if not parts:
        return ""
    out = parts[0]
    for nxt in parts[1:]:
        if out and nxt and _is_cjk(out[-1]) and _is_cjk(nxt[0]):
            out += nxt
        else:
            out += " " + nxt
    return out.strip()


def _sentences(text: str) -> list[str]:
    if _SEG is not None:
        try:
            return [s for s in _SEG.segment(text) if s.strip()]
        except Exception:  # noqa: BLE001
            pass
    return re.split(r"(?<=[.!?])\s+", text)


def _current_section(block: Block, block_map: dict, doc: Document) -> str:
    """Nearest preceding heading/title in reading order across the document."""
    ordered = sorted(doc.all_blocks(), key=lambda b: (b.page, b.reading_order))
    section = ""
    for b in ordered:
        if b.id == block.id:
            break
        if b.type in {"heading", "title"}:
            section = b.text()[:80]
    return section


def _tail(text: str, n: int) -> str:
    text = (text or "").strip()
    return text[-n:] if len(text) > n else text


def _unmask_for_context(tu: TU) -> str:
    text = tu.source_text
    for p in tu.placeholders:
        text = text.replace(f"⟦{p.key}⟧", p.raw)
    return text


def _plain_source(tu: TU) -> str:
    return _unmask_for_context(tu)


def _is_translatable_prose(text: str) -> bool:
    """Whether a block is running prose that SHOULD be translated (vs. author names,
    affiliations, journal headers, numeric table cells, equations, citations, which
    legitimately stay as-is). Prose has several lowercase word-tokens and is mostly
    letters rather than digits/symbols."""
    s = text.strip()
    if not s:
        return False
    non_space = [c for c in s if not c.isspace()]
    if not non_space:
        return False
    letters = sum(1 for c in non_space if c.isalpha())
    if letters < 0.55 * len(non_space):          # mostly digits/symbols → data, not prose
        return False
    if sum(1 for c in non_space if _is_cjk(c)) >= 8:
        return True                               # a substantial CJK run is prose (KO/JA/ZH source)
    # Latin: a real sentence has several lowercase words; names/titles are Capitalized
    lower_words = sum(1 for w in re.findall(r"[A-Za-z][A-Za-z']+", s)
                      if len(w) >= 3 and w[0].islower())
    return lower_words >= 3


def _looks_like_source(output: str, source: str) -> bool:
    # Short sources (headings, proper nouns like author names, acronyms) legitimately
    # translate to something identical or still-Latin — don't treat those as failures.
    if len(source.strip()) < 24:
        return False
    latin = sum(1 for c in output if ("a" <= c.lower() <= "z"))
    hangul = sum(1 for c in output if "가" <= c <= "힣")
    letters = latin + hangul
    if letters == 0:
        return False
    # only a genuinely untranslated long block trips this: lots of Latin, almost no Hangul
    return latin / letters > 0.5 and hangul < letters * 0.1


def _multiset_subset(sub: list[str], sup: list[str]) -> bool:
    from collections import Counter

    cs, cp = Counter(sub), Counter(sup)
    return all(cp[k] >= v for k, v in cs.items())


def _estimate_out_tokens(source_text: str) -> int:
    # Korean output tends to be a bit longer than the source; on top of that,
    # reasoning models spend a large hidden budget before answering.  Start with a
    # generous floor so the answer is not truncated; qwen.py grows it further if
    # the response is still cut off.
    est = int(len(source_text) / 2.0) + 2048
    return max(2048, min(8192, est))
