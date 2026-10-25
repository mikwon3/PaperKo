"""Equation recognition — turn a formula region into LaTeX.

The source PDF's math fonts (CMMI/CMSY/…) extract as garbage unicode, so we cannot
reconstruct equations from the text layer. Instead we read the *rendered* equation
as an image and transcribe it to LaTeX, which export then turns into native Word
(OMML) and Hangul (HWPX) equations.

Strategy chain, first that works wins (decided once per document, then reused):

  1. **Multimodal LLM** — crop the equation to a PNG and ask the configured model
     to transcribe it. No bundled model; needs a vision-capable backend (Qwen-VL,
     GPT-4o, Claude, Gemini …). This is the default.
  2. **Local math-OCR** — ``rapid_latex_ocr`` (ONNX; model fetched on demand).
     Fully offline, used when the LLM has no vision.
  3. **None** — leave ``latex=""``; export falls back to the preserved equation
     image (never the garbled text).

All failures are swallowed and downgrade to the next strategy, so recognition can
never break a translation run.
"""

from __future__ import annotations

import asyncio
import logging
import re

log = logging.getLogger("translate_engine.mathrec")

# Don't bother recognising tiny specks (equation numbers, stray glyphs).
_MIN_AREA_PT = 60.0      # bbox area in pt^2 below this is skipped
_MIN_SIDE_PT = 6.0
_RENDER_DPI = 220
_PAD_PT = 2.0


# --------------------------------------------------------------------------- #
# image rendering
# --------------------------------------------------------------------------- #
def render_clip_png(pdf_page, bbox, dpi: int = _RENDER_DPI) -> bytes | None:
    """Rasterise a PDF region (the equation) to PNG bytes, with a little padding."""
    import pymupdf as fitz

    try:
        x0, y0, x1, y1 = bbox
        rect = fitz.Rect(x0 - _PAD_PT, y0 - _PAD_PT, x1 + _PAD_PT, y1 + _PAD_PT)
        rect = rect & pdf_page.rect
        if rect.is_empty or rect.width < _MIN_SIDE_PT or rect.height < _MIN_SIDE_PT:
            return None
        pix = pdf_page.get_pixmap(clip=rect, dpi=dpi, alpha=False)
        return pix.tobytes("png")
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# output cleaning
# --------------------------------------------------------------------------- #
_FENCE_RE = re.compile(r"^```[a-zA-Z]*\s*|\s*```$")
_LABEL_RE = re.compile(r"^\s*(?:latex|equation|math)\s*[:=]\s*", re.I)


def clean_latex(text: str) -> str:
    """Strip code fences / prose / delimiters a model may wrap around the LaTeX."""
    s = (text or "").strip()
    if not s:
        return ""
    s = _FENCE_RE.sub("", s).strip()
    # If the model answered in prose, try to pull a fenced or $...$ block out.
    m = re.search(r"```(?:latex|tex)?\s*(.+?)\s*```", s, re.S)
    if m:
        s = m.group(1).strip()
    s = _LABEL_RE.sub("", s).strip()
    # unwrap one layer of display/inline delimiters
    for a, b in (("$$", "$$"), (r"\[", r"\]"), (r"\(", r"\)"), ("$", "$")):
        if s.startswith(a) and s.endswith(b) and len(s) > len(a) + len(b):
            s = s[len(a): len(s) - len(b)].strip()
            break
    # a single line only; collapse newlines the model may have added
    s = s.replace("\r", " ").replace("\n", " ")
    s = re.sub(r"\s{2,}", " ", s).strip()
    # reject obvious non-answers
    if len(s) > 2000 or s.lower() in {"none", "n/a", "no equation", "i cannot", "i'm sorry"}:
        return ""
    if _is_garbage_latex(s):
        return ""
    return s


def _is_garbage_latex(s: str) -> bool:
    """Reject the runaway output a weak math-OCR emits on hard/wide crops.

    pix2tex-style models, fed a wide or noisy equation, spit endless ``\\qquad``
    padding or huge empty ``array`` tables. Better to drop that (→ keep the original
    equation image) than to inject a nonsense equation.
    """
    if s.count(r"\qquad") >= 4 or s.count("~~~~") >= 1:
        return True
    if r"\begin{array}" in s and s.count("&") >= 10:
        return True
    # a long run of empty groups "{}{}{}" is the array-collapse signature
    if "{}{}{}{}" in s.replace(" ", ""):
        return True
    return False


# --------------------------------------------------------------------------- #
# strategy 1 — multimodal LLM
# --------------------------------------------------------------------------- #
_VLM_SYSTEM = (
    "You are a precise OCR engine for mathematical formulas. You transcribe the "
    "equation shown in the image into LaTeX exactly as printed."
)
_VLM_PROMPT = (
    "Transcribe the mathematical equation in this image to a single line of LaTeX.\n"
    "Rules: output ONLY the LaTeX code — no $ delimiters, no code fences, no words, "
    "no explanation. Preserve subscripts, superscripts, Greek letters, fractions, "
    "radicals, sums/integrals and operators exactly. Do not translate any text; keep "
    "variable names as printed. If the image is not an equation, output nothing."
)


