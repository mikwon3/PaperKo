"""renderer.py — layout-preserving PDF re-render (03 §6).

Redacts the original text of translated blocks and flows the Korean translation
into the same bounding box using PyMuPDF's HTML box engine, embedding a Korean
font subset.  Overflow is handled with the documented 4-step fitting strategy
(shrink font → tighten leading → expand box → mark overflow).
"""

from __future__ import annotations

import base64
import html as html_lib
import io
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf as fitz  # PyMuPDF (modern import; avoids the legacy `fitz` deprecation warning)

from .errors import EngineError, RESOURCE_MISSING
from .ir import Block, Document, TranslationResult

# Inline math the LLM emitted as LaTeX: $ … $  or  \( … \)
_INLINE_MATH_RE = re.compile(r"\$(?!\s)([^$\n]+?)\$|\\\(([^\n]*?)\\\)")
_MATH_PNG_CACHE: dict[tuple[str, str], str | None] = {}
_MATHTEXT_OK = True  # flipped off if matplotlib is unavailable/fails to import


def _latex_png_datauri(latex: str, color: str) -> str | None:
    """Render a LaTeX math snippet to a tight transparent PNG (matplotlib mathtext,
    no LaTeX install needed) and return a data: URI. None if it can't be rendered
    (caller then falls back to plain text)."""
    global _MATHTEXT_OK
    if not _MATHTEXT_OK or not latex.strip():
        return None
    key = (latex, color)
    if key in _MATH_PNG_CACHE:
        return _MATH_PNG_CACHE[key]
    uri: str | None = None
    try:
        import matplotlib
        matplotlib.use("Agg")
        from matplotlib.figure import Figure

        fig = Figure(figsize=(0.01, 0.01))
        fig.text(0, 0, f"${latex}$", fontsize=16, color=color or "#000000")
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=200, transparent=True,
                    bbox_inches="tight", pad_inches=0.01)
        uri = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
    except ImportError:
        _MATHTEXT_OK = False
        log.info("matplotlib not available — inline math kept as text")
    except Exception as exc:  # noqa: BLE001 - unparseable LaTeX → text fallback
        log.debug("mathtext render failed for %r: %s", latex, exc)
        uri = None
    _MATH_PNG_CACHE[key] = uri
    return uri


# Common LaTeX symbols → Unicode, so leaked commands don't show as raw `\leq` etc.
_LATEX_SYMS = {
    r"\leq": "≤", r"\le": "≤", r"\geq": "≥", r"\ge": "≥", r"\neq": "≠", r"\ne": "≠",
    r"\approx": "≈", r"\sim": "∼", r"\times": "×", r"\cdot": "·", r"\div": "÷",
    r"\pm": "±", r"\mp": "∓", r"\to": "→", r"\rightarrow": "→", r"\leftarrow": "←",
    r"\Rightarrow": "⇒", r"\infty": "∞", r"\partial": "∂", r"\nabla": "∇", r"\degree": "°",
    r"\alpha": "α", r"\beta": "β", r"\gamma": "γ", r"\delta": "δ", r"\epsilon": "ε",
    r"\varepsilon": "ε", r"\zeta": "ζ", r"\eta": "η", r"\theta": "θ", r"\lambda": "λ",
    r"\mu": "μ", r"\nu": "ν", r"\xi": "ξ", r"\pi": "π", r"\rho": "ρ", r"\sigma": "σ",
    r"\tau": "τ", r"\phi": "φ", r"\varphi": "φ", r"\psi": "ψ", r"\omega": "ω",
    r"\Delta": "Δ", r"\Sigma": "Σ", r"\Omega": "Ω", r"\Phi": "Φ", r"\Theta": "Θ",
    r"\%": "%", r"\&": "&", r"\_": "_", r"\#": "#", r"\{": "{", r"\}": "}",
    r"\,": " ", r"\;": " ", r"\:": " ", r"\!": "", r"\quad": " ", r"\qquad": "  ",
}
_LATEX_CMD_RE = re.compile(r"\\(?:text|mathrm|mathbf|mathit|mathsf|operatorname|textrm|textbf)\s*\{([^{}]*)\}")
_LATEX_SYM_RE = re.compile("|".join(re.escape(k) for k in sorted(_LATEX_SYMS, key=len, reverse=True)))


