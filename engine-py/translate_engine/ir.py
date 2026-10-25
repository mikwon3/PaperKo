"""Document Intermediate Representation (IR).

The single data contract shared by every module (``03-detailed-design.md`` §1).
Dataclasses here mirror ``schemas/document-ir.schema.json`` and
``schemas/translation.schema.json``.  All coordinates are PDF points with a
top-left origin; all identifiers are snake_case.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from . import IR_VERSION

BBox = list[float]  # [x0, y0, x1, y1]

# Block types (01-srs.md FR-20).
BLOCK_TYPES = (
    "title", "heading", "paragraph", "list", "caption", "table",
    "figure", "formula", "header", "footer", "footnote", "reference",
)
# Types excluded from translation by default (03 §2.3 rule ⑥; references kept in
# the original language — see layout._classify_blocks).
NON_TRANSLATED_TYPES = frozenset({"figure", "table", "formula", "header", "footer", "reference"})


# --------------------------------------------------------------------------- #
# Analysis IR
# --------------------------------------------------------------------------- #
@dataclass
class Span:
    text: str
    bbox: BBox
    font: str = ""
    size: float = 0.0
    color: str = "#000000"
    flags: dict[str, bool] = field(
        default_factory=lambda: {"bold": False, "italic": False, "superscript": False}
    )


@dataclass
class Line:
    bbox: BBox
    spans: list[Span] = field(default_factory=list)


@dataclass
class StyleSummary:
    font_size: float = 10.0
    bold: bool = False
    italic: bool = False
    color: str = "#000000"
    align: str = "left"          # left | right | center | justify
    line_height: float = 1.32
    serif: bool = False


@dataclass
class Block:
    id: str
    page: int
    type: str
    bbox: BBox
    reading_order: int = 0
    translate: bool = True
    continues: str | None = None                 # id of the preceding block, if merged
    lines: list[Line] = field(default_factory=list)
    style_summary: StyleSummary = field(default_factory=StyleSummary)
    confidence: float = 1.0
    inline_marks: list[dict[str, Any]] = field(default_factory=list)  # math/citation/url spans
    latex: str = ""              # recognised LaTeX for type=="formula" blocks (empty = not recognised)

    def text(self) -> str:
        parts: list[str] = []
        for ln in self.lines:
            line_txt = "".join(sp.text for sp in ln.spans)
            if line_txt:
                parts.append(line_txt)
        return " ".join(parts).strip()


@dataclass
class Column:
    x0: float
    x1: float


@dataclass
class Page:
    index: int
    width: float
    height: float
    rotation: int = 0
    ocr_used: bool = False
    columns: list[Column] = field(default_factory=list)
    blocks: list[Block] = field(default_factory=list)


@dataclass
class DocMeta:
    doc_id: str
    page_count: int
    title: str = ""
    has_text_layer: bool = True
    encrypted: bool = False
    language: str = "en"
    pages_without_text: list[int] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Translation IR
# --------------------------------------------------------------------------- #
@dataclass
class Placeholder:
    key: str                     # e.g. "M1"
    kind: str                    # math | citation | url | code
    raw: str                     # original text to restore
    span_ref: list[Any] | None = None


@dataclass
class TermHit:
    src: str
    dst: str
    priority: int = 0
    count: int = 1


@dataclass
class TU:
    """Translation Unit — one prompt's worth of text (03 §1.3)."""

    id: str
    block_ids: list[str]
    source_text: str                       # placeholder-masked text
    placeholders: list[Placeholder] = field(default_factory=list)
    context: dict[str, Any] = field(default_factory=dict)
    term_hits: list[TermHit] = field(default_factory=list)
    status: str = "pending"                # pending|running|done|failed|skipped
    target_text: str | None = None         # post-processed, placeholders restored
    attempts: int = 0
    error: dict[str, Any] | None = None
    edited_by_user: bool = False
    warnings: list[str] = field(default_factory=list)