async def vlm_latex(client, png: bytes) -> str | None:
    """Ask the (vision-capable) LLM to transcribe the equation image to LaTeX.

    Returns cleaned LaTeX, or None when the backend has no vision / errored.
    """
    import base64

    data_uri = "data:image/png;base64," + base64.b64encode(png).decode("ascii")
    messages = [
        {"role": "system", "content": _VLM_SYSTEM},
        {"role": "user", "content": [
            {"type": "text", "text": _VLM_PROMPT},
            {"type": "image_url", "image_url": {"url": data_uri}},
        ]},
    ]
    try:
        res = await client.chat(messages, temperature=0.0, top_p=1.0, max_tokens=512)
    except Exception as exc:  # noqa: BLE001 — any backend error downgrades
        log.info("vlm equation transcription failed: %s", exc)
        return None
    return clean_latex(res.text) or None


# --------------------------------------------------------------------------- #
# strategy 2 — local ONNX math-OCR (rapid_latex_ocr), lazy + cached
# --------------------------------------------------------------------------- #
_ocr_model = None
_ocr_tried = False


def _get_ocr():
    global _ocr_model, _ocr_tried
    if _ocr_tried:
        return _ocr_model
    _ocr_tried = True
    try:
        from rapid_latex_ocr import LaTeXOCR  # ONNX-based math-OCR (pix2tex weights)

        _ocr_model = LaTeXOCR()
    except Exception as exc:  # not installed / model unavailable / offline
        log.info("local math-OCR unavailable: %s", exc)
        _ocr_model = None
    return _ocr_model


def _trim_png(png: bytes) -> bytes:
    """Trim whitespace borders so the OCR model sees a tight equation crop."""
    try:
        import io

        from PIL import Image, ImageChops, ImageOps

        im = Image.open(io.BytesIO(png)).convert("L")
        inv = ImageChops.invert(im)
        box = inv.getbbox()
        if box:
            im = im.crop(box)
            im = ImageOps.expand(im, border=8, fill=255)   # small quiet margin
        out = io.BytesIO()
        im.convert("RGB").save(out, format="PNG")
        return out.getvalue()
    except Exception:
        return png


def ocr_latex(png: bytes) -> str | None:
    model = _get_ocr()
    if model is None:
        return None
    try:
        result, _elapsed = model(_trim_png(png))
        return clean_latex(result) or None
    except Exception as exc:  # noqa: BLE001
        log.info("local math-OCR failed: %s", exc)
        return None


# --------------------------------------------------------------------------- #
# document-level pass
# --------------------------------------------------------------------------- #
def _area(b) -> float:
    return max(0.0, (b[2] - b[0])) * max(0.0, (b[3] - b[1]))


async def recognize_formulas(
    client,
    doc,
    *,
    pdf_path: str | None = None,
    pages: set[int] | None = None,
    cancel=None,
    on_progress=None,
) -> int:
    """Fill ``block.latex`` for formula blocks by reading their rendered image.

    Mutates ``doc`` in place (caller persists it). Only touches formula blocks that
    have no latex yet and, when ``pages`` is given, only those pages. Returns the
    number of formulas newly recognised.
    """
    import pymupdf as fitz

    src = pdf_path or doc.source_path
    if not src:
        return 0
    from pathlib import Path as _P

    if not _P(src).exists():
        return 0

    targets = [
        b for b in doc.all_blocks()
        if b.type == "formula" and not (b.latex or "").strip()
        and (pages is None or b.page in pages)
        and _area(b.bbox) >= _MIN_AREA_PT
    ]
    if not targets:
        return 0

    try:
        pdf = fitz.open(src)
    except Exception:
        return 0

    use_vlm = True           # start with the LLM; fall back to OCR on first failure
    done = 0
    total = len(targets)
    try:
        for i, blk in enumerate(targets):
            if cancel is not None and cancel.is_set():
                break
            try:
                page = pdf.load_page(blk.page)
            except Exception:
                continue
            png = render_clip_png(page, blk.bbox)
            if not png:
                continue

            latex = None
            if use_vlm:
                latex = await vlm_latex(client, png)
                if latex is None:
                    # backend has no vision (or a hard error) — switch to OCR for
                    # the rest of the document so we don't retry a non-VLM each time.
                    use_vlm = False
            if latex is None:
                latex = await asyncio.to_thread(ocr_latex, png)

            if latex:
                blk.latex = latex
                done += 1
            if on_progress is not None:
                try:
                    on_progress(i + 1, total)
                except Exception:
                    pass
    finally:
        with __import__("contextlib").suppress(Exception):
            pdf.close()
    return done
