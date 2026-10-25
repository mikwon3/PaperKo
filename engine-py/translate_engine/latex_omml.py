"""LaTeX → OMML (Office Math Markup) for DOCX export.

Converts a LaTeX math string into an ``<m:oMath>`` element (OOXML math) that drops
straight into a python-docx paragraph. The very same OMML is what the bundled HWPX
converter (``hwpx/scripts/omml_to_hwp.py``) already turns into a native Hangul
equation, so once a formula carries OMML both DOCX and HWPX get real equations.

Conversion chain: ``latex2mathml`` (LaTeX → Presentation MathML) then
``mathml2omml`` (MathML → OMML). Both are small pure-Python packages.

Everything here is graceful: when the dependencies are missing or a particular
LaTeX string can't be converted, the functions return ``None`` and callers fall
back to plain text (or the preserved original image).
"""

from __future__ import annotations

import re

# OOXML math namespace.
M_NS = "http://schemas.openxmlformats.org/officeDocument/2006/math"

_convert_latex = None  # latex2mathml.converter.convert
_convert_mml = None    # mathml2omml.convert
_loaded = False


def _load() -> None:
    global _convert_latex, _convert_mml, _loaded
    if _loaded:
        return
    _loaded = True
    try:  # keep the import lazy so the engine runs without the math extras
        from latex2mathml.converter import convert as _cl
        from mathml2omml import convert as _cm

        _convert_latex, _convert_mml = _cl, _cm
    except Exception:
        _convert_latex = _convert_mml = None


def available() -> bool:
    """True when the LaTeX→OMML toolchain is importable."""
    _load()
    return _convert_latex is not None and _convert_mml is not None


_PROLOG_RE = re.compile(r"^\s*<\?xml[^>]*\?>\s*")


def latex_to_omml_xml(latex: str) -> str | None:
    """Return an ``<m:oMath …>`` XML string (namespace bound), or ``None``."""
    _load()
    if not _convert_latex or not _convert_mml:
        return None
    tex = (latex or "").strip()
    if not tex:
        return None
    # Strip surrounding math delimiters a recogniser might have left in.
    for a, b in (("$$", "$$"), ("$", "$"), (r"\[", r"\]"), (r"\(", r"\)")):
        if tex.startswith(a) and tex.endswith(b) and len(tex) > len(a) + len(b):
            tex = tex[len(a): len(tex) - len(b)].strip()
            break
    try:
        mml = _convert_latex(tex)
        omml = _convert_mml(mml)
    except Exception:
        return None
    if not omml:
        return None
    omml = _PROLOG_RE.sub("", omml).strip()
    if not omml.startswith("<m:oMath"):
        return None
    # mathml2omml emits m:-prefixed tags with no xmlns decl; bind it on the root
    # so the fragment parses stand-alone and serialises correctly inside the docx.
    if "xmlns:m=" not in omml:
        omml = omml.replace("<m:oMath>", f'<m:oMath xmlns:m="{M_NS}">', 1)
        omml = omml.replace("<m:oMath ", f'<m:oMath xmlns:m="{M_NS}" ', 1) if "xmlns:m=" not in omml else omml
    return omml


def latex_to_omml_element(latex: str):
    """Return an lxml ``<m:oMath>`` element ready to append to a ``w:p``, or ``None``."""
    xml = latex_to_omml_xml(latex)
    if xml is None:
        return None
    try:
        from lxml import etree

        return etree.fromstring(xml.encode("utf-8"))
    except Exception:
        return None
