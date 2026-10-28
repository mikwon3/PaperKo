"""layout.py — PDF Parser + Layout Analyzer + (optional) OCR.

Responsibilities (03-detailed-design.md §2):

* open / validate the PDF, extract metadata
* extract text, style and coordinates with PyMuPDF
* detect blocks, classify their type, compute reading order
* link paragraphs that continue across column/page boundaries
* serialise the result into the document IR

The design nominates a DocLayout-YOLO ONNX model for block detection.  That
model file is not bundled in this build, so we run the documented fallback
(§2.4): PyMuPDF's own block extraction plus heuristic classification.  The
public interface is model-agnostic, so an ONNX backend can slot in later.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
from collections.abc import Callable
from pathlib import Path

import pymupdf as fitz  # PyMuPDF (modern import; avoids the legacy `fitz` deprecation warning)

from .errors import EngineError, FILE_NOT_FOUND, PDF_CORRUPT, PDF_ENCRYPTED
from .ir import (
    Block,
    Column,
    Document,
    DocMeta,
    Line,
    Page,
    Span,
    StyleSummary,
)

log = logging.getLogger("translate_engine.layout")

# OCR language data bundled with the package (like the fonts). PyMuPDF's MuPDF
# build has the Tesseract engine compiled in, so only the *.traineddata files are
# needed — no system Tesseract install. TESSDATA_PREFIX must point at the dir that
# DIRECTLY contains the *.traineddata files (Tesseract 4+ semantics).
_BUNDLED_TESSDATA_DIR = Path(__file__).resolve().parent / "tessdata"


def _ensure_tessdata_env() -> None:
    """Point MuPDF's OCR at the bundled tessdata unless the environment overrides it."""
    if not os.environ.get("TESSDATA_PREFIX") and _BUNDLED_TESSDATA_DIR.is_dir():
        os.environ["TESSDATA_PREFIX"] = str(_BUNDLED_TESSDATA_DIR)


# Fonts whose glyphs indicate mathematics (03 §2.3).
_MATH_FONT_RE = re.compile(r"(CMMI|CMSY|CMEX|MSAM|MSBM|Math|Symbol|Mathematica)", re.I)
_CITATION_RE = re.compile(r"\[\d{1,3}(?:\s*[-,]\s*\d{1,3})*\]")
_AUTHOR_YEAR_RE = re.compile(r"\([A-Z][A-Za-z.\-]+(?:\s+et\s+al\.?)?,?\s*\d{4}[a-z]?\)")
_URL_RE = re.compile(r"https?://[^\s)]+|doi:\s*\S+", re.I)
_CAPTION_RE = re.compile(r"^\s*(figure|fig\.?|table|tab\.?|algorithm|listing)\s*\d", re.I)
_LIST_RE = re.compile(r"^\s*(?:[-•·▪◦*]|\(?\d{1,2}[.)]|[a-z][.)])\s+")
_SENT_END_RE = re.compile(r"[.!?:;)\]\"'”’]\s*$")
# numbered section heading: "1. Introduction", "3.1 RC specimen", "3.2 Details …"
_SECTION_HEAD_RE = re.compile(r"^\s*\d{1,2}(?:\.\d{1,2}){0,2}\.?\s+[A-Z]")
# a real caption *title* line: "Fig. 2 Mushroom-shaped …", "Table 1 Physical …" — the
# word after the number is Capitalized. This excludes a body sentence that merely
# starts "Fig. 7 presents the wrapping method …" (lowercase verb → not a caption).
_CAP_SPLIT_RE = re.compile(r"^\s*(?:[Ff]ig\.?|[Ff]igure|[Tt]able|[Tt]ab\.?)\s*\d+\.?\s+[A-Z]")

# Control characters (keep tab/newline). Some spec fonts encode a provision-change
# marker as a raw control char (e.g. ACI 440.11 uses U+0019/U+001B before clause
# numbers), which renders as a ◆ tofu box in the output font. They are never valid
# content, so strip them everywhere. U+00AD is a soft hyphen (invisible break point).
_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")


# A provision-change "=" marker glued to the start of a clause number or provision
# text in ACI/CSA codes, e.g. "=4.9.2 The strength…", "=The Code provisions…". The
# marker always sits at the start of its own span (the bold clause-number span or the
# body span), so stripping a leading "=" before an alphanumeric cleans every case.
_PROVISION_EQ_RE = re.compile(r"^=(?=[0-9A-Za-z])")


def _clean_span_text(text: str) -> str:
    """Drop control chars (spec change-marker tofu), soft hyphens, and a leading "="
    provision marker from raw span text so none reach the model or render as ◆ boxes."""
    if not text:
        return text
    if _CTRL_RE.search(text):
        text = _CTRL_RE.sub("", text)
    if "­" in text:
        text = text.replace("­", "")
    if text[:1] == "=":
        text = _PROVISION_EQ_RE.sub("", text)
    return text


def _line_text(ln: Line) -> str:
    return "".join(sp.text for sp in ln.spans).strip()


def _line_role(text: str) -> str:
    if _CAP_SPLIT_RE.match(text):
        return "caption"
    if _SECTION_HEAD_RE.match(text) and len(text) < 70 and not _SENT_END_RE.search(text):
        return "heading"
    return "body"


