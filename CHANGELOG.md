# Changelog

All notable changes to PaperKo are recorded here. Versioning follows
[Semantic Versioning](https://semver.org): the app and the Python engine ship
under the **same** version number; the JSON-RPC `protocol_version` and IR
`ir_version` are tracked separately.

## [1.8.7] — 2026-10-10

### Changed
- **Build/release tooling only — no app behavior change.** `release.sh` now prunes the
  distribution repo after publishing, keeping only the newest 2 releases (release + tag)
  and trimming the releases-repo README to match (override with `KEEP_RELEASES`).
  Unified all repo/folder names to `PaperKo` (source, releases, local, and the Windows
  build dir `C:/App-windows/PaperKo`).

## [1.8.6] — 2026-09-26

### Changed
- **Build/release tooling only — no app behavior change.** `release.sh` now updates the
  releases repo README "변경 이력" section automatically after publishing (via
  `scripts/update-release-readme.py`). Removed the unused Windows MSIX packaging path
  (`build/windows/msix/` and its dormant Taskfile tasks) and the leftover "My Company"
  placeholder; Windows distribution stays NSIS.

## [1.8.5] — 2026-09-26

### Changed
- **Author/copyright credits.** The in-app About dialog and the macOS/Windows bundle
  properties now credit **Minho Kwon (@mikwon3)** with contact **mikwon@me.com**; the
  About license label is corrected to **AGPL-3.0**, and the department/organization
  fields are dropped. macOS `NSHumanReadableCopyright`, Windows `CompanyName`/
  `LegalCopyright`, and the NSIS publisher are set accordingly.

### Docs
- Verified-journal table expanded with 6 more journals across 2 new publisher series
  (Hindawi, ASCE): _Structures_ and _Journal of Building Engineering_ (Elsevier),
  _Journal of Structural Engineering_ (ASCE), _Advances in Structural Engineering_ (SAGE),
  _Earthquake Engineering & Structural Dynamics_ (Wiley), and _Journal of Applied
  Mathematics_ (Hindawi).

## [1.8.4] — 2026-09-25

### Added
- **Online auto-update.** On startup the app checks for a new version (at most once a
  day; can be turned off or a version skipped in Settings) and can download and install
  a signed release itself — macOS swaps in the new `.app` and relaunches, Windows hands
  off to the NSIS installer. Release manifests are verified with an embedded Ed25519
  public key and installers by SHA-256. Manual "지금 업데이트 확인" is in Settings.
  Releases are published to the public repo `mikwon3/PaperKo-releases` via
  `scripts/release.sh`; see `internal/update` and `cmd/releasetool`.

### Changed
- **Licensed under AGPL-3.0.** PaperKo bundles PyMuPDF (AGPL-3.0), so the combined work
  is distributed under AGPL-3.0 with full corresponding source published at
  `mikwon3/PaperKo`.
- Bundled Python no longer ships pip console scripts (smaller bundle).

## [1.8.3] — 2026-09-03

### Added
- **Commercial LLM providers (OpenAI / Anthropic / Google).** Settings gains an **LLM
  공급자** selector — OpenAI (ChatGPT), Anthropic (Claude), Google (Gemini), or a custom
  OpenAI-compatible server — that fills the provider's OpenAI-compatible endpoint,
  suggests models, and shows where to get an API key. Requests to a commercial API now
  send only widely-supported fields (the vLLM/Qwen extensions `chat_template_kwargs`,
  `reasoning*`, `seed` are omitted, which those APIs reject); a self-hosted vLLM/LM
  Studio server still gets the full set. Just paste an API key and pick a model.

## [1.8.2] — 2026-08-29

### Added
- **General-document mode with split / resumable batch translation.** The app keeps
  its academic-paper UI by default, but a **문서 유형 (학술 논문 / 일반 문서)** toggle —
  auto-recommending 일반 문서 for long files (>30 pages) — switches to a workflow built
  for specs, guidebooks and other long documents: analyze the whole document once, then
  translate **page-range batches** ("1-40", "41-80", …) that accumulate into one output
  PDF. Each batch shows overall progress (translated pages / total).
- **Pause, resume, and continue across app restarts.** A running translation can be
  cancelled and resumed from its per-TU checkpoint (only the outstanding units re-run).
  Cancelling **keeps only fully-translated pages** in the temp output — a page that was
  mid-translation is dropped so it never renders half-Korean, and is re-done cleanly on
  the next batch. Clicking **이어받기** re-opens the document in batch mode with progress
  restored and the next page range pre-filled (you choose/adjust it, then translate);
  finishing a batch advances to the next range so you can keep going or stop. A **🗑
  임시본 삭제 후 처음부터** option discards a document's temporary translations to start over.
  Jobs are now persisted to `<job>/job.json`, so after quitting and reopening the app an
  **⏸ 이어할 번역** list offers every unfinished document (interrupted / cancelled /
  failed / partly-done) with its progress and a **이어받기** button. Backed by a new
  `pages` filter in the engine's `document.translate` (translate only these pages this
  run, auto-resuming the shared checkpoint) and `JobService.ResumeJob` / `ListResumable`.