def _delatex(text: str) -> str:
    """Turn common leaked LaTeX (\\text{ mm}, \\leq, \\alpha …) into plain Unicode so
    it reads naturally when inline-math rendering is off."""
    text = _LATEX_CMD_RE.sub(r"\1", text)             # \text{ mm} → " mm"
    text = _LATEX_SYM_RE.sub(lambda m: _LATEX_SYMS[m.group(0)], text)
    return text


_SUBSUP_RE = re.compile(r"([_^])\{([^{}]*)\}")


def _strip_math_delims(text: str) -> str:
    """When inline-math rendering is off: drop the $ / \\( \\) delimiters (keeping the
    inner content) and normalize leaked LaTeX to Unicode, so no `$`/`\\leq` shows."""
    if "$" in text or "\\(" in text:
        text = _INLINE_MATH_RE.sub(lambda m: m.group(1) if m.group(1) is not None else m.group(2), text)
    if "\\" in text:
        text = _delatex(text)
    # Subscript/superscript braces the model emits without a backslash
    # (e.g. "f_{fu}" → "f_fu", "x^{2}" → "x^2") — no delimiter, so handled here.
    if "_{" in text or "^{" in text:
        text = _SUBSUP_RE.sub(r"\1\2", text)
    return text


def _inline_math_html(text: str, font_px: float, color: str) -> str:
    """Escape body text, converting $…$ / \\(…\\) spans into inline math images so
    the LLM's LaTeX shows as rendered equations instead of literal `$x$`."""
    if "$" not in text and "\\(" not in text:
        return html_lib.escape(text)
    out: list[str] = []
    pos = 0
    for m in _INLINE_MATH_RE.finditer(text):
        out.append(html_lib.escape(text[pos:m.start()]))
        latex = m.group(1) if m.group(1) is not None else m.group(2)
        uri = _latex_png_datauri(latex, color)
        if uri:
            h = font_px * 1.15
            out.append(f'<img src="{uri}" style="height:{h:.1f}px;vertical-align:-15%;">')
        else:
            out.append(html_lib.escape(latex))  # fallback: drop $, keep content
        pos = m.end()
    out.append(html_lib.escape(text[pos:]))
    return "".join(out)

# A genuine heading opens with a section number (1., 2.3, 3.1.1), a list/figure/table
# label, or a short standalone word — used to tell real headings from body lines that
# layout mislabeled as headings (which are long prose and don't start this way).
_HEADING_LABEL_RE = re.compile(
    r"^\s*("
    r"\d+(\.\d+)*\.?(\s|$)"          # 1   2.3   3.1.1.
    r"|\d+\)"                         # 1)
    r"|[•·▪◦\-–—]\s"                  # bullet
    r"|제?\s*\d+\s*(장|절|항)"          # 제2장, 3절
    r"|(표|그림|부록)\s*\d"             # 표 1, 그림 2, 부록 A
    r"|(Table|Fig\.?|Figure|Appendix|Section|Chapter|Eq\.?)\s*\d"
    r")",
    re.IGNORECASE,
)

log = logging.getLogger("translate_engine.renderer")

# Candidate Korean fonts per family, in preference order. Relative names resolve
# under font_dir (the bundled `fonts/` folder by default), so a packaged app ships
# an open-licence font (SIL OFL) that is freely embeddable in the output PDF —
# unlike the OS system fonts, which are only a last-resort fallback.
_FONT_CANDIDATES = {
    # Nanum Gothic (sans) — bundled default
    "nanum": [
        "NanumGothic-Regular.ttf",
    ],
    # Nanum Myeongjo (serif) — for serif source documents
    "nanum-myeongjo": [
        "NanumMyeongjo-Regular.ttf",
        "NanumGothic-Regular.ttf",
    ],
    # Noto Sans KR if the user drops it into fonts/, else the bundled Nanum
    "noto": [
        "NotoSansKR-Regular.otf",
        "NotoSansKR-Regular.ttf",
        "NanumGothic-Regular.ttf",
    ],
    # CJK targets — bundled Noto Sans (static TTF; CFF OTF misrenders in htmlbox)
    "jp": [
        "NotoSansJP-Regular.ttf",
        "NanumGothic-Regular.ttf",
    ],
    "sc": [
        "NotoSansSC-Regular.ttf",
        "NanumGothic-Regular.ttf",
    ],
}

