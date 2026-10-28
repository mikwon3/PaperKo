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


def _hblk(typ, text, h=11.0, fs=10.6):
    b = Block(id="b", page=0, type=typ, bbox=[80, 100, 460, 100 + h])
    b.style_summary = StyleSummary(font_size=fs)
    return b


def test_real_headings_keep_enlarged_size():
    # genuine headings — short labels and section-numbered — render larger than body
    r = Renderer(opts=RenderOptions())
    r._body_size = 9.7
    for txt in ["1. 서론", "2.1. 데이터 수집", "3.1.1. 분할 프레임워크", "초록", "제2장—FRP 재료", "결론 및 향후 연구"]:
        assert r._norm_size(_hblk("heading", txt), txt) > r._body_size, txt


def test_mislabeled_body_lines_demoted_to_body_size():
    # body prose wrongly tagged as heading must render at the uniform body size,
    # so it doesn't stick out bigger than the surrounding paragraph
    r = Renderer(opts=RenderOptions())
    r._body_size = 9.7
    for txt in [
        "중복도는 강한 그룹 간 이질성을 확인시켜 준다. 따라서 시편들은 공통 모집단으로부터의",  # long prose
        "매립 비율, 표면 상태,",                                             # trails on a comma
        "본 적 없는 프로그램으로의 전이 능력을 다룬다.",                         # ends in a verb (…다.)
    ]:
        assert r._norm_size(_hblk("heading", txt), txt) == r._body_size, txt


def test_multiline_block_tagged_heading_is_body():
    # a block several lines tall is a paragraph regardless of text/type
    r = Renderer(opts=RenderOptions())
    r._body_size = 9.7
    tall = _hblk("heading", "제목처럼 보이지만 실제로는 여러 줄에 걸친 본문 문단", h=60.0)
    assert r._norm_size(tall, "제목처럼 보이지만 실제로는 여러 줄에 걸친 본문 문단") == r._body_size


def test_noun_ending_heading_not_demoted():
    # a heading ending in a noun that happens to end in 음/함 (이음, 그림) stays a heading
    r = Renderer(opts=RenderOptions())
    r._body_size = 9.7
    assert r._norm_size(_hblk("heading", "단계 11—부착 길이 및 이음"), "단계 11—부착 길이 및 이음") > r._body_size


def test_body_paragraph_never_centered():
    # a body paragraph mislabeled "center" by alignment detection must render left
    r = Renderer(opts=RenderOptions())
    html = r._html("some translated body text", _blk("paragraph", "center"), 1.3)
    assert "text-align:left" in html
    # a real heading keeps center
    html_h = r._html("A Centered Title", _blk("heading", "center"), 1.3)
    assert "text-align:center" in html_h