def _split_block_lines(lines: list[Line]) -> list[list[Line]]:
    """Split a raw PDF text block into logical blocks at section-heading and caption
    boundaries. PyMuPDF often lumps a caption, the following section heading and its
    body paragraph into one block; keeping them merged buries the heading inside a
    paragraph and mislabels the whole thing. A heading/caption line starts a new
    block, and the line after a heading starts another (the heading stands alone)."""
    segs: list[list[Line]] = []
    cur: list[Line] = []
    prev_role = "body"
    for ln in lines:
        t = _line_text(ln)
        if not t:                       # blank line — keep with the current segment
            cur.append(ln)
            continue
        role = _line_role(t)
        # Split at a numbered section heading or a caption-title line, so a lumped
        # "Fig. 2 … / Table 1 …" (two captions) or caption+body separates. A heading
        # is standalone (the following line also starts a new block); a caption may
        # wrap, so its continuation lines are allowed to stay with it.
        if cur and (role in ("heading", "caption") or prev_role == "heading"):
            segs.append(cur)
            cur = []
        cur.append(ln)
        prev_role = role

    if cur:
        segs.append(cur)

    def _trim(seg: list[Line]) -> list[Line]:
        while seg and not _line_text(seg[0]):
            seg = seg[1:]
        while seg and not _line_text(seg[-1]):
            seg = seg[:-1]
        return seg

    return [s for s in (_trim(x) for x in segs) if s]


class LayoutEngine:
    """Parses a PDF into the document IR."""

    def __init__(self, model_dir: Path | None = None, ocr_enabled: bool = True) -> None:
        self.model_dir = Path(model_dir) if model_dir else None
        self.ocr_enabled = ocr_enabled

    # ------------------------------------------------------------------ #
    # Metadata only (cheap; runs before analyze)
    # ------------------------------------------------------------------ #
    def open_document(self, pdf_path: Path, password: str | None = None) -> DocMeta:
        pdf_path = Path(pdf_path)
        if not pdf_path.exists():
            raise EngineError(FILE_NOT_FOUND, f"file not found: {pdf_path}")
        try:
            doc = fitz.open(pdf_path)
        except Exception as exc:  # noqa: BLE001 - PyMuPDF raises broad errors
            raise EngineError(PDF_CORRUPT, f"cannot open PDF: {exc}") from exc

        try:
            if doc.needs_pass:
                if not password or not doc.authenticate(password):
                    raise EngineError(PDF_ENCRYPTED, "PDF is password protected")

            doc_id = _doc_id(pdf_path)
            pages_without_text: list[int] = []
            for i in range(doc.page_count):
                page = doc.load_page(i)
                if _text_density(page) < _TEXT_DENSITY_MIN:
                    pages_without_text.append(i)
            title = (doc.metadata or {}).get("title") or pdf_path.stem
            return DocMeta(
                doc_id=doc_id,
                page_count=doc.page_count,
                title=title,
                has_text_layer=len(pages_without_text) < doc.page_count,
                encrypted=bool(doc.needs_pass),
                pages_without_text=pages_without_text,
            )
        finally:
            doc.close()

    # ------------------------------------------------------------------ #
    # Full analysis → IR
    # ------------------------------------------------------------------ #
    def analyze(
        self,
        pdf_path: Path,
        *,
        pages: list[int] | None = None,
        password: str | None = None,
        ocr: str = "auto",
        ocr_lang: str = "eng",
        on_progress: Callable[[int, int], None] | None = None,
    ) -> Document:
        pdf_path = Path(pdf_path)
        meta = self.open_document(pdf_path, password)
        doc = fitz.open(pdf_path)
        if doc.needs_pass and password:
            doc.authenticate(password)

        try:
            page_indices = pages if pages is not None else list(range(doc.page_count))
            total = len(page_indices)
            ir_pages: list[Page] = []
            block_counter = 0

            for done, pi in enumerate(page_indices):
                try:
                    page = doc.load_page(pi)
                    ir_page, block_counter = self._analyze_page(
                        page, pi, block_counter, ocr=ocr, ocr_lang=ocr_lang
                    )
                except Exception as exc:  # noqa: BLE001 - never crash on one bad page
                    log.warning("page %d failed to analyze: %s", pi, exc)
                    src = doc.load_page(pi) if pi < doc.page_count else None
                    ir_page = Page(
                        index=pi,
                        width=src.rect.width if src else 612.0,
                        height=src.rect.height if src else 792.0,
                    )
                ir_pages.append(ir_page)
                if on_progress:
                    on_progress(done + 1, total)

            block_count = sum(len(p.blocks) for p in ir_pages)
            document = Document(
                doc_id=meta.doc_id,
                source_path=str(pdf_path),
                meta={
                    "title": meta.title,
                    "page_count": meta.page_count,
                    "has_text_layer": meta.has_text_layer,
                    "language": meta.language,
                },
                pages=ir_pages,
                stats={
                    "block_count": block_count,
                    "ocr_pages": sum(1 for p in ir_pages if p.ocr_used),
                },
            )
            _mark_references_document(document)
            _mark_frontmatter_metadata(document)
            _mark_mdpi_sidebar(document)
            _mark_table_data(document)
            return document
        finally:
            doc.close()

    # ------------------------------------------------------------------ #
    # per-page pipeline
    # ------------------------------------------------------------------ #
    def _analyze_page(
        self, page: "fitz.Page", index: int, block_counter: int, *, ocr: str,
        ocr_lang: str = "eng",
    ) -> tuple[Page, int]:
        raw = page.get_text("dict")
        rotation = page.rotation
        width, height = page.rect.width, page.rect.height

        ocr_used = False
        text_blocks = [b for b in raw.get("blocks", []) if b.get("type") == 0]
        density = _text_density(page)
        # Re-OCR when there is no text layer (low density) OR the text layer is present
        # but decodes to gibberish (broken font cmap / substitution cipher) — otherwise
        # the garbled glyphs are trusted and translation produces nonsense.
        garbled = (ocr != "off" and density >= _TEXT_DENSITY_MIN
                   and _text_layer_garbled(page))
        if ocr != "off" and (ocr == "force" or density < _TEXT_DENSITY_MIN or garbled):
            ocr_blocks = self._try_ocr(page, lang=ocr_lang)
            if ocr_blocks is not None:
                text_blocks = ocr_blocks
                ocr_used = True

        # ① build raw blocks (each a list of lines of spans)
        blocks: list[Block] = []
        for tb in text_blocks:
            lines = _extract_lines(tb)
            if not lines or not any(sp.text.strip() for ln in lines for sp in ln.spans):
                continue
            # split a lumped block at heading/caption boundaries into logical blocks
            for seg in _split_block_lines(lines):
                block_counter += 1
                x0 = min(l.bbox[0] for l in seg)
                y0 = min(l.bbox[1] for l in seg)
                x1 = max(l.bbox[2] for l in seg)
                y1 = max(l.bbox[3] for l in seg)
                blk = Block(
                    id=f"b_{block_counter:04d}",
                    page=index,
                    type="paragraph",
                    bbox=[round(x0, 2), round(y0, 2), round(x1, 2), round(y1, 2)],
                    lines=seg,
                )
                blk.style_summary = _summarize_style(blk, width)
                blocks.append(blk)

        # ①b strip manuscript line-numbers (margin-column integers) so they don't
        #     pollute the translation text
        _strip_line_numbers(blocks, width)
        blocks = [b for b in blocks if b.lines and b.text().strip()]

        # ② column detection
        columns = _detect_columns(blocks, width)

        # ③ classification (needs neighbours / page geometry)
        _classify_blocks(blocks, page_index=index, width=width, height=height)

        # ④ reading order (column-aware XY-cut).  Horizontal separator rules break
        #    paragraph chaining so blocks on opposite sides are never merged.
        _order_blocks(blocks, columns, _extract_rules(page))

        # ⑤ inline mark detection (math / citation / url) for the translator
        for blk in blocks:
            if blk.translate:
                blk.inline_marks = _detect_inline_marks(blk)

        return (
            Page(
                index=index,
                width=round(width, 2),
                height=round(height, 2),
                rotation=rotation,
                ocr_used=ocr_used,
                columns=columns,
                blocks=blocks,
            ),
            block_counter,
        )

    def _try_ocr(self, page: "fitz.Page", *, lang: str = "eng") -> list[dict] | None:
        """OCR fallback for scanned pages.  Uses PyMuPDF's Tesseract bridge with the
        bundled tessdata; returns ``None`` (skip) otherwise so analysis still succeeds."""
        if not self.ocr_enabled:
            return None
        _ensure_tessdata_env()
        try:
            tp = page.get_textpage_ocr(flags=0, full=True, language=lang)
            raw = page.get_text("dict", textpage=tp)
            return [b for b in raw.get("blocks", []) if b.get("type") == 0]
        except Exception as exc:  # noqa: BLE001 - OCR is best-effort
            log.info("OCR unavailable for page %d (%s); using text layer", page.number, exc)
            return None