# Target-language → font family (CJK targets need a CJK font; others use the setting).
_CJK_FONT_FOR_TARGET = {
    "ja": "jp", "japanese": "jp", "일본어": "jp",
    "zh": "sc", "chinese": "sc", "중국어": "sc", "zh-cn": "sc", "zh_cn": "sc",
}
# Last-resort fallbacks tried in order across platforms (only if no bundled/named
# font resolves — e.g. a stripped-down build).
_SYSTEM_FALLBACKS = [
    "C:/Windows/Fonts/malgun.ttf",
    "/System/Library/Fonts/Supplemental/AppleGothic.ttf",
    "/usr/share/fonts/truetype/nanum/NanumGothic.ttf",
]
# Bundled fonts directory (shipped with the package).
_BUNDLED_FONT_DIR = Path(__file__).resolve().parent / "fonts"


@dataclass
class RenderOptions:
    mode: str = "replace"                 # replace | interleaved
    font_family: str = "noto"             # noto | nanum
    min_font_scale: float = 0.55          # allow more shrink so expanding targets
    #                                        (e.g. KO→EN) fit tight cells instead of overflowing
    mark_machine_translated: bool = True
    preview_png: bool = False
    preview_dpi: int = 110
    render_inline_math: bool = False      # $…$ → equation image (off: strip $ delimiters)
    target_lang: str = "Korean"           # picks a CJK font for Japanese/Chinese targets


@dataclass
class RenderReport:
    pages: int = 0
    fitted: int = 0
    shrunk: int = 0
    expanded: int = 0
    overflowed: int = 0
    failed_kept: int = 0
    overflow_blocks: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "pages": self.pages,
            "blocks": {
                "fitted": self.fitted,
                "shrunk": self.shrunk,
                "expanded": self.expanded,
                "overflowed": self.overflowed,
                "failed_kept": self.failed_kept,
            },
            "overflow_blocks": self.overflow_blocks,
        }


