"""Shared bbox geometry primitives.

A bbox is ``[x0, y0, x1, y1]`` in PDF points (origin top-left, y grows down),
the same convention used across layout and rendering. These were duplicated in
renderer.py and docx_export.py; keep one definition so overlap/containment logic
reads the same everywhere.
"""


def union_bbox(bboxes: list[list[float]]) -> list[float]:
    """Smallest bbox that contains all of ``bboxes`` (which must be non-empty)."""
    return [
        min(b[0] for b in bboxes), min(b[1] for b in bboxes),
        max(b[2] for b in bboxes), max(b[3] for b in bboxes),
    ]


def rect_area(b: list[float]) -> float:
    """Area of a bbox; 0 for a degenerate (zero- or negative-extent) rect."""
    return max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])


def intersect_area(a: list[float], b: list[float]) -> float:
    """Area of the overlap between two bboxes; 0 when they do not overlap."""
    ix = min(a[2], b[2]) - max(a[0], b[0])
    iy = min(a[3], b[3]) - max(a[1], b[1])
    return max(0.0, ix) * max(0.0, iy)


def overlap_frac(box: list[float], others: list[list[float]]) -> float:
    """Fraction of ``box``'s area covered by ``others`` (rough — sums overlaps,
    so heavily overlapping ``others`` can double-count, then clamp to 1.0)."""
    bx = rect_area(box)
    if bx <= 0:
        return 0.0
    cov = sum(intersect_area(box, o) for o in others)
    return min(cov / bx, 1.0)