# --------------------------------------------------------------------------- #
# extraction helpers
# --------------------------------------------------------------------------- #
# Characters of extractable text per 1000 pt² below which a page is treated as
# having no usable text layer (scan).  A typical text page scores ~1–5; a scanned
# page scores ~0, so a low threshold cleanly separates the two.
_TEXT_DENSITY_MIN = 0.2


def _text_density(page: "fitz.Page") -> float:
    txt = page.get_text("text") or ""
    area = max(page.rect.width * page.rect.height, 1.0)
    return len(txt.strip()) / area * 1000.0


def _extract_lines(text_block: dict) -> list[Line]:
    lines: list[Line] = []
    for ln in text_block.get("lines", []):
        spans: list[Span] = []
        for sp in ln.get("spans", []):
            text = _clean_span_text(sp.get("text", ""))
            if not text:
                continue
            flags = int(sp.get("flags", 0))
            spans.append(
                Span(
                    text=text,
                    bbox=[round(v, 2) for v in sp.get("bbox", [0, 0, 0, 0])],
                    font=sp.get("font", ""),
                    size=round(float(sp.get("size", 0.0)), 2),
                    color=_int_to_hex(sp.get("color", 0)),
                    flags={
                        "bold": bool(flags & 2 ** 4) or "bold" in sp.get("font", "").lower(),
                        "italic": bool(flags & 2 ** 1) or "italic" in sp.get("font", "").lower(),
                        "superscript": bool(flags & 2 ** 0),
                    },
                )
            )
        if spans:
            lines.append(Line(bbox=[round(v, 2) for v in ln.get("bbox", [0, 0, 0, 0])], spans=spans))
    return lines