- **Design codes / specifications (ACI, CSA) verified.** Two-column code documents
  translate with the same pipeline as journals. Verified end-to-end on **ACI
  CODE-440.11-22** (a digital two-column Code/Commentary standard) and **CSA S806-12**
  (a single-column OCR-scanned standard). Both are recorded in the desktop app's
  README "검증된 저널 레이아웃" table.

### Fixed
- **DOCX / HWPX export of scanned documents now contains the translation, not the
  scan.** A scanned page is a full-page image with an OCR text layer; the flowing
  export was detecting that page-sized image as a "figure" and embedding it — so the
  saved .docx/.hwpx showed the original English scan with no text. Near-full-page
  images (>85% of the page) are now treated as a scanned background and skipped, so the
  export emits the translated text instead.
- **A document whose only remaining pages have no translatable text now reaches 100%.**
  Blank pages and back-cover ads have no translation units, so progress stalled at
  e.g. 216/218 (99%) and the finished document kept reappearing in the resume list. A
  document is now complete once no units are left *pending* (done or failed), so it
  reads 100% and drops off the resume list; failed units are handled via retranslate.
- **Spec provision markers no longer clutter the translation.** ACI/CSA codes glue a
  change marker to the start of each clause — extracted inconsistently as `=`
  (`=4.9.2 The strength…`) or as a raw control char (U+0019/U+001B) that rendered as a
  ◆ tofu box. The layout stage now strips control characters, a leading `=` provision
  marker, and soft hyphens (`re­quire­ment` → `requirement`) from span text before it
  reaches the model or the renderer.
- **Scanned specs: heading tails no longer bleed through.** On an OCR'd scan (a text
  layer over a full-page image), a normal redaction left the scanned picture in place,
  and OCR bboxes are narrower than the visible glyphs — so a translated heading showed
  a stray English tail (`…Materials` → a floating `ls`). The renderer now detects a
  scanned page (a page-covering image), blanks the image pixels under redactions, and
  widens redaction boxes rightward to the column edge (all blocks on a scanned page,
  not just headings) with a small vertical pad. Digital pages (no full-page image)
  are unaffected. Limitation: content the source PDF's own OCR missed entirely (no
  text layer over a scanned equation/label) has no block to redact, so it can still
  bleed through on dense math pages — a source-OCR-quality limit, not a render bug.

### Verified
- **ACI MNL-723 GFRP Design Handbook** (218-page OCR scan): body and worked-example
  pages translate cleanly, figures and equations (as images) are preserved. Recorded
  in the desktop README table with its known limit (OCR-missed equation labels).

## [1.8.1] — 2026-08-23

### Added
- **UI language (한국어 / English).** Settings gains a **언어 / Language** toggle; the
  whole interface — menus, buttons, labels, placeholders, status text — switches
  between Korean and English (defaults to Korean). Strings live in a small `i18n`
  map and the choice persists in settings (`ui_language`). This is the app-interface
  language only; the document translation target is still chosen separately.
- **More languages: Spanish, German, French.** Both the source-language and the
  translation-language selectors add Español / Deutsch / Français (source also gains
  스페인어). The engine prompts in English for these Latin-script targets and the
  bundled font renders their accents (ñ, ä, ö, ü, ß, é, è, ç …). Note: OCR of
  *scanned* Spanish/German/French PDFs needs the matching Tesseract data added to the
  bundle (only `eng` ships); text PDFs are unaffected.