class Renderer:
    def __init__(self, font_dir: Path | None = None, opts: RenderOptions | None = None) -> None:
        # Default to the bundled OFL fonts so output PDFs embed a freely
        # redistributable Korean font on any platform.
        self.font_dir = Path(font_dir) if font_dir else _BUNDLED_FONT_DIR
        self.opts = opts or RenderOptions()
        # CJK targets (Japanese/Chinese) need a CJK font regardless of the font setting.
        family = _CJK_FONT_FOR_TARGET.get((self.opts.target_lang or "").strip().lower(), self.opts.font_family)
        self._font_path = self._resolve_font(family)
        self._archive = fitz.Archive()
        self._archive.add(str(self._font_path), "kfont")
        self._css = self._base_css()

    # ------------------------------------------------------------------ #
    def _resolve_font(self, family: str) -> Path:
        candidates = _FONT_CANDIDATES.get(family, [])
        for c in candidates:
            p = Path(c)
            if not p.is_absolute() and self.font_dir:
                p = self.font_dir / c
            if p.exists():
                return p
        for fb in _SYSTEM_FALLBACKS:
            if Path(fb).exists():
                log.warning("Korean font %r not found; using system fallback %s", family, fb)
                return Path(fb)
        raise EngineError(
            RESOURCE_MISSING,
            "no Korean font available for rendering (install Noto Sans KR / Malgun Gothic / Nanum)",
        )

    def _base_css(self) -> str:
        return (
            "@font-face { font-family: kfont; src: url(kfont); }\n"
            "* { font-family: kfont; }\n"
            "p { margin: 0; padding: 0; }\n"
        )

    def _is_translated(self, text: str | None) -> bool:
        """Whether a TU's target text is a real translation in the target language
        (so we replace the original) — language-aware, not Korean-only."""
        if not text or not text.strip():
            return False
        t = (self.opts.target_lang or "").strip().lower()
        if t in ("ko", "korean", "한국어"):
            return _has_hangul(text)
        if t in ("ja", "japanese", "일본어", "zh", "chinese", "중국어", "zh-cn", "zh_cn"):
            return _has_cjk(text)
        return True  # Latin-script targets (English, …): any non-empty output

    # ------------------------------------------------------------------ #
    def render(
        self,
        src_pdf: Path,
        doc: Document,
        tr: TranslationResult,
        out_path: Path,
        *,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> RenderReport:
        src_pdf = Path(src_pdf)
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)

        replaced = fitz.open(src_pdf)
        tu_by_block = _index_translations(doc, tr)
        report = RenderReport(pages=replaced.page_count)

        # One document-wide body size so body text renders at a uniform size
        # (per-block detected sizes carry extraction noise).
        self._body_size = _dominant_body_size(doc)

        # Map by absolute page index (doc.pages may hold only a subset when a
        # page range was analyzed) so translations land on the correct pages.
        ir_by_index = {p.index: p for p in doc.pages}

        total = replaced.page_count
        for pi in range(total):
            page = replaced.load_page(pi)
            ir_page = ir_by_index.get(pi)
            if ir_page is not None:
                self._render_page(page, ir_page, tu_by_block, report)
            if on_progress:
                on_progress(pi + 1, total)

        if self.opts.mark_machine_translated:
            _mark_machine_translated(replaced)

        if self.opts.mode == "interleaved":
            final = _interleave(src_pdf, replaced)
            replaced.close()
            final.save(str(out_path), garbage=4, deflate=True)
            final.close()
        else:
            replaced.save(str(out_path), garbage=4, deflate=True)
            replaced.close()

        if self.opts.preview_png:
            self._render_previews(out_path)

        return report

    # ------------------------------------------------------------------ #
    def _render_page(self, page, ir_page, tu_by_block, report: RenderReport) -> None:
        rules = _horizontal_rules(page)  # separator lines act as expansion barriers
        # Group the page's translated blocks by their TU so a paragraph split
        # across several line-blocks (e.g. double-spaced manuscripts) is redrawn
        # once into the union of those blocks — not duplicated into each line box.
        groups: dict[str, dict] = {}
        failed_blocks: list[Block] = []
        for blk in ir_page.blocks:
            if not blk.translate:
                continue
            tu = tu_by_block.get(blk.id)
            if tu is None:
                continue
            if tu.status == "done" and self._is_translated(tu.target_text):
                g = groups.setdefault(tu.id, {"tu": tu, "blocks": []})
                g["blocks"].append(blk)
            elif tu.status == "failed":
                failed_blocks.append(blk)

        # Guard against overlapping translations: a scanned/OCR'd page sometimes
        # yields a small block sitting *inside* a larger block of a different TU —
        # a double-captured paragraph (the same text grabbed twice) or a heading
        # sandwiched between two paragraph blocks that one TU then spans. Flowing
        # both would stack two Korean paragraphs in the same space (the "여러 줄이
        # 겹쳐보임" overlap). Keep the larger region; mark the smaller contained one
        # so its source is still redacted but its translation is not drawn again.
        ordered = sorted(
            groups.values(),
            key=lambda g: _rect_area(_union_bbox([b.bbox for b in g["blocks"]])),
            reverse=True,
        )
        kept_rects: list[list[float]] = []
        for g in ordered:
            u = _union_bbox([b.bbox for b in g["blocks"]])
            area = _rect_area(u) or 1.0
            contained = any(_intersect_area(u, k) / area >= 0.55 for k in kept_rects)
            g["draw"] = not contained
            if contained:
                log.debug("suppressing overlapping TU %s (inside a larger region)", g["tu"].id)
            else:
                kept_rects.append(u)

        # 1. redact the original text of every constituent block.
        #    On a scanned page (OCR text layer over a full-page image, e.g. an
        #    OCR'd standard) a normal redaction leaves the scan untouched, and OCR
        #    bboxes are often narrower than the visible glyphs — so heading tails
        #    ("…Materials" → a stray "ls") bleed past the Korean. Blank the image
        #    pixels, and widen short/heading blocks rightward to the column edge so
        #    the scanned glyphs beyond the OCR box are covered too.
        scanned = _is_scanned_page(page, ir_page)
        for g in groups.values():
            for blk in g["blocks"]:
                rrect = fitz.Rect(blk.bbox)
                if scanned:
                    rrect = self._scan_redact_rect(rrect, blk, ir_page)
                page.add_redact_annot(rrect, fill=(1, 1, 1))
        if groups:
            page.apply_redactions(
                images=fitz.PDF_REDACT_IMAGE_PIXELS if scanned else fitz.PDF_REDACT_IMAGE_NONE
            )

        # 2. flow each TU's translation once into the union region of its blocks
        #    (skipping groups suppressed above as overlapping duplicates)
        for g in groups.values():
            if not g.get("draw", True):
                continue
            blocks = g["blocks"]
            union = _union_bbox([b.bbox for b in blocks])
            exclude = {b.id for b in blocks}
            outcome = self._fit_region(page, fitz.Rect(union), blocks[0], g["tu"].target_text,
                                       ir_page, exclude, rules)
            if outcome == "fitted":
                report.fitted += 1
            elif outcome == "shrunk":
                report.shrunk += 1
            elif outcome == "expanded":
                report.expanded += 1
            else:
                report.overflowed += 1
                report.overflow_blocks.append({"page": blocks[0].page, "block_id": blocks[0].id})

        # 3. failed TUs: keep source + faint orange highlight
        for blk in failed_blocks:
            annot = page.add_highlight_annot(fitz.Rect(blk.bbox))
            annot.set_colors(stroke=(1.0, 0.8, 0.4))
            annot.set_info(content="번역 실패 — 원문 유지")
            annot.update()
            report.failed_kept += 1

    def _fit_region(self, page, rect: fitz.Rect, style_block: Block, target: str,
                    ir_page, exclude_ids: set[str], rules: list | None = None) -> str:
        """Fit strategy that prefers a UNIFORM font size: keep the normalized size
        and use space (expand the box) before shrinking, so paragraphs across the
        document render at the same size.  Shrinking is a last resort."""
        page_size = (ir_page.width, ir_page.height)
        # Cap line-height: double-spaced manuscripts report ~2.0, which makes the
        # Korean tall enough to expand into (and overlap) the next region.
        lh = min(1.5, max(1.2, style_block.style_summary.line_height))
        html = self._html(target, style_block, lh)

        # 1. no shrink, original box
        spare, _ = self._measure(html, rect, page_size, 1.0)
        if spare >= 0:
            self._draw(page, rect, html, 1.0)
            return "fitted"

        # 2. no shrink, expand into the available gap (down, and right for short
        #    blocks like headings whose translation is wider than the original box)
        expanded = self._expand_rect(rect, ir_page, exclude_ids, rules)
        if expanded.width > rect.width + 1 or expanded.height > rect.height + 1:
            spare, _ = self._measure(html, expanded, page_size, 1.0)
            if spare >= 0:
                self._draw(page, expanded, html, 1.0)
                return "expanded"

        # 3. last resort: tighter leading + allow shrink to min_font_scale
        html2 = self._html(target, style_block, 1.12)
        spare, scale = self._measure(html2, expanded, page_size, self.opts.min_font_scale)
        if spare >= 0:
            self._draw(page, expanded, html2, self.opts.min_font_scale)
            return "shrunk" if scale < 0.985 else "fitted"

        # 4. overflow: draw at min scale + attach full text as annotation
        self._draw(page, expanded, html2, self.opts.min_font_scale)
        note = page.add_text_annot(fitz.Point(expanded.x1 - 8, expanded.y0), target)
        note.set_info(content=target)
        note.update()
        _draw_overflow_marker(page, expanded)
        return "overflowed"

    def _expand_rect(self, rect: fitz.Rect, ir_page, exclude_ids: set[str],
                     rules: list | None = None) -> fitz.Rect:
        # find nearest obstacle below in the same horizontal band: another text
        # block OR a horizontal separator rule (so text never grows over a line)
        limit = ir_page.height * 0.97
        for other in ir_page.blocks:
            if other.id in exclude_ids:
                continue
            ox0, oy0, ox1, _oy1 = other.bbox
            if min(rect.x1, ox1) - max(rect.x0, ox0) > 0 and oy0 >= rect.y1 - 1:
                limit = min(limit, oy0 - 2)
        for ry, rx0, rx1 in (rules or []):
            if ry >= rect.y1 - 1 and min(rect.x1, rx1) - max(rect.x0, rx0) > 0:
                limit = min(limit, ry - 2)
        limit = max(limit, rect.y1)

        # find nearest obstacle to the RIGHT in the same vertical band, so a short
        # block (e.g. a one-word heading like "총평"→"Overall Review") whose
        # translation is wider than the original narrow box can grow rightward into
        # the empty margin instead of overflowing. Bounded by the next block/rule so
        # it never overlaps a neighbouring column or table cell.
        right = ir_page.width * 0.97
        for other in ir_page.blocks:
            if other.id in exclude_ids:
                continue
            ox0, oy0, ox1, oy1 = other.bbox
            if min(rect.y1, oy1) - max(rect.y0, oy0) > 0 and ox0 >= rect.x1 - 1:
                right = min(right, ox0 - 2)
        for ry, rx0, rx1 in (rules or []):
            if min(rect.y1, ry) - max(rect.y0, ry) >= 0 and rx0 >= rect.x1 - 1:
                right = min(right, rx0 - 2)
        right = max(right, rect.x1)
        return fitz.Rect(rect.x0, rect.y0, right, limit)

    def _scan_redact_rect(self, rect: fitz.Rect, blk: Block, ir_page) -> fitz.Rect:
        """Widen a scanned-page heading's redaction box rightward to the next block
        or column edge, so scanned glyphs beyond the (narrow) OCR bbox get blanked
        too. Right-only + a small vertical pad; never grows down into other text."""
        right = ir_page.width * 0.97
        for other in ir_page.blocks:
            if other.id == blk.id:
                continue
            ox0, oy0, ox1, oy1 = other.bbox
            if min(rect.y1, oy1) - max(rect.y0, oy0) > 0 and ox0 >= rect.x1 - 1:
                right = min(right, ox0 - 2)
        right = max(right, rect.x1)
        return fitz.Rect(rect.x0, rect.y0 - 1.5, right, rect.y1 + 1.5)

    # ------------------------------------------------------------------ #
    def _looks_like_prose_heading(self, blk: Block, text: str | None = None) -> bool:
        """True when a block tagged *heading* carries running body prose (a line that
        layout split off and mislabeled), so it should render at body size, not with
        the heading bump.  A genuine heading is short or starts with a section number
        / label; mislabeled body lines are long prose or trail off mid-sentence."""
        if blk.type != "heading":
            return False
        s = (text or "").strip()
        if not s:
            return False
        if s.endswith((",", "，", "…", ";", "·", "、")):  # trails mid-sentence
            return True
        # a Korean verb predicate (…한다. …이다. …않는다) is a sentence, never a heading
        # (headings are noun phrases); "다" is the reliable signal — nominal endings
        # like 음·함 are skipped, since many nouns end that way (이음, 그림, 모음).
        if s.rstrip(".").endswith("다") and not _HEADING_LABEL_RE.match(s):
            return True
        # long and not opening with a section number / list / figure-table label
        return len(s) > 34 and not _HEADING_LABEL_RE.match(s)

    def _looks_like_paragraph(self, blk: Block, text: str | None = None) -> bool:
        """True when a block tagged heading/title/caption is really body text that
        layout analysis mislabeled — a genuine heading is short (a line or two) and
        set in a larger face.  We judge by the SOURCE geometry (independent of how
        long the translation turns out), with the translated length as a backstop."""
        fs = blk.style_summary.font_size or getattr(self, "_body_size", 10.0) or 10.0
        _x0, y0, _x1, y1 = blk.bbox
        lines_tall = max(0.0, y1 - y0) / max(1.0, fs * 1.25)
        if lines_tall > 3.0:            # taller than ~3 lines → a paragraph, not a heading
            return True
        if text and len(text.strip()) > 120:  # long run of prose → not a heading
            return True
        return False

    def _norm_size(self, blk: Block, text: str | None = None) -> float:
        """Font size normalized by role, so all body text is uniform and headings
        differ by a fixed step rather than by noisy per-block detected sizes.

        Guards against the common layout-analysis error of tagging a multi-line body
        paragraph as a heading/title: it would otherwise render 12–70% larger than
        the surrounding text and make the page font look uneven. The guard keys on
        block SHAPE (a genuine heading is only a line or two), not on the detected
        font size — which is unreliable on OCR'd pages where every block measures
        near the body size, so a size-ratio test would wrongly demote real headings."""
        body = getattr(self, "_body_size", 0.0) or blk.style_summary.font_size or 10.0
        t = blk.type
        if t in ("title", "heading"):
            if self._looks_like_paragraph(blk, text) or self._looks_like_prose_heading(blk, text):
                return round(body, 1)  # mislabeled paragraph/body line → uniform body size
            if t == "title":
                return round(body * 1.7, 1)
            ratio = (blk.style_summary.font_size / body) if body else 1.0
            return round(body * (1.3 if ratio >= 1.3 else 1.12), 1)
        if t == "caption":
            return round(body * 0.92, 1)
        return round(body, 1)  # paragraph / list / reference / footnote

    def _html(self, text: str, blk: Block, line_height: float) -> str:
        st = blk.style_summary
        font_px = self._norm_size(blk, text)
        if self.opts.render_inline_math:
            esc = _inline_math_html(text, font_px, st.color or "#000000")
        else:
            esc = html_lib.escape(_strip_math_delims(text))
        if st.bold:
            esc = f"<b>{esc}</b>"
        if st.italic:
            esc = f"<i>{esc}</i>"
        align = st.align if st.align in {"left", "right", "center", "justify"} else "left"
        # Body paragraphs/lists are never centered: alignment detection sometimes
        # mislabels a full-width justified line as "center" (near-symmetric page
        # margins), which renders the translated paragraph centered and looks
        # broken. Only genuine headings/titles/captions may center.
        if align == "center" and blk.type in {"paragraph", "list", "reference", "footnote"}:
            align = "left"
        style = (
            f"font-size:{font_px:.1f}px;"
            f"line-height:{line_height:.2f};"
            f"text-align:{align};"
            f"color:{st.color};"
        )
        return f'<p style="{style}">{esc}</p>'

    def _measure(self, html: str, rect: fitz.Rect, page_size, scale_low: float) -> tuple[float, float]:
        """Fit-test on a throwaway page so we never double-draw on the real one."""
        tmp = fitz.open()
        tp = tmp.new_page(width=page_size[0], height=page_size[1])
        try:
            return tp.insert_htmlbox(rect, html, css=self._css, archive=self._archive,
                                     scale_low=scale_low)
        finally:
            tmp.close()

    def _draw(self, page, rect: fitz.Rect, html: str, scale_low: float) -> None:
        page.insert_htmlbox(rect, html, css=self._css, archive=self._archive, scale_low=scale_low)

    # ------------------------------------------------------------------ #
    def _render_previews(self, out_path: Path) -> None:
        prev_dir = out_path.parent / "preview"
        prev_dir.mkdir(exist_ok=True)
        doc = fitz.open(out_path)
        try:
            zoom = self.opts.preview_dpi / 72.0
            mat = fitz.Matrix(zoom, zoom)
            for pi in range(doc.page_count):
                pix = doc.load_page(pi).get_pixmap(matrix=mat)
                pix.save(str(prev_dir / f"page-{pi:03d}.png"))
        finally:
            doc.close()


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _index_translations(doc: Document, tr: TranslationResult) -> dict:
    tu_by_id = tr.tu_map()
    # a TU can cover several blocks; map each block to its TU
    out: dict = {}
    for tu in doc.translation_units or tr.translation_units:
        cur = tu_by_id.get(tu.id, tu)
        for bid in tu.block_ids:
            out[bid] = cur
    return out