def _summarize_style(block: Block, page_width: float) -> StyleSummary:
    sizes: list[float] = []
    bolds = italics = serif = 0
    n = 0
    color = "#000000"
    for ln in block.lines:
        for sp in ln.spans:
            if not sp.text.strip():
                continue
            n += 1
            sizes.append(sp.size or 10.0)
            bolds += sp.flags.get("bold", False)
            italics += sp.flags.get("italic", False)
            serif += _is_serif(sp.font)
            color = sp.color
    n = max(n, 1)
    font_size = _median(sizes) if sizes else 10.0

    # line height from consecutive line baselines
    line_height = 1.32
    if len(block.lines) >= 2:
        gaps = [
            block.lines[i + 1].bbox[1] - block.lines[i].bbox[1]
            for i in range(len(block.lines) - 1)
        ]
        gaps = [g for g in gaps if g > 0]
        if gaps and font_size > 0:
            line_height = round(max(1.0, min(2.0, _median(gaps) / font_size)), 2)

    return StyleSummary(
        font_size=round(font_size, 2),
        bold=bolds > n / 2,
        italic=italics > n / 2,
        color=color,
        align=_infer_align(block, page_width),
        line_height=line_height,
        serif=serif > n / 2,
    )


def _infer_align(block: Block, page_width: float) -> str:
    if not block.lines:
        return "left"
    x0 = block.bbox[0]
    x1 = block.bbox[2]
    left_margin = x0
    right_margin = page_width - x1
    width = x1 - x0
    balanced = abs(left_margin - right_margin) < 12
    is_body = block.type in ("paragraph", "list", "reference", "footnote")
    # centered: comparable margins and narrow-ish. Body paragraphs that merely
    # fill the text column with near-symmetric page margins must NOT be mistaken
    # for centered text (that renders the translation centered and looks broken),
    # so for body blocks require a genuine indent on both sides and a clearly
    # narrow width. Headings/titles/captions keep the lenient rule.
    if balanced:
        if not is_body and width < page_width * 0.75:
            return "center"
        if is_body and width < page_width * 0.55 and left_margin > page_width * 0.18:
            return "center"
    # justify: most lines reach a common right edge
    rights = [ln.bbox[2] for ln in block.lines]
    if len(rights) >= 3:
        maxr = max(rights)
        flush = sum(1 for r in rights[:-1] if maxr - r < 4)
        if flush >= len(rights[:-1]) * 0.7:
            return "justify"
    return "left"


_LINE_NUMBER_RE = re.compile(r"^\d{1,4}$")


def _strip_line_numbers(blocks: list[Block], page_width: float) -> None:
    """Remove manuscript line-number artifacts (pre-submission format).

    Such numbers sit in a narrow margin column as their own line (e.g. ``60`` at
    the far left/right), separate from the body text column.  Left in place they
    get appended to each block's text and wreck the sentence fed to the LLM.  We
    drop only lone integers that sit clearly outside the body text column and only
    when several appear on the page (so ordinary numbers are never touched).
    """
    def lone_int(line: Line) -> bool:
        spans = [sp for sp in line.spans if sp.text.strip()]
        return len(spans) == 1 and bool(_LINE_NUMBER_RE.match(spans[0].text.strip()))

    body_lefts: list[float] = []
    body_rights: list[float] = []
    for blk in blocks:
        for ln in blk.lines:
            if any(sp.text.strip() for sp in ln.spans) and not lone_int(ln):
                body_lefts.append(ln.bbox[0])
                body_rights.append(ln.bbox[2])
    if not body_lefts:
        return
    body_left = _median(body_lefts)
    body_right = _median(body_rights)

    # collect candidate line-number lines sitting outside the body column
    to_remove: set[int] = set()
    for blk in blocks:
        for ln in blk.lines:
            if not lone_int(ln):
                continue
            x0, x1 = ln.bbox[0], ln.bbox[2]
            in_left_margin = x1 <= body_left - 3
            in_right_margin = x0 >= body_right + 3
            if in_left_margin or in_right_margin:
                to_remove.add(id(ln))

    if len(to_remove) < 3:  # not a line-numbered document
        return

    for blk in blocks:
        kept = [ln for ln in blk.lines if id(ln) not in to_remove]
        if len(kept) != len(blk.lines):
            blk.lines = kept
            if kept:
                blk.bbox = [
                    round(min(ln.bbox[0] for ln in kept), 2),
                    round(min(ln.bbox[1] for ln in kept), 2),
                    round(max(ln.bbox[2] for ln in kept), 2),
                    round(max(ln.bbox[3] for ln in kept), 2),
                ]