### Fixed
- **Updated the bundled docx→hwpx converter to the latest skill version.** Picks up
  upstream fixes to the HWPX output — notably lines no longer overlap one another on
  a row. The bundled `convert.py` / `omml_to_hwp.py` now match the source skill; the
  `convert()` API is unchanged, so PaperKo's export wiring is untouched.
- **MDPI front page keeps its masthead and metadata sidebar in the original.** MDPI
  papers (Polymers, etc.) put the journal-name logo at the top and a narrow left
  column of front-matter (Citation, Academic Editor, Received/Accepted/Published,
  Publisher's Note, copyright/licence). These were being translated and reflowed,
  breaking the page; they now stay as-is (the journal name is a brand; the sidebar is
  boilerplate), while the title, abstract, keywords and body are translated normally.

## [1.8.0] — 2026-08-19

### Added
- **Save the translation as Hancom (.hwpx) or Word (.docx), fully offline.** In
  addition to the layout-preserving PDF, the desktop app (macOS + Windows) can now
  export the translated document as an editable *flowing* document — headings,
  paragraphs and list items in reading order at a uniform body size. HWPX is produced
  by a bundled docx→hwpx converter (no internet, no Hancom install needed); the only
  new dependency is `python-docx`, auto-installed into the bundled runtime. The "완료"
  panel gains **PDF로 저장 / 한글(HWPX) / Word(DOCX)** buttons. The docx→hwpx step runs
  in-process (not as a subprocess) so it works regardless of how Python is packaged
  and never flashes a console window on Windows, and its output is kept off the
  engine's JSON-RPC stdout. **Tables are reconstructed as real tables** (not plain
  text): column boundaries + the table's top come from PyMuPDF's table finder and
  the body rows from the page's horizontal rules, then each translated cell is
  dropped into its (row, col); the Word table converts to a native Hancom table.
  Only genuinely 2-D ruled regions (≥2 rule-separated rows filling ≥2 columns)
  become tables — including cover-page forms with tall multi-line cells — while
  unruled prose stays flowing text.
  **Two-column layout and figures are preserved too.** Each page is segmented into
  vertical *column bands* from block geometry, so an intra-page layout change is
  reproduced — a title/author block or a full-width figure/table becomes a
  1-column band, while runs of narrow blocks that fill both halves become a
  2-column section. On the first page the front matter (title, authors, highlights,
  abstract, keywords — everything above the "1. Introduction" heading) is kept as a
  single 1-column band, with the two-column body starting at the introduction. Figures — including vector plots/diagrams, which are not embedded raster
  images — are rasterized from the page region and embedded as pictures in reading
  order (they convert to native Hangul `hp:pic` objects); a full-width figure spans
  the page (its own 1-column band) rather than being squeezed into a column.
  Single-column papers are detected (from body-paragraph width) and kept entirely
  1-column, so a borderless table or a figure's stray axis labels can't be split
  into false columns/sections; a figure also absorbs the small labels around it
  (axis ticks, numbers) so they are rasterized into the picture instead of leaking
  out as stray text.
  Note: the export reconstructs the content for editing, so it does not mirror the
  PDF's exact page layout; merged/multi-line table cells are best-effort, a prose
  block inside a ruled box may occasionally render as a table, and figure/column
  placement is approximate. Section headings or captions that the layout analysis
  merged into an adjacent block stay merged (an analysis-stage limitation).

### Fixed
- **Borderless table rows keep their column alignment (PDF).** A borderless table's
  each row is one text block whose cells are separated only by spacing; translating
  it collapsed that spacing and jumbled the numbers across the figure/caption. Data
  rows just below a "Table N" caption (several tokens, mostly numbers/codes, not
  prose) are now kept in the original — the layout-preserving renderer leaves
  untouched blocks exactly where they are, so the table reads as in the source; only
  the caption is translated.
- **Borderless tables rebuilt into real grids (DOCX/HWPX).** For the flowing export,
  a borderless table under a "Table N" caption is reconstructed into an actual table:
  each cell is a separate PDF line, so cells are clustered by y into rows and by x
  into columns, all-empty rows/columns dropped, and the grid emitted as a Word table
  (converted to a native Hangul table). Simple tables come out clean; complex ones
  with merged/stacked cells are best-effort.