def _dominant_body_size(doc: Document) -> float:
    """Median font size of translatable body blocks — the uniform body size."""
    sizes = [
        b.style_summary.font_size
        for b in doc.all_blocks()
        if b.translate and b.type in {"paragraph", "list"} and b.style_summary.font_size > 0
    ]
    if not sizes:
        sizes = [b.style_summary.font_size for b in doc.all_blocks() if b.style_summary.font_size > 0]
    if not sizes:
        return 10.0
    sizes.sort()
    return round(sizes[len(sizes) // 2], 1)


def _horizontal_rules(page) -> list[tuple[float, float, float]]:
    """Horizontal separator lines on the page (as (y, x0, x1)).  Text must not be
    expanded across these, or it ends up overlapping the rule (e.g. the section
    dividers on a journal first page)."""
    rules: list[tuple[float, float, float]] = []
    try:
        for d in page.get_drawings():
            for it in d.get("items", []):
                if it[0] == "l":  # line segment
                    p1, p2 = it[1], it[2]
                    if abs(p1.y - p2.y) < 1.0 and abs(p2.x - p1.x) > 40:
                        rules.append(((p1.y + p2.y) / 2, min(p1.x, p2.x), max(p1.x, p2.x)))
                elif it[0] == "re":  # thin rectangle acting as a rule
                    r = it[1]
                    if r.height < 3 and r.width > 40:
                        rules.append((r.y0, r.x0, r.x1))
    except Exception:  # noqa: BLE001 - drawings are best-effort
        pass
    return rules


def _is_scanned_page(page, ir_page) -> bool:
    """True when one image covers most of the page — an OCR'd scan whose text layer
    sits over a picture, so redaction must blank image pixels, not just text."""
    try:
        imgs = page.get_images(full=True)
    except Exception:  # noqa: BLE001
        return False
    page_area = max(ir_page.width * ir_page.height, 1.0)
    for img in imgs:
        try:
            for r in page.get_image_rects(img[0]):
                if r.width * r.height >= 0.6 * page_area:
                    return True
        except Exception:  # noqa: BLE001
            continue
    return False


def _union_bbox(bboxes: list[list[float]]) -> list[float]:
    return [
        min(b[0] for b in bboxes), min(b[1] for b in bboxes),
        max(b[2] for b in bboxes), max(b[3] for b in bboxes),
    ]


def _rect_area(b: list[float]) -> float:
    return max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])