# --------------------------------------------------------------------------- #
# column detection
# --------------------------------------------------------------------------- #
def _detect_columns(blocks: list[Block], page_width: float) -> list[Column]:
    """Detect column bands from the horizontal distribution of block centers."""
    mid = page_width / 2
    gut = page_width * 0.05  # half-gutter tolerance around the midline

    def _straddles(b: Block) -> bool:
        # crosses the midline → a full-width / table / spanning element, not a
        # single-column body block
        return b.bbox[0] < mid - gut and b.bbox[2] > mid + gut

    # Body sample for the gutter vote: single-column-sized blocks that sit cleanly
    # on one side of the page. Full-width blocks, gutter-crossing blocks (table
    # rows, spanning notes, wide figure captions) and tiny marks (page numbers)
    # are NOT column bodies — left in, a handful of them centers near the midline
    # and wrongly flips a clear two-column page to one column (e.g. a results page
    # with a wide stats table or a full-width Taylor-diagram caption).
    body = [
        b for b in blocks
        if (b.bbox[2] - b.bbox[0]) < page_width * 0.7
        and not _straddles(b)
        and (b.bbox[2] - b.bbox[0]) > page_width * 0.04
    ]
    if len(body) < 4:
        return [Column(x0=0.0, x1=round(page_width, 2))]

    centers = sorted((b.bbox[0] + b.bbox[2]) / 2 for b in body)
    # Two-column test: is there a clear vertical gutter near the middle?
    near_mid = [c for c in centers if abs(c - mid) < page_width * 0.12]
    left = [c for c in centers if c < mid - gut]
    right = [c for c in centers if c > mid + gut]
    if len(near_mid) <= len(centers) * 0.15 and left and right and \
            len(left) > len(centers) * 0.2 and len(right) > len(centers) * 0.2:
        return [
            Column(x0=0.0, x1=round(mid, 2)),
            Column(x0=round(mid, 2), x1=round(page_width, 2)),
        ]
    return [Column(x0=0.0, x1=round(page_width, 2))]


# --------------------------------------------------------------------------- #
# classification
# --------------------------------------------------------------------------- #
def _classify_blocks(blocks: list[Block], *, page_index: int, width: float, height: float) -> None:
    if not blocks:
        return
    body_size = _median([b.style_summary.font_size for b in blocks]) or 10.0
    in_references = False

    for blk in blocks:
        text = blk.text()
        st = blk.style_summary
        low = text.lower().strip()
        y0, y1 = blk.bbox[1], blk.bbox[3]

        # header / footer by vertical position + short text
        if y1 < height * 0.06 and len(text) < 120:
            blk.type = "header"
        elif y0 > height * 0.94 and len(text) < 120:
            blk.type = "footer" if not text.strip().isdigit() else "footer"
        elif _CAPTION_RE.match(text):
            blk.type = "caption"
        elif re.match(r"^\s*(references|bibliography|참고문헌)\s*$", low):
            blk.type = "heading"
            in_references = True
        elif in_references:
            blk.type = "reference"
        elif _is_formula(blk) or _is_ocr_formula(blk):
            blk.type = "formula"
        elif page_index == 0 and st.font_size >= body_size * 1.6 and y0 < height * 0.4:
            blk.type = "title"
        elif (st.bold or st.font_size >= body_size * 1.15) and len(text) < 140 \
                and not _SENT_END_RE.search(text):
            blk.type = "heading"
        elif _SECTION_HEAD_RE.match(text) and len(text) < 70 and not _SENT_END_RE.search(text):
            blk.type = "heading"                     # "3.1 RC specimen" (not bolded)
        elif _LIST_RE.match(text):
            blk.type = "list"
        else:
            blk.type = "paragraph"

        # Reference list entries stay in the original language — author names,
        # paper/journal titles and DOIs should not be translated, and doing so
        # overflows the dense two-column bibliography.
        blk.translate = blk.type not in {"figure", "table", "formula", "header", "footer", "reference"}
        # page numbers / very short footers need no translation
        if blk.type in {"header", "footer"} and len(text.strip()) <= 6:
            blk.translate = False


# Labelled display equation ("Equation 10.7", "Eq. 5", "식 (3)").
_EQ_LABEL_RE = re.compile(r"\b(?:Equation|Eq\.?|식)\s*\(?\s*\d+(?:\.\d+)?", re.I)
_MATH_SYMS = set("=+×÷*/^_<>≤≥≈≠±~|()[]{}∑∏∫√∞")


def _is_ocr_formula(block: Block) -> bool:
    """Catch equations in scanned/OCR'd PDFs, where the math-font signal is gone.

    OCR turns a printed equation into garbled text (``Aro? rpf epr + … = …``) with no
    CMMI/CMSY fonts, so ``_is_formula`` misses it. We instead look at the *shape* of
    the text: a short block that is mostly operators, digits and one/two-character
    tokens — optionally carrying an "Equation N.N" label — and crucially NOT a normal
    prose sentence (which has many long dictionary words). Kept conservative so real
    paragraphs are never skipped.
    """
    text = block.text().strip()
    if not text or len(text) > 160:
        return False
    words = text.split()
    chars = [c for c in text if not c.isspace()]
    if not words or not chars:
        return False
    has_eq = "=" in text
    label = bool(_EQ_LABEL_RE.search(text))
    if not (has_eq or label):
        return False
    sym_ratio = sum(1 for c in chars if c in _MATH_SYMS or c.isdigit()) / len(chars)
    short = sum(1 for w in words if len(w) <= 2
                or any(ch.isdigit() or ch in _MATH_SYMS for ch in w))
    short_ratio = short / len(words)
    longwords = sum(1 for w in words if len(w) >= 4 and w.isalpha())
    # (1) strong math shape: an '=' with lots of symbols and short tokens;
    # (2) a labelled equation that is math-heavy and not a prose reference to it.
    if has_eq and sym_ratio >= 0.22 and short_ratio >= 0.5 and longwords <= 4:
        return True
    if label and (has_eq or sym_ratio >= 0.25) and longwords <= 3:
        return True
    return False