- **Section headings the model echoed are retried and translated.** Models sometimes
  return a common short heading unchanged (e.g. "1. Introduction" → "1. Introduction",
  "2. Optimization method" → "2. Optimization method") while translating every other
  heading. Such an echo is now detected (target equals source, no target script) and
  the heading is re-translated once with an explicit heading prompt (the number is
  kept, only the title text is translated). Journal-masthead titles are excluded so a
  brand name is not translated.
- **Abstracts ending with a DOI are translated (not skipped).** The front-matter
  metadata guard (which keeps DOI/copyright/corresponding-author lines in the
  original) matched a "[DOI: …]" anywhere in a block, so an abstract paragraph that
  ends with its DOI was wrongly excluded from translation and rendered in English.
  It now only excludes a short standalone metadata line (≤ 220 chars) or one whose
  marker sits at the very start, so a long abstract is translated.
- **No more false "재번역 필요" highlights on names/numbers/headers.** A block was
  flagged untranslated (orange highlight + retry chip) whenever its result lacked the
  target script — but author names, affiliations, journal mastheads, numeric table
  cells, equations and citations legitimately stay as-is, so a paper's first page
  was painted with false-failure orange boxes. The check now fires only on genuine
  running prose (several lowercase word-tokens, or a substantial CJK run, and mostly
  letters), so those blocks are left clean. On a KSCE first page this cleared 22 of
  23 false failures, leaving only the one truly-untranslated block.
- **Section headings split out of merged blocks (layout analysis).** PyMuPDF often
  lumps a figure caption, the following numbered section heading and its body
  paragraph into a single text block, which buried the heading inside a paragraph
  (e.g. "3.2 Details of the test specimens    The three RC specimens …") and
  mislabelled the whole block. The analyzer now splits a raw block at numbered
  section-heading boundaries ("1. Introduction", "3.1 RC specimen", "3.2 …") into
  separate heading + body blocks, and types the heading as a heading even when the
  source isn't bold. It also splits at caption-title lines, so a lumped "Fig. 2
  Mushroom-shaped head hook … / Table 1 Physical properties of nylon" becomes two
  separate captions instead of one wide block that overlaps on re-render. Applies to
  the whole pipeline (PDF re-render, HWPX, DOCX); a body sentence that merely begins
  "Fig. 7 presents …" (lowercase verb) is left intact — only a caption *title*
  (Capitalized word after the number) is a split point.
- **Expanding-target layouts (e.g. Korean→English) and tables hold together.** When
  translating into a longer language, text overflowed tight cells/boxes and was
  replaced by empty overflow markers, breaking tables. The fit floor now allows more
  shrink (min font 0.72→0.55), so expanded text fits instead of overflowing (a form
  page went 7 overflows → 1). Also, leaked LaTeX the model sometimes emits
  (`\text{ mm}`, `\leq`, `\alpha` …) is normalized to plain Unicode (mm, ≤, α) when
  inline-math rendering is off.
- **Short headings/labels no longer overflow when the translation is wider.** A
  one-word heading in a narrow box (e.g. Korean "총평" → "Overall Assessment") used
  to be replaced by an overflow marker because boxes only grew downward. Boxes now
  also grow *rightward* into the empty margin — bounded by the next block/rule so
  they never overlap a neighbouring column or table cell — so the heading renders at
  full size on one line.
- **Uniform font size across a page + correct line breaks for lists.** Line-
  fragmented documents (each PDF line a separate block) used to translate and fit
  each line independently, so a page came out with wildly varying font sizes; and a
  paragraph whose chain contained bullet items collapsed them all into one blob
  (losing the `-`/number line breaks). Continues-chains are now split into logical
  units — a heading/list item begins a new unit and its wrapped lines flow into it —
  so each numbered/bulleted item is one region on its own line and wrapped paragraph
  lines share one uniform body size. Korean line-wraps between two CJK characters now
  join without a spurious space. The translation prompt also asks the model to keep
  leading bullets (`-`, `•`, `·`), numbering (`①`, `1.`, `a)`) and line structure.
- **Body paragraphs no longer render centered.** Alignment detection mislabeled a
  full-width justified line as "center" when the page margins happened to be near-
  symmetric, so whole translated paragraphs came out centered and looked broken.
  Body blocks (paragraph/list/reference/footnote) are never centered now (only real
  headings/titles/captions may center), fixed both in detection and at render time.