def _intersect_area(a: list[float], b: list[float]) -> float:
    ix = min(a[2], b[2]) - max(a[0], b[0])
    iy = min(a[3], b[3]) - max(a[1], b[1])
    return max(0.0, ix) * max(0.0, iy)


def _has_hangul(text: str) -> bool:
    return any("가" <= c <= "힣" for c in text)


def _has_cjk(text: str) -> bool:
    """True if the text contains Japanese kana or CJK ideographs."""
    return any(
        "぀" <= c <= "ヿ"   # hiragana + katakana
        or "㐀" <= c <= "鿿"  # CJK unified ideographs
        or "豈" <= c <= "﫿"  # CJK compatibility ideographs
        for c in text
    )


def _mark_machine_translated(doc: fitz.Document) -> None:
    meta = doc.metadata or {}
    meta["subject"] = (meta.get("subject") or "") + " [기계번역본 / machine-translated]"
    meta["keywords"] = (meta.get("keywords") or "") + " machine-translation"
    doc.set_metadata(meta)
    if doc.page_count:
        page = doc.load_page(0)
        r = page.rect
        page.insert_text(
            fitz.Point(r.x0 + 12, r.y1 - 8),
            "기계번역본 (machine-translated)",
            fontsize=6,
            color=(0.5, 0.5, 0.5),
        )


def _draw_overflow_marker(page, rect: fitz.Rect) -> None:
    page.draw_rect(
        fitz.Rect(rect.x0, rect.y0, rect.x1, rect.y1),
        color=(0.95, 0.6, 0.1), width=0.4,
    )


def _interleave(src_pdf: Path, translated: fitz.Document) -> fitz.Document:
    src = fitz.open(src_pdf)
    out = fitz.open()
    try:
        for pi in range(src.page_count):
            out.insert_pdf(src, from_page=pi, to_page=pi)
            out.insert_pdf(translated, from_page=pi, to_page=pi)
    finally:
        src.close()
    return out