def _text_layer_garbled(page: "fitz.Page") -> bool:
    """True when a dense text layer decodes to gibberish (broken font cmap).

    Some PDFs — especially pages extracted/re-saved from a scanned+OCR'd original —
    carry a text layer whose glyphs map to the wrong Unicode (a substitution cipher),
    e.g. ``Transverse`` → ``BQ&Z_;“Q_“``. Trusting it yields untranslatable nonsense,
    so we detect it and re-OCR. Signal: Latin-ish text whose tokens almost never
    contain a vowel. Guarded to Latin text so a genuine CJK page is never mis-flagged.
    """
    try:
        text = page.get_text()
    except Exception:
        return False
    if len(text) < 200:
        return False
    toks = [t for t in text.split() if len(t) >= 2]
    if len(toks) < 20:
        return False
    ascii_alpha = sum(1 for c in text if c.isascii() and c.isalpha())
    if ascii_alpha / len(text) < 0.30:        # mostly non-Latin (e.g. CJK) → don't judge
        return False
    vowelly = sum(1 for t in toks if any(c in "aeiouAEIOU" for c in t))
    return (vowelly / len(toks)) < 0.40


def _is_formula(block: Block) -> bool:
    math_chars = 0
    total = 0
    math_font_spans = 0
    span_total = 0
    for ln in block.lines:
        for sp in ln.spans:
            span_total += 1
            if _MATH_FONT_RE.search(sp.font):
                math_font_spans += 1
            for ch in sp.text:
                total += 1
                if ch in "=+−-×÷∑∏∫√≤≥≈≠∈∀∃∇∂αβγδεθλμπσφψω⟨⟩∞":
                    math_chars += 1
    if span_total == 0 or total == 0:
        return False
    return (math_font_spans / span_total > 0.6) or (math_chars / total > 0.18 and total < 80)


def _detect_inline_marks(block: Block) -> list[dict]:
    """Mark inline math / citation / url spans for the translator to mask.

    Coordinates are given as ``(kind, raw)`` — the translator masks by string.
    """
    marks: list[dict] = []
    text = block.text()
    for m in _URL_RE.finditer(text):
        marks.append({"kind": "url", "raw": m.group(0)})
    for m in _CITATION_RE.finditer(text):
        marks.append({"kind": "citation", "raw": m.group(0)})
    for m in _AUTHOR_YEAR_RE.finditer(text):
        marks.append({"kind": "citation", "raw": m.group(0)})
    # inline math from math-font spans (born-digital). Carry a best-effort LaTeX so
    # export can render it as a native inline equation; prose/PDF keep the raw text.
    from .mathrec import inline_unicode_to_latex
    for ln in block.lines:
        for sp in ln.spans:
            if _MATH_FONT_RE.search(sp.font) and sp.text.strip():
                raw = sp.text.strip()
                marks.append({"kind": "math", "raw": raw, "latex": inline_unicode_to_latex(raw)})
    # de-dup preserving order, longest first so nested matches mask cleanly
    seen: set[tuple[str, str]] = set()
    uniq: list[dict] = []
    for mk in sorted(marks, key=lambda m: -len(m["raw"])):
        key = (mk["kind"], mk["raw"])
        if key not in seen and len(mk["raw"]) >= 2:
            seen.add(key)
            uniq.append(mk)
    return uniq


# --------------------------------------------------------------------------- #
# reading order  (XY-cut within columns)
# --------------------------------------------------------------------------- #
def _order_blocks(blocks: list[Block], columns: list[Column],
                  rules: list[tuple[float, float, float]] | None = None) -> None:
    def column_of(b: Block) -> int:
        cx = (b.bbox[0] + b.bbox[2]) / 2
        for i, col in enumerate(columns):
            if col.x0 <= cx < col.x1:
                return i
        return len(columns) - 1

    # header/footer always read first/last within page, keep separate
    body = [b for b in blocks if b.type not in {"header", "footer"}]
    chrome = [b for b in blocks if b.type in {"header", "footer"}]

    body.sort(key=lambda b: (column_of(b), round(b.bbox[1] / 3), b.bbox[0]))
    order = 0
    for b in body:
        b.reading_order = order
        order += 1
    for b in sorted(chrome, key=lambda b: b.bbox[1]):
        b.reading_order = order
        order += 1

    # store blocks in reading order for downstream simplicity
    blocks.sort(key=lambda b: b.reading_order)

    # paragraph continuation linking (§2.3 ⑤): within page ordering
    _link_continuations(body, rules or [])


def _extract_rules(page: "fitz.Page") -> list[tuple[float, float, float]]:
    """Horizontal separator rules on the page as (y, x0, x1)."""
    rules: list[tuple[float, float, float]] = []
    try:
        for d in page.get_drawings():
            for it in d.get("items", []):
                if it[0] == "l":
                    p1, p2 = it[1], it[2]
                    if abs(p1.y - p2.y) < 1.0 and abs(p2.x - p1.x) > 40:
                        rules.append(((p1.y + p2.y) / 2, min(p1.x, p2.x), max(p1.x, p2.x)))
                elif it[0] == "re":
                    r = it[1]
                    if r.height < 3 and r.width > 40:
                        rules.append((r.y0, r.x0, r.x1))
    except Exception:  # noqa: BLE001 - best-effort
        pass
    return rules


