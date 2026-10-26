"""__main__.py — engine entrypoint (03 §7, 04 §2).

Two run modes:

* No sub-command  → stdio JSON-RPC 2.0 server (the Wails app's sidecar).
* A sub-command   → developer/testing CLI that drives the pipeline app-free:
      python -m translate_engine analyze  in.pdf [--out document.json]
      python -m translate_engine translate in.pdf out.pdf [--glossary g.json]
      python -m translate_engine render   in.pdf document.json translation.json out.pdf
      python -m translate_engine health
      python -m translate_engine pipeline  in.pdf out.pdf   (analyze+translate+render)

stdout is reserved for JSON-RPC framing in server mode, so all logging goes to
stderr and engine code must never ``print`` to stdout.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import threading
from pathlib import Path
from typing import Any

from . import IR_VERSION, PROTOCOL_VERSION, __version__
from .errors import CANCELLED, EngineError, INTERNAL
from .ir import Document, TranslationResult
from .layout import LayoutEngine
from .qwen import QwenClient
from .renderer import RenderOptions, Renderer
from .terminology import Glossary
from .translator import TranslateOptions, Translator

log = logging.getLogger("translate_engine")


def _setup_logging(level: str = "info") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )


# --------------------------------------------------------------------------- #
# Shared LLM config resolution (CLI + server)
# --------------------------------------------------------------------------- #
def _llm_from_env(overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = {
        "base_url": os.environ.get("PAPERKO_LLM_URL", "http://localhost:8000/v1"),
        "model": os.environ.get("PAPERKO_LLM_MODEL", "qwen3.6-35B-A3B-NVFP4-fast"),
        "api_key": os.environ.get("PAPERKO_LLM_KEY") or None,
        "max_concurrency": int(os.environ.get("PAPERKO_LLM_CONCURRENCY", "4")),
        "timeout_s": float(os.environ.get("PAPERKO_LLM_TIMEOUT", "120")),
    }
    if overrides:
        cfg.update({k: v for k, v in overrides.items() if v is not None})
    return cfg


def _make_client(cfg: dict[str, Any]) -> QwenClient:
    return QwenClient(
        base_url=cfg["base_url"],
        model=cfg["model"],
        api_key=cfg.get("api_key"),
        max_concurrency=cfg.get("max_concurrency", 4),
        timeout_s=cfg.get("timeout_s", 120.0),
    )


# =========================================================================== #
# CLI
# =========================================================================== #
def _cli(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="translate_engine", description="PaperKo engine CLI")
    parser.add_argument("--log-level", default="info")
    parser.add_argument("--llm-url", dest="llm_url")
    parser.add_argument("--model", dest="model")
    parser.add_argument("--api-key", dest="api_key")
    parser.add_argument("--concurrency", type=int, dest="concurrency")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("analyze", help="parse + layout analysis → IR json")
    p.add_argument("pdf")
    p.add_argument("--out", default=None)
    p.add_argument("--password", default=None)
    p.add_argument("--pages", default=None, help="e.g. 1-10,15 (1-based)")
    p.add_argument("--ocr", default="auto", choices=["auto", "off", "force"])

    p = sub.add_parser("translate", help="analyze + translate → out.pdf (full pipeline)")
    p.add_argument("pdf")
    p.add_argument("out")
    p.add_argument("--glossary", default=None)
    p.add_argument("--style", default="formal", choices=["formal", "concise"])
    p.add_argument("--enforce-glossary", action="store_true")
    p.add_argument("--pages", default=None)
    p.add_argument("--mode", default="replace", choices=["replace", "interleaved"])

    p = sub.add_parser("render", help="IR + translation → out.pdf")
    p.add_argument("pdf")
    p.add_argument("ir")
    p.add_argument("translation")
    p.add_argument("out")
    p.add_argument("--mode", default="replace", choices=["replace", "interleaved"])

    p = sub.add_parser("pipeline", help="alias for translate")
    p.add_argument("pdf")
    p.add_argument("out")
    p.add_argument("--glossary", default=None)
    p.add_argument("--pages", default=None)
    p.add_argument("--mode", default="replace", choices=["replace", "interleaved"])

    sub.add_parser("health", help="check vLLM connectivity")

    args = parser.parse_args(argv)
    _setup_logging(args.log_level)
    cfg = _llm_from_env(
        {"base_url": args.llm_url, "model": args.model, "api_key": args.api_key,
         "max_concurrency": args.concurrency}
    )

    try:
        if args.cmd == "analyze":
            return _cli_analyze(args)
        if args.cmd in ("translate", "pipeline"):
            return asyncio.run(_cli_translate(args, cfg))
        if args.cmd == "render":
            return _cli_render(args)
        if args.cmd == "health":
            return asyncio.run(_cli_health(cfg))
    except EngineError as exc:
        print(f"error [{exc.code}]: {exc.message}", file=sys.stderr)
        return 2
    return 1


def _parse_pages(spec: str | None, page_count: int | None = None) -> list[int] | None:
    """'1-10,15' (1-based, inclusive) → sorted 0-based list."""
    if not spec:
        return None
    out: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            for i in range(int(a), int(b) + 1):
                out.add(i - 1)
        else:
            out.add(int(part) - 1)
    return sorted(i for i in out if i >= 0)


def _cli_analyze(args) -> int:
    eng = LayoutEngine()
    pages = _parse_pages(args.pages)
    doc = eng.analyze(
        Path(args.pdf), pages=pages, password=args.password, ocr=args.ocr,
        on_progress=lambda d, t: print(f"analyze {d}/{t}", file=sys.stderr),
    )
    out = Path(args.out) if args.out else Path(args.pdf).with_suffix(".document.json")
    doc.save(out)
    s = doc.stats
    print(f"analyzed {len(doc.pages)} pages, {s.get('block_count')} blocks → {out}", file=sys.stderr)
    return 0


async def _cli_translate(args, cfg) -> int:
    eng = LayoutEngine()
    pages = _parse_pages(args.pages)
    log.info("analyzing %s ...", args.pdf)
    doc = eng.analyze(Path(args.pdf), pages=pages,
                      on_progress=lambda d, t: print(f"\ranalyze {d}/{t}", end="", file=sys.stderr))
    print("", file=sys.stderr)

    glossary = Glossary.load(Path(args.glossary)) if args.glossary else Glossary.empty()
    client = _make_client(cfg)
    opts = TranslateOptions(style=args.style, enforce_glossary=getattr(args, "enforce_glossary", False))
    translator = Translator(client=client, glossary=glossary, opts=opts)

    job_dir = Path(args.out).parent / (Path(args.out).stem + "_work")
    job_dir.mkdir(parents=True, exist_ok=True)
    doc.save(job_dir / "document.json")

    def prog(p):
        pct = int(p.done / max(p.total, 1) * 100)
        print(f"\rtranslate {p.done}/{p.total} ({pct}%)", end="", file=sys.stderr)

    log.info("translating %d TUs ...", len(translator.build_tus(doc)))
    try:
        tr = await translator.translate_document(
            doc, checkpoint_path=job_dir / "translation.json", on_progress=prog
        )
    finally:
        await client.aclose()
    print("", file=sys.stderr)
    log.info("translation stats: %s", json.dumps(tr.stats, ensure_ascii=False))

    renderer = Renderer(opts=RenderOptions(mode=args.mode, preview_png=False))
    report = renderer.render(Path(args.pdf), doc, tr, Path(args.out))
    log.info("render report: %s", json.dumps(report.to_dict(), ensure_ascii=False))
    print(f"done → {args.out}", file=sys.stderr)
    return 0


def _cli_render(args) -> int:
    doc = Document.load(Path(args.ir))
    tr = TranslationResult.load(Path(args.translation))
    renderer = Renderer(opts=RenderOptions(mode=args.mode))
    report = renderer.render(Path(args.pdf), doc, tr, Path(args.out))
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2), file=sys.stderr)
    return 0


async def _cli_health(cfg) -> int:
    client = _make_client(cfg)
    try:
        info = await client.health()
    finally:
        await client.aclose()
    print(json.dumps(info.__dict__, ensure_ascii=False, indent=2))
    return 0 if info.ok and info.model_found else 3


# =========================================================================== #
# JSON-RPC 2.0 stdio server  (04 §2)
# =========================================================================== #
class RpcServer:
    def __init__(self) -> None:
        self.data_dir: Path | None = None
        self.llm_cfg: dict[str, Any] = _llm_from_env()
        self.layout = LayoutEngine()
        self._cancels: dict[str, asyncio.Event] = {}
        self._write_lock = threading.Lock()  # stdout written from loop + worker threads
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stdout = sys.stdout.buffer
        self._stdin = sys.stdin.buffer

    # -- framing -------------------------------------------------------- #
    def _read_message_blocking(self) -> dict | None:
        """Read one Content-Length-framed message from stdin (blocking).

        Runs in a dedicated thread — NOT on the event loop — because asyncio's
        pipe transports are unreliable for stdin on Windows (ProactorEventLoop
        raises ``_ProactorReadPipeTransport ... _empty_waiter``). Blocking reads
        in a thread work identically on all platforms.
        """
        headers: dict[str, str] = {}
        while True:
            line = self._stdin.readline()
            if not line:
                return None  # EOF → parent closed
            text = line.decode("ascii", "replace").strip()
            if text == "":
                break
            if ":" in text:
                k, v = text.split(":", 1)
                headers[k.strip().lower()] = v.strip()
        length = int(headers.get("content-length", "0"))
        if length <= 0:
            return {}
        body = bytearray()
        while len(body) < length:
            chunk = self._stdin.read(length - len(body))
            if not chunk:
                return None
            body.extend(chunk)
        return json.loads(bytes(body).decode("utf-8"))

    def _write_message(self, obj: dict) -> None:
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        header = f"Content-Length: {len(data)}\r\n\r\n".encode("ascii")
        with self._write_lock:  # replies + progress notifications may race
            self._stdout.write(header)
            self._stdout.write(data)
            self._stdout.flush()

    def _reply(self, req_id: Any, result: Any) -> None:
        self._write_message({"jsonrpc": "2.0", "id": req_id, "result": result})

    def _error(self, req_id: Any, code: str, message: str, detail: Any = None) -> None:
        err = {"code": -32000, "message": message, "data": {"code": code}}
        if detail is not None:
            err["data"]["detail"] = detail
        self._write_message({"jsonrpc": "2.0", "id": req_id, "error": err})

    def notify(self, method: str, params: dict) -> None:
        self._write_message({"jsonrpc": "2.0", "method": method, "params": params})

    # -- main loop ------------------------------------------------------ #
    async def serve(self) -> None:
        self._loop = asyncio.get_running_loop()
        stopped = asyncio.Event()

        def reader_thread() -> None:
            try:
                while True:
                    msg = self._read_message_blocking()
                    if msg is None:
                        break  # EOF
                    if msg:
                        self._loop.call_soon_threadsafe(self._on_message, msg)
            except Exception:  # noqa: BLE001
                log.exception("stdin reader thread failed")
            finally:
                self._loop.call_soon_threadsafe(stopped.set)

        threading.Thread(target=reader_thread, name="rpc-stdin", daemon=True).start()
        await stopped.wait()

    def _on_message(self, msg: dict) -> None:
        """Handle a parsed message on the event loop thread."""
        method = msg.get("method")
        if method is None:
            return
        req_id = msg.get("id")
        params = msg.get("params", {}) or {}
        if req_id is None:  # notification (e.g. job.cancel)
            asyncio.create_task(self._handle_notification(method, params))
        else:
            asyncio.create_task(self._dispatch(req_id, method, params))

    async def _handle_notification(self, method: str, params: dict) -> None:
        if method == "job.cancel":
            ev = self._cancels.get(params.get("job_id", ""))
            if ev:
                ev.set()

    async def _dispatch(self, req_id: Any, method: str, params: dict) -> None:
        try:
            handler = getattr(self, "_m_" + method.replace(".", "_"), None)
            if handler is None:
                self._error(req_id, INTERNAL, f"unknown method: {method}")
                return
            result = await handler(params)
            self._reply(req_id, result)
        except EngineError as exc:
            self._error(req_id, exc.code, exc.message, exc.detail)
        except Exception as exc:  # noqa: BLE001
            log.exception("method %s failed", method)
            self._error(req_id, INTERNAL, str(exc), {"traceback": repr(exc)})

    # -- methods -------------------------------------------------------- #
    async def _m_initialize(self, params: dict) -> dict:
        self.data_dir = Path(params.get("data_dir", ".")) if params.get("data_dir") else None
        if "llm" in params and params["llm"]:
            self.llm_cfg = _llm_from_env(params["llm"])
        if params.get("log_level"):
            logging.getLogger("translate_engine").setLevel(
                getattr(logging, params["log_level"].upper(), logging.INFO)
            )
        return {
            "engine_version": __version__,
            "protocol_version": PROTOCOL_VERSION,
            "ir_version": IR_VERSION,
            "capabilities": {"ocr": self.layout.ocr_enabled, "gpu": False},
            "models_loaded": True,
        }

    async def _m_document_open(self, params: dict) -> dict:
        meta = self.layout.open_document(Path(params["path"]), params.get("password"))
        return {
            "doc_id": meta.doc_id,
            "page_count": meta.page_count,
            "title": meta.title,
            "has_text_layer": meta.has_text_layer,
            "encrypted": meta.encrypted,
            "pages_without_text": meta.pages_without_text,
        }

    async def _m_document_analyze(self, params: dict) -> dict:
        job_dir = Path(params["job_dir"])
        job_dir.mkdir(parents=True, exist_ok=True)
        job_id = params.get("job_id", job_dir.name)
        pages = params.get("pages")
        loop = asyncio.get_running_loop()

        def on_progress(done: int, total: int) -> None:
            loop.call_soon_threadsafe(
                self.notify, "progress",
                {"job_id": job_id, "stage": "analyze", "done": done, "total": total},
            )

        doc = await asyncio.to_thread(
            self.layout.analyze, Path(params["pdf_path"]),
            pages=pages, ocr=params.get("ocr", "auto"),
            ocr_lang=params.get("ocr_lang", "eng"), on_progress=on_progress,
        )
        ir_path = job_dir / "document.json"
        doc.save(ir_path)
        return {
            "ir_path": str(ir_path),
            "stats": {
                "block_count": doc.stats.get("block_count", 0),
                "tu_estimate": len(Translator(
                    client=_make_client(self.llm_cfg), glossary=Glossary.empty()
                ).build_tus(doc)),
                "ocr_pages": doc.stats.get("ocr_pages", 0),
            },
            "warnings": _low_confidence_warnings(doc),
        }

    async def _m_document_translate(self, params: dict) -> dict:
        job_id = params["job_id"]
        doc = Document.load(Path(params["ir_path"]))
        gpath = params.get("glossary_path")
        glossary = Glossary.load(Path(gpath)) if gpath and Path(gpath).exists() else Glossary.empty()
        o = params.get("options", {}) or {}
        opts = TranslateOptions(
            style=o.get("style", "formal"),
            enforce_glossary=o.get("enforce_glossary", False),
            max_concurrency=o.get("max_concurrency", self.llm_cfg.get("max_concurrency", 4)),
            page_range=o.get("page_range"),
            custom_system_prompt=o.get("custom_system_prompt"),
            source_lang=o.get("source_lang", "auto"),
            target_lang=o.get("target_lang", "Korean"),
        )
        client = _make_client(self.llm_cfg)
        client._sem = asyncio.Semaphore(opts.max_concurrency)
        translator = Translator(client=client, glossary=glossary, opts=opts)

        cancel = asyncio.Event()
        self._cancels[job_id] = cancel
        translation_path = Path(params["ir_path"]).parent / "translation.json"

        # Batch translation (general-document mode): translate only these 0-based
        # pages this run and accumulate into the shared checkpoint. When a checkpoint
        # already exists we auto-resume so earlier batches are preserved.
        pages = o.get("pages")
        only_pages = {int(p) for p in pages} if pages else None
        do_resume = bool(params.get("resume")) or (only_pages is not None and translation_path.exists())

        def on_progress(p) -> None:
            self.notify("progress", {
                "job_id": job_id, "stage": "translate",
                "done": p.done, "total": p.total, "page": p.page,
            })

        def on_page_done(page: int) -> None:
            self.notify("partial", {"job_id": job_id, "kind": "page_translated", "page": page})

        def on_tu_failed(tu) -> None:
            self.notify("tu_failed", {
                "job_id": job_id, "tu_id": tu.id, "block_ids": tu.block_ids,
                "page": tu.context.get("page"),
                "error": tu.error or {"code": "postprocess", "message": "failed"},
            })

        # NOTE: equation recognition (image → LaTeX) is NOT done here — it would stall
        # the start of translation (the LLM serialises vision calls). It runs instead
        # at export time (_m_document_export), where the LaTeX is actually used for
        # native Word/Hangul equations, so translation stays fast and responsive.
        try:
            tr = await translator.translate_document(
                doc,
                checkpoint_path=translation_path,
                resume_from=translation_path if do_resume else None,
                only_pages=only_pages,
                on_progress=on_progress, on_page_done=on_page_done, on_tu_failed=on_tu_failed,
                cancel=cancel,
            )
        finally:
            await client.aclose()
            self._cancels.pop(job_id, None)

        # A cancelled run is NOT an error: it returns the partial result so the app
        # can render/save/resume the pages that finished (partial pages were dropped
        # from the checkpoint). stats["cancelled"] flags it for the caller.
        return {"translation_path": str(translation_path), "stats": tr.stats}

    async def _m_document_render(self, params: dict) -> dict:
        doc = Document.load(Path(params["ir_path"]))
        tr = TranslationResult.load(Path(params["translation_path"]))
        o = params.get("options", {}) or {}
        opts = RenderOptions(
            mode=o.get("mode", "replace"),
            font_family=o.get("font_family", "noto"),
            min_font_scale=o.get("min_font_scale", 0.55),
            mark_machine_translated=o.get("mark_machine_translated", True),
            preview_png=o.get("preview_png", False),
            target_lang=o.get("target_lang", "Korean"),
        )
        renderer = Renderer(opts=opts)
        out_path = Path(params["out_path"])
        job_id = params.get("job_id", "")

        def on_progress(done: int, total: int) -> None:
            self.notify("progress",
                        {"job_id": job_id, "stage": "render", "done": done, "total": total})

        report = await asyncio.to_thread(
            renderer.render, Path(params["pdf_path"]), doc, tr, out_path, on_progress=on_progress
        )
        return {"out_path": str(out_path), "report": report.to_dict()}

    async def _m_document_export(self, params: dict) -> dict:
        """Export the translated document as a flowing .docx or .hwpx file."""
        from .docx_export import export_document
        doc = Document.load(Path(params["ir_path"]))
        tr = TranslationResult.load(Path(params["translation_path"]))
        fmt = (params.get("format") or "docx").lower()
        out_path = Path(params["out_path"])
        pdf_path = params.get("pdf_path")
        job_id = params.get("job_id", "")

        # Recognise equations → LaTeX here, at export, so DOCX/HWPX get native Word /
        # Hangul math. Done at export (not during translation) so translation stays
        # fast and responsive. Graceful: vision LLM → bundled OCR → keep the original
        # image. Recognised LaTeX is saved into the IR, so re-exports skip it.
        try:
            from .mathrec import recognize_formulas

            src_pdf = pdf_path or doc.source_path
            need = any(b.type == "formula" and not (b.latex or "").strip()
                       for b in doc.all_blocks())
            if src_pdf and need:
                client = _make_client(self.llm_cfg)
                try:
                    n_eq = await recognize_formulas(
                        client, doc, pdf_path=src_pdf, concurrency=2,
                        on_progress=lambda i, t: self.notify("progress", {
                            "job_id": job_id, "stage": "formula", "done": i, "total": t,
                            "page": None,
                        }),
                    )
                finally:
                    await client.aclose()
                if n_eq:
                    doc.save(Path(params["ir_path"]))
                    log.info("recognised %d equation(s) as LaTeX for export", n_eq)
        except Exception as exc:  # recognition must never break an export
            log.warning("equation recognition skipped: %s", exc)

        result = await asyncio.to_thread(export_document, doc, tr, out_path, fmt, pdf_path)
        return {"out_path": str(result), "format": fmt}

    async def _m_tu_retranslate(self, params: dict) -> dict:
        doc = Document.load(Path(params["ir_path"])) if params.get("ir_path") else None
        translation_path = Path(params["translation_path"])
        tr = TranslationResult.load(translation_path)
        if doc is None:
            raise EngineError(INTERNAL, "ir_path required for retranslate")
        doc.translation_units = tr.translation_units
        gpath = params.get("glossary_path")
        glossary = Glossary.load(Path(gpath)) if gpath and Path(gpath).exists() else Glossary.empty()
        o = params.get("options", {}) or {}
        opts = TranslateOptions(
            style=o.get("style", "formal"),
            enforce_glossary=o.get("enforce_glossary", False),
            max_concurrency=o.get("max_concurrency", self.llm_cfg.get("max_concurrency", 4)),
            source_lang=o.get("source_lang", "auto"),
            target_lang=o.get("target_lang", "Korean"),
        )
        client = _make_client(self.llm_cfg)
        translator = Translator(client=client, glossary=glossary, opts=opts)
        try:
            tu = await translator.translate_single(doc, params["tu_id"])
        finally:
            await client.aclose()
        tr.save(translation_path)
        return {"tu": _tu_to_dict(tu)}

    async def _m_configure(self, params: dict) -> dict:
        """Update the live LLM config (model, URL, key, concurrency) without a
        restart.  Called when the user saves settings so the next job uses them."""
        if "llm" in params and params["llm"]:
            self.llm_cfg = _llm_from_env(params["llm"])
        if params.get("log_level"):
            logging.getLogger("translate_engine").setLevel(
                getattr(logging, params["log_level"].upper(), logging.INFO)
            )
        return {"ok": True, "llm": {"base_url": self.llm_cfg["base_url"],
                                    "model": self.llm_cfg["model"]}}

    async def _m_llm_health(self, params: dict) -> dict:
        """Health-check the vLLM server (FR-36).  Uses the given llm config or the
        one from initialize."""
        cfg = _llm_from_env(params.get("llm")) if params.get("llm") else self.llm_cfg
        client = _make_client(cfg)
        try:
            info = await client.health()
        finally:
            await client.aclose()
        return {
            "ok": info.ok, "latency_ms": info.latency_ms,
            "model_found": info.model_found, "error": info.error,
        }

    async def _m_page_preview(self, params: dict) -> dict:
        """Rasterize one PDF page to a PNG (base64) for the compare view.

        params: {pdf_path, page (0-based), zoom?}  → {png_base64, width, height}
        """
        import base64

        import pymupdf as fitz

        pdf_path = params["pdf_path"]
        page_index = int(params.get("page", 0))
        zoom = float(params.get("zoom", 1.5))

        def render() -> dict:
            doc = fitz.open(pdf_path)
            try:
                if page_index < 0 or page_index >= doc.page_count:
                    raise EngineError(INTERNAL, f"page {page_index} out of range")
                pix = doc.load_page(page_index).get_pixmap(matrix=fitz.Matrix(zoom, zoom))
                return {
                    "png_base64": base64.b64encode(pix.tobytes("png")).decode("ascii"),
                    "width": pix.width,
                    "height": pix.height,
                }
            finally:
                doc.close()

        return await asyncio.to_thread(render)

    async def _m_shutdown(self, params: dict) -> dict:
        asyncio.get_running_loop().call_later(0.1, lambda: os._exit(0))
        return {"ok": True}


def _low_confidence_warnings(doc: Document) -> list[dict]:
    warnings: list[dict] = []
    for page in doc.pages:
        low = [b for b in page.blocks if b.confidence < 0.6]
        if low:
            warnings.append({"page": page.index, "code": "low_confidence_blocks", "count": len(low)})
    return warnings


def _tu_to_dict(tu) -> dict:
    from dataclasses import asdict

    return asdict(tu)


# =========================================================================== #
def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and not argv[0].startswith("-"):
        return _cli(argv)
    if argv and argv[0] in ("-h", "--help"):
        return _cli(argv)
    # no sub-command → sidecar server
    _setup_logging(os.environ.get("PAPERKO_LOG_LEVEL", "info"))
    log.info("translate_engine %s — JSON-RPC stdio server", __version__)
    server = RpcServer()
    try:
        asyncio.run(server.serve())
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