- **Leaked subscript/superscript LaTeX normalized.** `f_{fu}`, `f_{yv}`, `x^{2}`
  (emitted without a backslash, so the earlier `\`-gated cleanup missed them) now
  render as `f_fu`, `f_yv`, `x^2` instead of showing the raw braces.

## [1.7.0] — 2026-08-18

### Added
- **Language selectors on the main form.** The document's source language and the
  translation language are chosen directly on the left panel, right above "번역 시작"
  (moved out of Settings), so you set them per document without opening Settings.
- **Offline mode — run fully standalone with a local model (llama.cpp).** The
  desktop app can now bundle a `llama-server` (Apple Silicon/Metal + Windows x64)
  and translate against a local GGUF model — no external vLLM/LM Studio needed.
  Settings gains a **원격 서버 ↔ 로컬 모델(오프라인)** toggle; the model is
  downloaded on first run (or selected from an existing GGUF file). Designed for
  Google's `gemma-4-e2b-it-qat`. Bundle the runtime with
  `scripts/fetch-llama-macos.sh` / `scripts/fetch-llama-windows.ps1`.

### Added
- **Source & target language selection (UI).** Settings now let you pick the
  document's original language (auto-detect / English / Korean / Japanese / Chinese
  / …) and the translation language (**한국어 / English / 日本語 / 中文**). The source
  language also drives OCR (Tesseract) language; the prompt uses Korean instructions
  for a Korean target and English instructions otherwise (avoids a Korean-output
  bias). Verified EN→KO/EN/ZH/JA all produce the right language.
- **Bundled CJK fonts (Noto Sans JP + SC).** Japanese/Chinese targets now render
  correctly — the renderer auto-selects the CJK font by target language, and the
  "is this translated?" check is language-aware (was Korean-hangul-only, which
  silently dropped non-Korean translations). Static TTF instances are used because
  PyMuPDF's HTML box misrenders CFF/OTF CJK fonts. Adds ~15 MB to the bundle.
- **Robust local-model downloader.** The offline-model download now uses resumable
  HTTP Range requests (continues a partial `.part` file), reports progress, and
  verifies SHA-256, saving to `PaperKo/models/<file>.gguf`. Defaults to Google's
  public `gemma-4-E2B-it-qat-q4_0` GGUF (~3.35 GB, no token). Optional Hugging Face
  token field for gated models. The chosen model path is remembered, so the local
  engine's run button is enabled automatically on the next launch when the file is
  present (and offline mode auto-starts if it was the active engine).
- **Inline math rendering (opt-in, off by default).** Can turn LaTeX inline math
  (`$f_y$`, `$\frac{a}{b}$` that small models emit) into inline equation images via
  matplotlib mathtext (`RenderOptions.render_inline_math`, extra `translate_engine[math]`).
  **Off by default** — subscript alignment still needs work — in which case the `$`
  delimiters are simply stripped so no literal `$` shows (`$f_y$` → `f_y`). Display
  equations remain preserved as the original either way. matplotlib is NOT bundled
  unless the feature is enabled.

### Changed
- **Page-batched translation — big quality + reliability win.** The engine now
  translates all blocks of a page in ONE call (numbered `<<<n>>>` segments,
  parsed back per block) instead of translating each block in isolation. The
  model gets full-page context, so quality rises and the "재번역 필요" count drops
  sharply; it also makes fewer, larger calls (faster). Segments the model omits or
  mangles fall back to per-block translation automatically. Verified against the
  35B: 7/7 units done, 0 failures, 2 calls (was ~7+).
- **Retranslate now works on the whole page** (with full-page context) instead of
  re-running one block in isolation, so a failed region is actually fixed and the
  result is reflected after re-render. Applies to desktop and web.
- **Tolerant placeholder handling — huge drop in "재번역 필요" with small local
  models.** Small models (e.g. gemma-4-E2B) routinely drop the internal math/
  citation tokens (⟦R1⟧ …). That used to be a HARD failure that reverted the whole
  block to English. Now the present tokens are restored, hallucinated extras
  stripped, and any dropped ones re-appended so nothing is lost — accepted as a
  soft result. Measured on a real 3-page paper with local gemma-4-E2B: **14 failed
  → 0 failed** (12 citations auto-restored).

### Fixed
- **Dense pages / large blocks fully translated with small local models.** On pages
  where a whole section is one big text block, small models (e.g. gemma-4-E2B) would
  stop early and translate only the first ~20%, leaving most of the page in the
  source language. The engine now detects a truncated translation (output far
  shorter than the source) and re-translates that block in bounded chunks, joined
  back into one region — no layout change. Strong models are unaffected (they return
  complete translations). Verified on a ZAMM paper: page-2 coverage went from ~11% to
  full with local gemma; the 35B is essentially unchanged.
- **Untranslated regions are now flagged, and retranslate respects the job's
  language/glossary.** Two linked bugs: (1) a substantial block that came back NOT
  in the target language (source echo) was silently accepted as "done" and kept as
  the original with no marker — it is now marked failed, so it shows the orange
  "재번역 필요" highlight and a retry chip. (2) Retranslate ran with hardcoded
  defaults (Korean target, no glossary), so it produced the wrong language for
  non-Korean jobs and ignored terms — it now passes the job's source/target
  language, style, and glossary snapshot. Verified: EN→JA retranslate returns
  Japanese; long untranslated blocks → failed, short acronyms stay done.
- **First-page front-matter/footer kept in the original.** Journal masthead
  ("Contents lists available…", homepage) and the bottom metadata
  (corresponding-author note, DOI, copyright, ISSN, submission dates) were being
  translated and merged into the body flow, losing the separator. They now stay in
  the original language, visually separated as in the source.
- **Cancel button now stops translation immediately.** Cancelling used to only
  prevent *new* translation units from starting — in-flight LLM calls kept running
  (seconds each with the 35B model), so the button felt dead. A watcher now aborts
  the in-flight requests at once (verified: stops in ~0.5s instead of ~5s+).
  Cancelling is reported as a clean **CANCELLED** state ("취소됨") instead of a
  scary FAILED error, and the UI shows "취소 중…" on click.

### Engine
- **Image/scanned PDF support (OCR).** Pages with no text layer are OCR'd
  automatically (`analyze(ocr="auto")`). Uses PyMuPDF's built-in Tesseract engine
  — no system Tesseract needed; `eng.traineddata` is bundled in the package
  (`translate_engine/tessdata/`) and `TESSDATA_PREFIX` is set automatically. OCR
  language is selectable (`analyze(ocr_lang=...)`, default `eng`). Applies to the
  desktop app and the web app.

### Web
- Progress is now driven by `/status` polling in addition to SSE, so the UI stays
  correct even when a proxy (Cloudflare) buffers the event stream. Collapsible
  glossary; About dialog; served-asset no-cache.

## [1.0.0] — 2026-08-11

First official cross-platform release (Windows + macOS), deployed and working
end-to-end against a vLLM (Spark) server.

### Engine (`translate_engine`)
- PDF parse → layout analysis → Korean translation (vLLM/OpenAI-compatible) →
  layout-preserving re-render, as a library + CLI + stdio JSON-RPC sidecar.
- Manuscript support: strips margin line-numbers, reconstructs paragraphs from
  line-fragmented PDFs, uniform output font size, capped line-height.
- Horizontal-rule–aware layout so translated text never overlaps separator lines.
- References excluded from translation (kept in the original language).
- Bundled open-licence Korean fonts (Nanum Gothic / Nanum Myeongjo, SIL OFL).
- Cross-platform stdin reader (thread-based) — fixes the Windows asyncio
  `_ProactorReadPipeTransport ... _empty_waiter` crash.

### Desktop app (Wails v3 + React)
- Open / analyze / translate with live progress, original↔translated compare view.
- Zoom (buttons, ⌘/Ctrl+wheel) + click-and-drag panning.
- Glossary manager (CRUD, CSV import/export) applied per job.
- Retranslate failed regions, labelled by page number (click to jump).
- Settings: fetch model list from the server into a dropdown; live LLM health test.
- Self-contained: bundles a standalone Python runtime (no system Python required).
- Windows: no console window (sidecar launched with `CREATE_NO_WINDOW`).
- App version shown in the top bar.

### Packaging
- macOS `.app` + `.dmg` (scripts/build-macos-dmg.sh).
- Windows portable `bin\` + NSIS installer including `resources\` (BUILD-WINDOWS.md).