def _rule_between(rules: list[tuple[float, float, float]], prev: Block, cur: Block) -> bool:
    top = min(prev.bbox[3], cur.bbox[3])
    bot = max(prev.bbox[1], cur.bbox[1])
    lo, hi = min(top, bot), max(top, bot)
    for ry, rx0, rx1 in rules:
        if lo - 1 <= ry <= hi + 1:
            x_overlap = min(prev.bbox[2], cur.bbox[2], rx1) - max(prev.bbox[0], cur.bbox[0], rx0)
            if x_overlap > 0:
                return True
    return False


def _link_continuations(ordered_body: list[Block],
                        rules: list[tuple[float, float, float]] | None = None) -> None:
    """Reconstruct paragraphs by chaining consecutive body blocks that are really
    lines of the same paragraph.

    Normal PDFs already group a paragraph into one block, so consecutive blocks are
    separated by paragraph spacing and won't chain.  Manuscripts (double-spaced,
    line-numbered) split every line into its own block; chaining them back into a
    paragraph is essential so the LLM sees coherent text and the renderer lays out
    one region per paragraph.

    A block continues the previous one when they share a column and style, sit at
    the normal line pitch (not a paragraph-sized gap), and the current block is not
    indented (a first-line indent marks a new paragraph).
    """
    ordered = sorted(ordered_body, key=lambda b: b.reading_order)
    if len(ordered) < 2:
        return

    lefts = sorted(b.bbox[0] for b in ordered)
    body_left = lefts[len(lefts) // 2]  # robust to a minority of indented first lines

    pitches = [
        ordered[i].bbox[1] - ordered[i - 1].bbox[1]
        for i in range(1, len(ordered))
        if 2 < (ordered[i].bbox[1] - ordered[i - 1].bbox[1]) < 80
    ]
    pitch = _median(pitches) if pitches else 14.0

    for i in range(1, len(ordered)):
        prev, cur = ordered[i - 1], ordered[i]
        if prev.type not in {"paragraph", "list"} or cur.type not in {"paragraph", "list"}:
            continue
        # A list marker ("a)", "b)", "①", "-") always begins a NEW item — it never
        # continues the previous block. Otherwise OCR'd standards glue each marker
        # onto the tail of the previous item and the whole list shifts.
        if cur.type == "list":
            continue
        # size ratio, not absolute: per-line detection is noisy (a subscript or
        # inline math pulls a line's median size down), so tolerate ~35% while a
        # real heading (much larger) still separates
        ps = prev.style_summary.font_size or 10.0
        cs = cur.style_summary.font_size or 10.0
        same_style = min(ps, cs) / max(ps, cs) > 0.65
        overlap = min(prev.bbox[2], cur.bbox[2]) - max(prev.bbox[0], cur.bbox[0])
        same_column = overlap > 0
        vgap = cur.bbox[1] - prev.bbox[1]
        adjacent = 0 < vgap <= pitch * 1.5              # one line down, not a paragraph break
        cur_indent = cur.bbox[0] > body_left + 10        # first-line indent -> new paragraph
        prev_indent = prev.bbox[0] > body_left + 10
        # Hanging-indent list continuation: lines under a list marker are indented to
        # align past the marker, so they are continuations, not new paragraphs. But a
        # line that RETURNS to the margin after an indented run starts a fresh block.
        list_continuation = (prev.type == "list" or prev_indent) and cur_indent
        returns_to_margin = prev_indent and not cur_indent
        separated = _rule_between(rules or [], prev, cur)  # a rule divides them
        clean_adjacent = (adjacent and not returns_to_margin
                          and (not cur_indent or list_continuation))
        # Math-heavy lines fragment: a paragraph carrying inline fractions / sub-
        # or superscripts (f'_c, l_d/d_b, s∕b) is split by the PDF text layer into
        # two blocks whose line boxes OVERLAP vertically instead of stacking at the
        # clean line pitch, so the top-to-top adjacency test above never rejoins
        # them. Flowed as two TUs they stack two Korean paragraphs in the same band
        # (the "중첩/겹침" overprint). Rejoin when the current block begins *inside*
        # the previous block's vertical span in the same column.
        vertical_overlap = prev.bbox[1] <= cur.bbox[1] < prev.bbox[3] - 2
        if (same_style and same_column and not separated
                and (clean_adjacent or vertical_overlap)):
            cur.continues = prev.id


_REFERENCES_HEADING_RE = re.compile(r"^\s*(references|bibliography|참고\s*문헌)\s*$", re.I)

# Journal front-matter / footer metadata (masthead, corresponding-author note,
# DOI, copyright, ISSN, submission dates). Not body prose — kept in the original
# language so it stays visually separate instead of being translated and merged
# into the body flow.
_FRONTMATTER_RE = re.compile(
    r"(corresponding\s+author|e-?mail\s+address(?:es)?|doi\.org|doi:\s|"
    r"contents\s+lists\s+available|journal\s+homepage|sciencedirect|"
    r"all\s+rights\s+reserved|available\s+online|"
    r"received\s+\d|revised\s+\d|accepted\s+\d|©\s*\d{4}|\b\d{4}-\d{3}[\dxX]\b)",
    re.I,
)


def _mark_frontmatter_metadata(document: "Document") -> None:
    """Keep journal masthead/footer metadata (corresponding author, DOI, copyright,
    ISSN, submission dates) in the original — translating it merges it into the body
    and drops the visual separation readers expect."""
    for blk in document.all_blocks():
        if not blk.translate or blk.type == "reference":
            continue
        text = blk.text().strip()
        m = _FRONTMATTER_RE.search(text)
        # Only a short standalone metadata line (DOI/copyright/corresponding author),
        # or one whose marker is right at the start, counts. Otherwise a long abstract
        # that merely ends with "[DOI: …]" would be wrongly excluded from translation.
        if m and (len(text) <= 220 or m.start() <= 30):
            blk.translate = False
            blk.continues = None


_TABLE_CAP_RE = re.compile(r"^\s*(?:table|tab\.?|표)\s*\d", re.I)


def _looks_like_table_row(blk: "Block") -> bool:
    """A borderless-table data row: several space-separated tokens, mostly data
    (numbers / short codes), not running prose."""
    toks = blk.text().split()
    if len(toks) < 3:
        return False
    lower_words = sum(1 for w in toks if len(w) >= 3 and w.isalpha() and w[0].islower())
    if lower_words >= 4:
        return False                                  # a prose sentence, not a row
    numeric = sum(1 for w in toks if any(c.isdigit() for c in w))
    return numeric >= 2 or len(toks) >= 6


def _mark_table_data(document: "Document") -> None:
    """Keep borderless-table data rows in the original so their column alignment is
    preserved. Each table row is one text block whose cells are separated by spacing;
    translating it collapses that spacing and jumbles the table. The renderer leaves
    translate=False blocks untouched, so the table keeps the source layout — only the
    caption is translated. Rows sit just below a 'Table N' caption, in its column."""
    for page in document.pages:
        caps = [b for b in page.blocks if b.type == "caption" and _TABLE_CAP_RE.match(b.text())]
        for cap in caps:
            cx0, cy0, cx1, _cy1 = cap.bbox
            col_lo, col_hi = cx0 - 20, cx0 + max(cx1 - cx0, 240) + 40
            for b in page.blocks:
                if b is cap or not b.translate or b.type in ("heading", "title", "caption"):
                    continue
                bx0, by0, bx1, _by1 = b.bbox
                bxc = (bx0 + bx1) / 2
                if not (cy0 - 4 <= by0 <= cy0 + 190 and col_lo <= bxc <= col_hi):
                    continue
                if _looks_like_table_row(b):
                    b.translate = False
                    b.continues = None


_MDPI_META_RE = re.compile(
    r"(citation:|academic\s+editor|received:|accepted:|published:|publisher.?s\s+note|"
    r"licensee|creative\s+commons|open\s+access|copyright:|©\s*\d)", re.I)


def _mark_mdpi_sidebar(document: "Document") -> None:
    """Keep the MDPI front-page furniture in the original: the journal-name masthead
    logo (a brand, not to be translated) and the narrow left metadata sidebar
    (Citation, Academic Editor, Received/Accepted/Published, Publisher's Note,
    copyright/licence). Translating these breaks the page and adds nothing."""
    if not document.pages:
        return
    page = document.pages[0]
    w, h = page.width, page.height
    # masthead journal-name logo at the very top → brand, keep original
    for b in page.blocks:
        if b.type == "title" and b.bbox[1] < 0.09 * h and (b.bbox[2] - b.bbox[0]) < 0.4 * w:
            b.translate = False
            b.continues = None
    # narrow left metadata sidebar (only when it really carries MDPI metadata)
    left = [b for b in page.blocks if b.translate and b.bbox[2] < 0.30 * w]
    has_main = any(b.bbox[2] > 0.60 * w for b in page.blocks)   # a wide main column exists
    if len(left) >= 4 and has_main and any(_MDPI_META_RE.search(b.text()) for b in left):
        for b in left:
            b.translate = False
            b.continues = None


def _mark_references_document(document: "Document") -> None:
    """Document-level pass: once the References/Bibliography heading is seen, every
    following non-chrome block belongs to the reference list — even across page
    boundaries — and is excluded from translation (author names, titles, DOIs).

    Per-page classification cannot see this because the heading and its entries
    span multiple pages.
    """
    ordered = sorted(document.all_blocks(), key=lambda b: (b.page, b.reading_order))
    in_refs = False
    for blk in ordered:
        if not in_refs:
            if blk.type in {"heading", "title"} and _REFERENCES_HEADING_RE.match(blk.text()):
                in_refs = True
            continue
        # inside the reference section
        if blk.type in {"header", "footer"}:
            continue
        blk.type = "reference"
        blk.translate = False
        blk.continues = None


# --------------------------------------------------------------------------- #
# small utilities
# --------------------------------------------------------------------------- #
def _median(xs: list[float]) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    n = len(s)
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2


def _is_serif(font: str) -> bool:
    f = font.lower()
    return any(k in f for k in ("times", "serif", "roman", "georgia", "minion", "nimbusrom"))


def _int_to_hex(color: int) -> str:
    try:
        r = (color >> 16) & 255
        g = (color >> 8) & 255
        b = color & 255
        return f"#{r:02x}{g:02x}{b:02x}"
    except Exception:  # noqa: BLE001
        return "#000000"


def _doc_id(pdf_path: Path) -> str:
    h = hashlib.sha256()
    with open(pdf_path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return "d_" + h.hexdigest()[:12]