@dataclass
class Document:
    doc_id: str
    source_path: str
    meta: dict[str, Any]
    pages: list[Page] = field(default_factory=list)
    translation_units: list[TU] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)
    ir_version: str = IR_VERSION

    # -- lookups -----------------------------------------------------------
    def all_blocks(self) -> list[Block]:
        return [b for p in self.pages for b in p.blocks]

    def block_map(self) -> dict[str, Block]:
        return {b.id: b for b in self.all_blocks()}

    # -- (de)serialisation -------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "ir_version": self.ir_version,
            "doc_id": self.doc_id,
            "source_path": self.source_path,
            "meta": self.meta,
            "pages": [_page_to_dict(p) for p in self.pages],
            "translation_units": [asdict(tu) for tu in self.translation_units],
            "stats": self.stats,
        }

    def save(self, path: Path) -> None:
        _atomic_write_json(path, self.to_dict())

    @classmethod
    def load(cls, path: Path) -> "Document":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls.from_dict(data)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Document":
        _check_ir_version(data.get("ir_version", "1.0"))
        pages = [_page_from_dict(p) for p in data.get("pages", [])]
        tus = [_tu_from_dict(t) for t in data.get("translation_units", [])]
        return cls(
            doc_id=data["doc_id"],
            source_path=data.get("source_path", ""),
            meta=data.get("meta", {}),
            pages=pages,
            translation_units=tus,
            stats=data.get("stats", {}),
            ir_version=data.get("ir_version", IR_VERSION),
        )


# --------------------------------------------------------------------------- #
# translation.json checkpoint  (only the TU state; the IR lives in document.json)
# --------------------------------------------------------------------------- #
@dataclass
class TranslationResult:
    doc_id: str
    translation_units: list[TU]
    stats: dict[str, Any] = field(default_factory=dict)
    cancelled: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "doc_id": self.doc_id,
            "translation_units": [asdict(tu) for tu in self.translation_units],
            "stats": self.stats,
            "cancelled": self.cancelled,
        }

    def save(self, path: Path) -> None:
        _atomic_write_json(path, self.to_dict())

    @classmethod
    def load(cls, path: Path) -> "TranslationResult":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(
            doc_id=data["doc_id"],
            translation_units=[_tu_from_dict(t) for t in data.get("translation_units", [])],
            stats=data.get("stats", {}),
            cancelled=data.get("cancelled", False),
        )

    def tu_map(self) -> dict[str, TU]:
        return {tu.id: tu for tu in self.translation_units}


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _check_ir_version(version: str) -> None:
    from .errors import EngineError, INTERNAL

    try:
        major = int(str(version).split(".")[0])
    except ValueError as exc:  # pragma: no cover
        raise EngineError(INTERNAL, f"malformed ir_version: {version!r}") from exc
    if major > int(IR_VERSION.split(".")[0]):
        raise EngineError(
            INTERNAL,
            f"IR version {version} is newer than engine supports ({IR_VERSION}); please update.",
        )


def _atomic_write_json(path: Path, obj: dict[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)  # atomic on POSIX/Windows


def _page_to_dict(p: Page) -> dict[str, Any]:
    return {
        "index": p.index,
        "width": p.width,
        "height": p.height,
        "rotation": p.rotation,
        "ocr_used": p.ocr_used,
        "columns": [asdict(c) for c in p.columns],
        "blocks": [asdict(b) for b in p.blocks],
    }


def _page_from_dict(d: dict[str, Any]) -> Page:
    return Page(
        index=d["index"],
        width=d["width"],
        height=d["height"],
        rotation=d.get("rotation", 0),
        ocr_used=d.get("ocr_used", False),
        columns=[Column(**c) for c in d.get("columns", [])],
        blocks=[_block_from_dict(b) for b in d.get("blocks", [])],
    )


def _block_from_dict(d: dict[str, Any]) -> Block:
    lines = [
        Line(bbox=ln["bbox"], spans=[Span(**sp) for sp in ln.get("spans", [])])
        for ln in d.get("lines", [])
    ]
    style = d.get("style_summary", {})
    return Block(
        id=d["id"],
        page=d["page"],
        type=d["type"],
        bbox=d["bbox"],
        reading_order=d.get("reading_order", 0),
        translate=d.get("translate", True),
        continues=d.get("continues"),
        lines=lines,
        style_summary=StyleSummary(**style) if style else StyleSummary(),
        confidence=d.get("confidence", 1.0),
        inline_marks=d.get("inline_marks", []),
        latex=d.get("latex", ""),
    )


def _tu_from_dict(d: dict[str, Any]) -> TU:
    return TU(
        id=d["id"],
        block_ids=d.get("block_ids", []),
        source_text=d.get("source_text", ""),
        placeholders=[Placeholder(**ph) for ph in d.get("placeholders", [])],
        context=d.get("context", {}),
        term_hits=[TermHit(**th) for th in d.get("term_hits", [])],
        status=d.get("status", "pending"),
        target_text=d.get("target_text"),
        attempts=d.get("attempts", 0),
        error=d.get("error"),
        edited_by_user=d.get("edited_by_user", False),
        warnings=d.get("warnings", []),
    )
