from translate_engine.ir import Block, StyleSummary
from translate_engine.renderer import (
    Renderer,
    RenderOptions,
    _intersect_area,
    _rect_area,
    _strip_math_delims,
    _union_bbox,
)


def test_strip_math_delims_normalizes_leaked_latex():
    # inline-math off: $..$ / \(..\) delimiters dropped, LaTeX → Unicode
    assert _strip_math_delims("$f_y$") == "f_y"
    assert _strip_math_delims(r"2.5d = 1,031 \text{ mm} \leq a") == "2.5d = 1,031  mm ≤ a"


def test_strip_math_delims_subscript_braces_without_backslash():
    # models emit "f_{fu}" (no backslash) — braces must be removed, not left raw
    assert _strip_math_delims("f_{fu}") == "f_fu"
    assert _strip_math_delims("f_{yf}/f_{yus}") == "f_yf/f_yus"
    assert _strip_math_delims("x^{2}") == "x^2"


def _blk(typ, align):
    b = Block(id="b", page=0, type=typ, bbox=[0, 0, 100, 20])
    b.style_summary = StyleSummary(align=align)
    return b


def test_rect_and_intersect_area():
    assert _rect_area([0, 0, 10, 20]) == 200
    assert _rect_area([10, 10, 0, 0]) == 0          # degenerate → 0, not negative
    assert _intersect_area([0, 0, 10, 10], [5, 5, 15, 15]) == 25
    assert _intersect_area([0, 0, 10, 10], [20, 20, 30, 30]) == 0  # disjoint


def test_overlap_containment_ratio():
    # A heading box fully inside a paragraph's union (the OCR double-layer / sandwiched
    # case): the smaller region is ≥55% contained, so it would be suppressed.
    paragraph = _union_bbox([[80, 310, 410, 420], [80, 420, 410, 540]])  # y 310..540
    heading = [90, 419, 170, 430]                                        # sits inside it
    ratio = _intersect_area(heading, paragraph) / (_rect_area(heading) or 1.0)
    assert ratio >= 0.55
    # Two side-by-side columns are NOT contained in each other → both kept.
    left = [40, 60, 300, 700]
    right = [320, 60, 580, 700]
    assert _intersect_area(left, right) == 0


def test_body_paragraph_never_centered():
    # a body paragraph mislabeled "center" by alignment detection must render left
    r = Renderer(opts=RenderOptions())
    html = r._html("some translated body text", _blk("paragraph", "center"), 1.3)
    assert "text-align:left" in html
    # a real heading keeps center
    html_h = r._html("A Centered Title", _blk("heading", "center"), 1.3)
    assert "text-align:center" in html_h
