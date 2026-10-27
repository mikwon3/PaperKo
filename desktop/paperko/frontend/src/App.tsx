import { useCallback, useEffect, useRef, useState, type WheelEvent, type MouseEvent } from 'react'
import { Events } from '@wailsio/runtime'
import { DocumentService, JobService, SettingsService, GlossaryService, UpdateService } from '../bindings/paperko/services'
import type { Settings, GlossaryMeta, FailedRegion, ResumableJob, UpdateInfo } from './types'
import SettingsModal from './SettingsModal'
import GlossaryModal from './GlossaryModal'
import AboutModal from './AboutModal'
import UpdateDialog from './UpdateDialog'
import { LangProvider, tr, type UILang } from './i18n'
import './app.css'

type Stage = 'idle' | 'analyze' | 'translate' | 'render' | 'done' | 'failed'

interface Meta {
  doc_id: string
  page_count: number
  title: string
  has_text_layer: boolean
  encrypted: boolean
  pages_without_text: number[]
}

interface LogLine { level: string; source: string; msg: string; ts: string }

// Given the pages already done (0-based), the batch size, and the doc length,
// return the next range as a 1-based "start-end" string ('' when everything is done).
function nextRangeInput(done: number[], size: number, pageCount: number): string {
  const set = new Set(done)
  let next = 0
  while (next < pageCount && set.has(next)) next++
  if (next >= pageCount) return ''
  return `${next + 1}-${Math.min(next + size, pageCount)}`
}

export default function App() {
  const [engineState, setEngineState] = useState('starting')
  const [pdfPath, setPdfPath] = useState('')
  const [meta, setMeta] = useState<Meta | null>(null)
  const [pagesSpec, setPagesSpec] = useState('')
  const [style, setStyle] = useState('formal')
  const [mode, setMode] = useState('replace')

  const [jobId, setJobId] = useState('')
  const [stage, setStage] = useState<Stage>('idle')
  const [pct, setPct] = useState(0)
  const [stateLabel, setStateLabel] = useState('')
  const [error, setError] = useState('')
  const [outPath, setOutPath] = useState('')
  const [exporting, setExporting] = useState(false)   // writing a .docx/.hwpx file

  const [page, setPage] = useState(0)
  const [srcImg, setSrcImg] = useState('')
  const [dstImg, setDstImg] = useState('')
  const [loadingPreview, setLoadingPreview] = useState(false)
  const [zoom, setZoom] = useState(1)          // 1 = fit two pages side by side
  const [viewW, setViewW] = useState(900)
  const scrollRef = useRef<HTMLDivElement>(null)

  const [logs, setLogs] = useState<LogLine[]>([])
  const [showSettings, setShowSettings] = useState(false)
  const [showGlossary, setShowGlossary] = useState(false)
  const [showAbout, setShowAbout] = useState(false)
  const [updateInfo, setUpdateInfo] = useState<UpdateInfo | null>(null)
  const [settings, setSettings] = useState<Settings | null>(null)

  const [glossaries, setGlossaries] = useState<GlossaryMeta[]>([])
  const [selectedGlossaries, setSelectedGlossaries] = useState<number[]>([])
  const [failed, setFailed] = useState<FailedRegion[]>([])
  const [renderVersion, setRenderVersion] = useState(0)
  const [resumable, setResumable] = useState<ResumableJob[]>([])
  const [docType, setDocType] = useState<'paper' | 'general'>('paper')
  const [batchInput, setBatchInput] = useState('')
  const [batchDone, setBatchDone] = useState<number[]>([])  // 0-based pages translated so far
  const [confirmDel, setConfirmDel] = useState('')          // job id (or '__batch__') pending delete confirm

  const jobIdRef = useRef('')
  const genJobRef = useRef('')          // whole-doc job for general-mode batches
  const pendingBatchRef = useRef<number[] | null>(null) // pages to translate after analyze
  const curBatchRef = useRef<number[]>([])              // pages of the batch in flight
  const exportFmtRef = useRef<null | 'docx' | 'hwpx'>(null) // export (no translation) after analyze
  const analyzedRef = useRef(false)                     // this job's IR is ready to export
  const optsRef = useRef({ style, mode, glossaryIds: selectedGlossaries })
  optsRef.current = { style, mode, glossaryIds: selectedGlossaries }
  const metaRef = useRef<Meta | null>(null); metaRef.current = meta
  const batchDoneRef = useRef<number[]>([]); batchDoneRef.current = batchDone

  const [appVersion, setAppVersion] = useState('')
  useEffect(() => { SettingsService.GetSettings().then((s) => setSettings(s as Settings)) }, [])
  // On startup, list translations that were left unfinished so the user can resume them.
  useEffect(() => { JobService.ListResumable().then((r) => setResumable((r as ResumableJob[]) || [])).catch(() => {}) }, [])
  // On startup, quietly check for a newer release (at most once a day; respects the
  // "auto-check off" and "skip this version" settings). Offer it if there is one.
  useEffect(() => {
    UpdateService.Check(false).then((i) => { if (i && i.available) setUpdateInfo(i as UpdateInfo) }).catch(() => {})
  }, [])

  // persist a small settings change (e.g. source/target language) immediately
  async function saveSettingPatch(patch: Partial<Settings>) {
    if (!settings) return
    const next = { ...settings, ...patch }
    setSettings(next)
    try { await SettingsService.SaveSettings(next as any) } catch { /* ignore */ }
  }
  useEffect(() => { SettingsService.AppVersion().then(setAppVersion).catch(() => {}) }, [])
  const loadGlossaries = useCallback(() => {
    GlossaryService.ListGlossaries().then((g) => setGlossaries((g as GlossaryMeta[]) || []))
  }, [])
  useEffect(() => { loadGlossaries() }, [loadGlossaries])

  // ---- event wiring ----
  useEffect(() => {
    const offs: Array<() => void> = []
    offs.push(Events.On('engine:status', (e: any) => setEngineState(e.data.state)))
    offs.push(Events.On('log:line', (e: any) => setLogs((l) => [...l.slice(-200), e.data as LogLine])))
    offs.push(Events.On('job:progress', (e: any) => {
      const d = e.data
      if (d.job_id !== jobIdRef.current) return
      setStage(d.stage); setPct(d.pct)
    }))
    offs.push(Events.On('job:state', (e: any) => {
      const d = e.data
      if (d.job_id !== jobIdRef.current) return
      setStateLabel(d.state)
      if (d.state === 'FAILED') { setStage('failed'); setError(d.error || '알 수 없는 오류') }
      else if (d.state === 'CANCELLED') { setStage('idle'); setError('') }
      // keep the resume list current as jobs finish or become resumable
      if (['DONE', 'FAILED', 'CANCELLED', 'PARTIAL'].includes(d.state)) {
        JobService.ListResumable().then((r) => setResumable((r as ResumableJob[]) || [])).catch(() => {})
      }
    }))
    offs.push(Events.On('job:analyzed', (e: any) => {
      const d = e.data
      if (d.job_id !== jobIdRef.current) return
      analyzedRef.current = true
      setFailed([])
      // Export-without-translation: the user asked to save the (untranslated) document
      // as Word/Hangul. Skip translation entirely and write the file from the source.
      if (exportFmtRef.current) {
        const fmt = exportFmtRef.current
        exportFmtRef.current = null
        setStage('idle')
        pickAndExport(d.job_id, fmt)
        return
      }
      const pages = pendingBatchRef.current // null in paper mode → whole doc
      pendingBatchRef.current = null
      if (pages) curBatchRef.current = pages
      JobService.StartTranslation(d.job_id, {
        style: optsRef.current.style, mode: optsRef.current.mode,
        enforce_glossary: false, glossary_ids: optsRef.current.glossaryIds,
        pages: pages || [],
      }).catch((err) => { setStage('failed'); setError(String(err)) })
    }))
    offs.push(Events.On('job:tu_failed', (e: any) => {
      const d = e.data
      if (d.job_id !== jobIdRef.current) return
      setFailed((cur) => cur.some((f) => f.tu_id === d.tu_id) ? cur : [...cur, {
        tu_id: d.tu_id, block_ids: d.block_ids || [],
        page: typeof d.page === 'number' ? d.page : undefined,
        message: (d.error && d.error.message) || '번역 실패',
      }])
    }))
    offs.push(Events.On('job:tu_retranslated', (e: any) => {
      const d = e.data
      if (d.job_id !== jobIdRef.current) return
      // only clear the chip if the unit actually succeeded; otherwise keep it so
      // the user can try again (it may have failed the quality check once more)
      if (!d.error && d.status === 'done') {
        setFailed((cur) => cur.filter((f) => f.tu_id !== d.tu_id))
      } else {
        setFailed((cur) => cur.map((f) => f.tu_id === d.tu_id ? { ...f, retrying: false } : f))
      }
    }))
    // job:translated carries the authoritative set of fully-translated pages
    // (partial pages are dropped on cancel), so update batch progress from it —
    // not from the requested range, which would be wrong after a cancel.
    offs.push(Events.On('job:translated', (e: any) => {
      const d = e.data
      if (d.job_id !== jobIdRef.current) return
      const cancelled = !!(d.stats && d.stats.cancelled)
      const done: number[] = (d.stats && d.stats.done_page_list) || []
      const tuDone = (d.stats && d.stats.tu_done) || 0
      const tuTotal = (d.stats && d.stats.tu_total) || 0
      const tuFailed = (d.stats && d.stats.tu_failed) || 0
      const pc = metaRef.current?.page_count ?? 0
      let merged: number[]
      if (!cancelled && tuTotal > 0 && (tuDone + tuFailed) >= tuTotal && pc > 0) {
        // nothing left to translate (every unit is done or failed) → the document is
        // complete; any remaining pages have no translatable text (blank pages,
        // back-cover ads). Count all pages so progress reads 100%, not 99%.
        merged = Array.from({ length: pc }, (_, i) => i)
      } else {
        // On a completed batch the whole requested range is finished — include it so
        // pages with no translatable text (covers, dividers) still count and the next
        // range advances past them. On cancel, trust only the fully-done pages.
        const add = cancelled ? done : [...done, ...curBatchRef.current]
        merged = [...new Set([...batchDoneRef.current, ...add])].sort((x, y) => x - y)
      }
      batchDoneRef.current = merged
      setBatchDone(merged)
      const size = curBatchRef.current.length || 40
      setBatchInput(nextRangeInput(merged, size, pc))
    }))
    offs.push(Events.On('job:rendered', (e: any) => {
      const d = e.data
      if (d.job_id !== jobIdRef.current) return
      setOutPath(d.out_path); setStage('done'); setPct(100)
      setRenderVersion((v) => v + 1) // cache-bust the translated preview
    }))
    return () => offs.forEach((f) => f && f())
  }, [])

  // measure the viewer width so zoom can be expressed relative to a fit. Defer to a
  // rAF and ignore sub-pixel changes so a scrollbar toggle can't feed back into the
  // image size and make the view oscillate (scrollbar-gutter: stable is the main fix).
  useEffect(() => {
    const el = scrollRef.current
    if (!el || typeof ResizeObserver === 'undefined') return
    let raf = 0
    const measure = () => {
      raf = 0
      const w = scrollRef.current?.clientWidth ?? 0
      if (w) setViewW((prev) => (Math.abs(prev - w) > 1 ? w : prev))
    }
    const ro = new ResizeObserver(() => { if (!raf) raf = requestAnimationFrame(measure) })
    ro.observe(el); measure()
    return () => { ro.disconnect(); if (raf) cancelAnimationFrame(raf) }
  }, [pdfPath])

  const zoomBy = (d: number) => setZoom((z) => Math.min(4, Math.max(0.5, Math.round((z + d) * 100) / 100)))
  const onWheelZoom = (e: WheelEvent<HTMLDivElement>) => {
    if (e.ctrlKey || e.metaKey) { e.preventDefault(); zoomBy(e.deltaY < 0 ? 0.15 : -0.15) }
  }

  // click-and-hold to pan the zoomed view (grab-to-drag, like a PDF/map viewer)
  const pan = useRef<{ x: number; y: number; sl: number; st: number } | null>(null)
  const onPanStart = (e: MouseEvent<HTMLDivElement>) => {
    const el = scrollRef.current
    if (e.button !== 0 || !el) return
    pan.current = { x: e.clientX, y: e.clientY, sl: el.scrollLeft, st: el.scrollTop }
    el.classList.add('grabbing')
    e.preventDefault()
  }
  const onPanMove = (e: MouseEvent<HTMLDivElement>) => {
    const p = pan.current, el = scrollRef.current
    if (!p || !el) return
    el.scrollLeft = p.sl - (e.clientX - p.x)
    el.scrollTop = p.st - (e.clientY - p.y)
  }
  const onPanEnd = () => { pan.current = null; scrollRef.current?.classList.remove('grabbing') }

  // ---- preview loading ----
  // Render the page bitmap once at a crisp DPI; CSS handles smooth zoom so
  // zooming never refetches or flashes.
  const loadPreview = useCallback(async (p: number) => {
    if (!pdfPath) return
    setLoadingPreview(true)
    try {
      setSrcImg(await DocumentService.GetPagePreview(pdfPath, p, 2.2))
      if (outPath) setDstImg(await DocumentService.GetPagePreview(outPath, p, 2.2))
      else setDstImg('')
    } catch { /* ignore */ } finally { setLoadingPreview(false) }
  }, [pdfPath, outPath])

  useEffect(() => { loadPreview(page) }, [page, loadPreview, renderVersion])

  function retranslate(tuId: string) {
    setFailed((cur) => cur.map((f) => f.tu_id === tuId ? { ...f, retrying: true } : f))
    JobService.RetranslateUnit(jobIdRef.current, tuId).catch(() => {
      setFailed((cur) => cur.map((f) => f.tu_id === tuId ? { ...f, retrying: false } : f))
    })
  }

  // ---- actions ----
  async function openPdf() {
    const path = await DocumentService.PickPDF()
    if (!path) return
    resetJob(); setPdfPath(path); setPage(0)
    genJobRef.current = ''; setBatchDone([]); setBatchInput('')
    try {
      const m = await DocumentService.OpenDocument(path, '') as Meta
      setMeta(m)
      // Recommend general-document mode for long documents (specs, guidebooks…),
      // where translating/resuming in page-range batches is the better workflow.
      setDocType(m.page_count > 30 ? 'general' : 'paper')
    }
    catch (e) { setError(String(e)) }
  }

  // "1-40, 45, 60-62" (1-based) → sorted unique 0-based page indices, clamped to the doc.
  function parsePageSpec(spec: string, maxPages: number): number[] {
    const out = new Set<number>()
    for (const part of spec.split(',')) {
      const s = part.trim(); if (!s) continue
      const m = s.match(/^(\d+)\s*-\s*(\d+)$/)
      if (m) { for (let i = +m[1]; i <= +m[2]; i++) out.add(i - 1) }
      else if (/^\d+$/.test(s)) out.add(+s - 1)
    }
    return [...out].filter((p) => p >= 0 && p < maxPages).sort((a, b) => a - b)
  }

  function resetJob() {
    setJobId(''); jobIdRef.current = ''
    curBatchRef.current = []
    analyzedRef.current = false
    setStage('idle'); setPct(0); setStateLabel(''); setError('')
    setOutPath(''); setDstImg('')
  }

  async function startTranslation() {
    if (!pdfPath) return
    resetJob(); setStage('analyze')
    try {
      const id = await DocumentService.AnalyzeDocument(pdfPath, '', pagesSpec)
      setJobId(id); jobIdRef.current = id
    } catch (e) { setStage('failed'); setError(String(e)) }
  }

  // General-document mode: translate one page-range batch. The whole document is
  // analyzed once (first batch); later batches translate directly into the same
  // job so all batches accumulate into one output document.
  function translateBatch() {
    if (!pdfPath || !meta) return
    const pgs = batchInput.trim()
      ? parsePageSpec(batchInput, meta.page_count)
      : Array.from({ length: meta.page_count }, (_, i) => i) // empty = whole doc
    if (pgs.length === 0) { setError(t('유효한 페이지 범위를 입력하세요 (예: 1-40)')); return }
    setError(''); curBatchRef.current = pgs
    if (genJobRef.current) {
      setJobId(genJobRef.current); jobIdRef.current = genJobRef.current
      setStage('translate'); setStateLabel('')
      JobService.StartTranslation(genJobRef.current, {
        style: optsRef.current.style, mode: optsRef.current.mode,
        enforce_glossary: false, glossary_ids: optsRef.current.glossaryIds, pages: pgs,
      }).catch((e) => { setStage('failed'); setError(String(e)) })
    } else {
      resetJob(); setStage('analyze'); pendingBatchRef.current = pgs
      DocumentService.AnalyzeDocument(pdfPath, '', '').then((id) => {
        setJobId(id); jobIdRef.current = id; genJobRef.current = id
      }).catch((e) => { setStage('failed'); setError(String(e)) })
    }
  }

  // Continue an unfinished translation: open the document in general/batch mode,
  // restore how far it got, and pre-fill the next range. The user picks/adjusts the
  // range and presses translate — we do NOT auto-translate the whole remainder.
  async function resume(job: ResumableJob) {
    resetJob()
    setPdfPath(job.pdf_path); setPage(0)
    setJobId(job.id); jobIdRef.current = job.id
    genJobRef.current = job.id; curBatchRef.current = []
    setResumable((cur) => cur.filter((r) => r.id !== job.id))
    // Restore progress as the contiguous prefix up to the furthest translated page.
    // The engine's done list omits pages with no translatable text (covers, dividers);
    // for the usual front-to-back workflow those are effectively done, so counting the
    // whole prefix gives the right "X/Y pages" and starts the next range right after it.
    const raw = job.done_page_list || []
    const maxDone = raw.length ? Math.max(...raw) : -1
    const done = maxDone >= 0 ? Array.from({ length: maxDone + 1 }, (_, i) => i) : []
    batchDoneRef.current = done; setBatchDone(done)
    // Show the previously-translated output: point the preview at the temp output.pdf
    // and open the last translated page so the user sees where they left off.
    if (job.out_path) setOutPath(job.out_path)
    if (maxDone >= 0) setPage(maxDone)
    try {
      const m = await DocumentService.OpenDocument(job.pdf_path, '') as Meta
      setMeta(m); setDocType('general')
      setBatchInput(nextRangeInput(done, 40, m.page_count))
    } catch { /* preview optional */ }
  }

  // Delete a document's temporary translations and start over. Keeps the analyzed
  // IR (no re-analyze); clears the checkpoint, output and progress. Confirmation is
  // handled inline by the caller (window.confirm doesn't work in the webview).
  function discardTemp(jobID: string, resetView: boolean) {
    if (!jobID) return
    setConfirmDel('')
    JobService.DiscardJob(jobID).then(() => {
      setResumable((cur) => cur.filter((r) => r.id !== jobID))
      if (resetView) {
        batchDoneRef.current = []; setBatchDone([])
        const pc = metaRef.current?.page_count ?? 0
        setBatchInput(nextRangeInput([], 40, pc))
        setStage('idle'); setStateLabel(''); setError(''); setOutPath(''); setDstImg('')
      }
    }).catch((e) => setError(String(e)))
  }

  function cancel() { if (jobIdRef.current) { setStateLabel('취소 중…'); JobService.CancelJob(jobIdRef.current) } }

  async function save(fmt: 'pdf' | 'docx' | 'hwpx' = 'pdf') {
    if (!jobId || (fmt === 'pdf' && !outPath)) return
    if (fmt === 'pdf') {
      const base = (metaRef.current?.title || 'translated').replace(/[^\w가-힣.-]+/g, '_').slice(0, 60)
      const dst = await DocumentService.PickSavePath(`${base}_KO.${fmt}`)
      if (!dst) return
      try { await JobService.SaveOutput(jobId, dst) } catch (e) { setError(String(e)) }
      return
    }
    await pickAndExport(jobId, fmt, true)
  }

  // Write a job's document as a .docx/.hwpx file. `translated` only changes the
  // suggested filename suffix (_KO) — the engine emits a translation when the job has
  // one and falls back to the original text per block otherwise, so this also serves
  // the "export without translating" path.
  async function pickAndExport(id: string, fmt: 'docx' | 'hwpx', translated = false) {
    const base = (metaRef.current?.title || 'document').replace(/[^\w가-힣.-]+/g, '_').slice(0, 60)
    const dst = await DocumentService.PickSavePath(`${base}${translated ? '_KO' : ''}.${fmt}`)
    if (!dst) { setExporting(false); return }
    setExporting(true); setError('')
    try { await JobService.ExportOutput(id, dst, fmt) }
    catch (e) { setError(String(e)) }
    finally { setExporting(false) }
  }

  // Save the current PDF as Word/Hangul WITHOUT translating. Analyze first if needed
  // (the job:analyzed handler then exports), otherwise export the already-analyzed job.
  async function exportSourceOnly(fmt: 'docx' | 'hwpx') {
    if (!pdfPath || busy || exporting) return
    if (jobId && analyzedRef.current) { await pickAndExport(jobId, fmt, false); return }
    exportFmtRef.current = fmt
    resetJob(); setStage('analyze'); setExporting(true)
    try {
      const id = await DocumentService.AnalyzeDocument(pdfPath, '', '')
      setJobId(id); jobIdRef.current = id
    } catch (e) {
      setStage('failed'); setError(String(e)); setExporting(false); exportFmtRef.current = null
    }
  }

  const busy = stage === 'analyze' || stage === 'translate' || stage === 'render'
  const lang: UILang = (settings?.ui_language as UILang) || 'ko'
  const t = (ko: string) => tr(lang, ko)
  const stageLabelKo: Record<Stage, string> = {
    idle: '대기', analyze: '레이아웃 분석', translate: '번역', render: '재조판',
    done: '완료', failed: '실패',
  }
  // Localize a job-state enum (from Go) or a transient label. Enums map to both
  // languages here; anything else falls back to the i18n table.
  const stateLabelText = (s: string): string => {
    const ko: Record<string, string> = {
      PENDING: '대기', ANALYZING: '분석 중', TRANSLATING: '번역 중', RENDERING: '재조판',
      DONE: '완료', PARTIAL: '일부 완료', CANCELLED: '취소됨', FAILED: '실패', INTERRUPTED: '중단됨',
    }
    const en: Record<string, string> = {
      PENDING: 'Pending', ANALYZING: 'Analyzing', TRANSLATING: 'Translating', RENDERING: 'Rendering',
      DONE: 'Done', PARTIAL: 'Partly done', CANCELLED: 'Cancelled', FAILED: 'Failed', INTERRUPTED: 'Interrupted',
    }
    if (s in ko) return (lang === 'en' ? en : ko)[s]
    return t(s)
  }

  return (
    <LangProvider lang={lang}>
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <span className="logo">📄</span>
          <div>
            <div className="title">PaperKo {appVersion && <span className="version">v{appVersion}</span>}</div>
            <div className="subtitle">{t('학술 논문 PDF 한국어 번역')}</div>
          </div>
        </div>
        <div className="topbar-right">
          <span className={`engine ${engineState}`}><i /> {t('엔진')}: {engineState === 'ready' ? t('준비됨') : engineState}</span>
          <button className="ghost" onClick={() => setShowAbout(true)}>{t('ⓘ 정보')}</button>
          <button className="ghost" onClick={() => setShowSettings(true)}>{t('⚙ 설정')}</button>
        </div>
      </header>

      <div className="body">
        <aside className="sidebar">
          <button className="primary block" onClick={openPdf}>{t('📂 PDF 열기')}</button>

          {meta && (
            <div className="metacard">
              <div className="meta-title" title={meta.title}>{meta.title || t('(제목 없음)')}</div>
              <div className="meta-row"><span>{t('페이지')}</span><b>{meta.page_count}</b></div>
              <div className="meta-row"><span>{t('텍스트 레이어')}</span><b>{meta.has_text_layer ? t('있음') : t('없음(OCR)')}</b></div>
              {meta.encrypted && <div className="meta-row warn"><span>{t('암호화')}</span><b>{t('예')}</b></div>}
            </div>
          )}

          {meta && (
            <div className="field">
              <label>{t('문서 유형')}</label>
              <div className="seg">
                <button className={docType === 'paper' ? 'seg-on' : ''} disabled={busy}
                  onClick={() => setDocType('paper')}>{t('학술 논문')}</button>
                <button className={docType === 'general' ? 'seg-on' : ''} disabled={busy}
                  onClick={() => setDocType('general')}>{t('일반 문서')}</button>
              </div>
              {meta.page_count > 30 && docType === 'general' && (
                <div className="hint">{t('페이지가 많아 일반 문서 모드를 추천합니다. 페이지 범위로 나누어·이어서 번역할 수 있습니다.')}</div>
              )}
            </div>
          )}

          {docType === 'paper' && (
            <div className="field">
              <label>{t('페이지 범위')}</label>
              <input value={pagesSpec} onChange={(e) => setPagesSpec(e.target.value)}
                placeholder={t('예: 1-3 (비우면 전체)')} disabled={busy} />
            </div>
          )}

          {docType === 'general' && meta && (
            <div className="batch-card">
              <div className="field">
                <label>{t('이번에 번역할 페이지 범위')}</label>
                <input value={batchInput} onChange={(e) => setBatchInput(e.target.value)}
                  placeholder={t('예: 1-40 (비우면 전체)')} disabled={busy} />
              </div>
              {batchDone.length > 0 && (
                <div className="batch-done">
                  {t('번역 완료')}: {batchDone.length}/{meta.page_count}{t('쪽')} ({Math.round(batchDone.length * 100 / meta.page_count)}%)
                  <div className="bar sm"><div className="bar-fill"
                    style={{ width: `${Math.round(batchDone.length * 100 / meta.page_count)}%` }} /></div>
                </div>
              )}
              {!busy && (
                <button className="primary block" onClick={translateBatch} disabled={!meta}>
                  {batchDone.length > 0 ? t('▶ 이 범위 이어서 번역') : t('▶ 이 범위 번역')}
                </button>
              )}
              {!busy && batchDone.length > 0 && (
                confirmDel === '__batch__' ? (
                  <div className="save-row">
                    <button className="danger" onClick={() => discardTemp(genJobRef.current, true)}>{t('삭제 확인')}</button>
                    <button className="ghost" onClick={() => setConfirmDel('')}>{t('취소')}</button>
                  </div>
                ) : (
                  <button className="ghost block danger-text" onClick={() => setConfirmDel('__batch__')}>
                    {t('🗑 임시본 삭제 후 처음부터')}
                  </button>
                )
              )}
            </div>
          )}
          <div className="field">
            <label>{t('문체')}</label>
            <select value={style} onChange={(e) => setStyle(e.target.value)} disabled={busy}>
              <option value="formal">{t('격식체(논문투)')}</option>
              <option value="concise">{t('간결체')}</option>
            </select>
          </div>
          <div className="field">
            <label>{t('출력 모드')}</label>
            <select value={mode} onChange={(e) => setMode(e.target.value)} disabled={busy}>
              <option value="replace">{t('번역 대체본')}</option>
              <option value="interleaved">{t('대역본(원문+번역 교차)')}</option>
            </select>
          </div>

          <div className="field">
            <div className="field-head">
              <label>{t('용어집 적용')}</label>
              <button className="link" onClick={() => setShowGlossary(true)}>{t('관리')}</button>
            </div>
            {glossaries.length === 0 && <div className="dim sm">{t('등록된 용어집 없음 · [관리]에서 추가')}</div>}
            <div className="gloss-picker">
              {glossaries.map((g) => (
                <label key={g.id} className="gloss-check">
                  <input type="checkbox" disabled={busy}
                    checked={selectedGlossaries.includes(g.id)}
                    onChange={(e) => setSelectedGlossaries((cur) =>
                      e.target.checked ? [...cur, g.id] : cur.filter((x) => x !== g.id))} />
                  {g.name} <span className="dim">({g.term_count})</span>
                </label>
              ))}
            </div>
          </div>

          <div className="row2">
            <div className="field">
              <label>{t('원문 언어')}</label>
              <select value={settings?.source_lang || 'auto'} disabled={busy || !settings}
                onChange={(e) => saveSettingPatch({ source_lang: e.target.value })}>
                <option value="auto">{t('자동 감지')}</option>
                <option value="en">{t('영어')}</option>
                <option value="ko">{t('한국어')}</option>
                <option value="ja">{t('일본어')}</option>
                <option value="zh">{t('중국어')}</option>
                <option value="es">{t('스페인어')}</option>
                <option value="de">{t('독일어')}</option>
                <option value="fr">{t('프랑스어')}</option>
              </select>
            </div>
            <div className="field">
              <label>{t('번역 언어')}</label>
              <select value={settings?.target_lang || 'Korean'} disabled={busy || !settings}
                onChange={(e) => saveSettingPatch({ target_lang: e.target.value })}>
                <option value="Korean">한국어</option>
                <option value="English">English</option>
                <option value="Japanese">日本語</option>
                <option value="Chinese">中文(简体)</option>
                <option value="Spanish">Español</option>
                <option value="German">Deutsch</option>
                <option value="French">Français</option>
              </select>
            </div>
          </div>

          {resumable.length > 0 && !busy && (
            <div className="resume-card">
              <div className="resume-head">{t('⏸ 이어할 번역')} ({resumable.length})</div>
              {resumable.slice(0, 5).map((r) => {
                // count the contiguous prefix up to the last translated page (blank
                // pages within it are effectively done) — matches the batch view
                const doneN = r.done_page_list && r.done_page_list.length
                  ? Math.max(...r.done_page_list) + 1 : r.done_pages
                const pctR = r.total_pages > 0 ? Math.round(doneN * 100 / r.total_pages)
                  : (r.total_tus > 0 ? Math.round(r.done_tus * 100 / r.total_tus) : 0)
                return (
                  <div key={r.id} className="resume-row">
                    <div className="resume-info">
                      <div className="resume-title" title={r.pdf_path}>{r.title}</div>
                      <div className="resume-sub">
                        {r.total_pages > 0
                          ? `${doneN}/${r.total_pages}${t('쪽')} (${pctR}%)`
                          : stateLabelText(r.state)}
                      </div>
                    </div>
                    {confirmDel === r.id ? (
                      <>
                        <button className="danger sm" onClick={() => discardTemp(r.id, false)}>{t('삭제')}</button>
                        <button className="ghost sm x" onClick={() => setConfirmDel('')}>✕</button>
                      </>
                    ) : (
                      <>
                        <button className="primary sm" onClick={() => resume(r)}>{t('이어받기')}</button>
                        <button className="ghost sm x" title={t('임시본 삭제')}
                          onClick={() => setConfirmDel(r.id)}>🗑</button>
                      </>
                    )}
                  </div>
                )
              })}
            </div>
          )}

          {docType === 'paper' && !busy && stage !== 'done' && (
            <button className="primary block" onClick={startTranslation} disabled={!meta}>{t('▶ 번역 시작')}</button>
          )}
          {/* Save the PDF as an editable Word/Hangul file WITHOUT translating it —
              the layout engine turns the original text into a flowing document. */}
          {meta && !busy && stage !== 'done' && (
            <div className="save-row no-trans">
              <span className="save-label">{t('번역 없이 저장')}</span>
              <button className="ghost" disabled={exporting}
                onClick={() => exportSourceOnly('hwpx')}>{t('📝 한글(HWPX)')}</button>
              <button className="ghost" disabled={exporting}
                onClick={() => exportSourceOnly('docx')}>{t('📄 Word(DOCX)')}</button>
            </div>
          )}
          {exporting && <div className="state-label">{t('문서 파일로 변환 중…')}</div>}
          {/* Continue the whole document from where it stopped — available in both
              modes (a document-level action). In general mode it finishes every
              remaining page at once, complementing the batch card's range control. */}
          {!busy && jobId && (stage === 'failed' || stateLabel === 'PARTIAL' || stateLabel === 'CANCELLED') && (
            <button className="primary block" onClick={() => {
              setStage('translate'); setError(''); setStateLabel('이어받는 중…')
              JobService.ResumeJob(jobId).catch((e) => { setStage('failed'); setError(String(e)) })
            }}>{docType === 'general' ? t('⏵ 나머지 전체 이어서 번역') : t('⏵ 이어서 번역')}</button>
          )}
          {busy && <button className="danger block" onClick={cancel}>{t('■ 취소')}</button>}
          {stage === 'done' && (
            <>
              <button className="primary block" onClick={() => save('pdf')}>{t('💾 PDF로 저장')}</button>
              <div className="save-row">
                <button className="ghost" onClick={() => save('hwpx')}>{t('📝 한글(HWPX)')}</button>
                <button className="ghost" onClick={() => save('docx')}>{t('📄 Word(DOCX)')}</button>
              </div>
              {docType === 'paper' && (
                <button className="ghost block" onClick={startTranslation}>{t('다시 번역')}</button>
              )}
            </>
          )}

          {(busy || stage === 'done' || stage === 'failed') && (
            <div className="progress-card">
              <div className="progress-head"><span>{t(stageLabelKo[stage])}</span><span>{pct}%</span></div>
              <div className="bar"><div className="bar-fill" style={{ width: `${pct}%` }} /></div>
              {stateLabel && <div className="state-label">{stateLabelText(stateLabel)}</div>}
              {error && <div className="err">⚠ {t(error)}</div>}
            </div>
          )}

          {failed.length > 0 && (
            <div className="failed-card">
              <div className="failed-head">{t('⚠ 재번역 필요')} ({failed.length})</div>
              {[...failed].sort((a, b) => (a.page ?? 1e9) - (b.page ?? 1e9)).map((f) => (
                <div key={f.tu_id} className="failed-row">
                  <button className="f-page" title={`${f.tu_id} · ${t(f.message)}`}
                    onClick={() => f.page !== undefined && setPage(f.page)}>
                    {f.page !== undefined ? (lang === 'en' ? `p.${f.page + 1}` : `${f.page + 1}쪽`) : f.tu_id}
                  </button>
                  <div className="spacer" />
                  <button className="ghost sm" disabled={f.retrying || busy}
                    onClick={() => retranslate(f.tu_id)}>
                    {f.retrying ? t('재번역 중…') : t('다시 번역')}
                  </button>
                </div>
              ))}
            </div>
          )}
        </aside>

        <main className="viewer">
          {!pdfPath && (
            <div className="empty">
              <div className="empty-icon hangul">
                <span className="en">A</span>
                <span className="arrow">→</span>
                <span className="ko">가나다</span>
              </div>
              <p>{t('왼쪽')} <b>{t('PDF 열기')}</b>{lang === 'en' ? ' ' : ''}{t('로 논문을 불러오세요.')}</p>
              <p className="dim">{t('원본 레이아웃(수식·표·그림·2단)을 유지한 채 한국어로 번역합니다.')}</p>
            </div>
          )}

          {pdfPath && (() => {
            const gap = 16
            // subtract the container's horizontal padding (12px × 2) so two columns
            // fit exactly at zoom 1 and no horizontal scrollbar toggles the layout
            const colW = Math.max(120, ((viewW - gap - 24) / 2) * zoom)
            return (
            <>
              <div className="pager">
                <button onClick={() => setPage((p) => Math.max(0, p - 1))} disabled={page <= 0}>◀</button>
                <span>{page + 1} / {meta?.page_count ?? '?'}</span>
                <button onClick={() => setPage((p) => Math.min((meta?.page_count ?? 1) - 1, p + 1))}
                  disabled={!!meta && page >= meta.page_count - 1}>▶</button>
                {loadingPreview && <span className="dim"> · {t('렌더링…')}</span>}
                <div className="spacer" />
                <div className="zoom-ctl" title={t('Ctrl(⌘)+휠로도 확대/축소')}>
                  <button onClick={() => zoomBy(-0.25)} disabled={zoom <= 0.5}>－</button>
                  <span className="zoom-val" onClick={() => setZoom(1)}>{Math.round(zoom * 100)}%</span>
                  <button onClick={() => zoomBy(0.25)} disabled={zoom >= 4}>＋</button>
                  <button className="ghost sm" onClick={() => setZoom(1)}>{t('맞춤')}</button>
                </div>
              </div>
              <div className="compare-scroll" ref={scrollRef} onWheel={onWheelZoom}
                onMouseDown={onPanStart} onMouseMove={onPanMove}
                onMouseUp={onPanEnd} onMouseLeave={onPanEnd}>
                <div className="compare" style={{ gap }}>
                  <figure style={{ width: colW }}>
                    <figcaption>{t('원문')}</figcaption>
                    {srcImg ? <img src={srcImg} alt="source" style={{ width: colW }} />
                      : <div className="ph" style={{ width: colW, height: colW * 1.3 }} />}
                  </figure>
                  <figure style={{ width: colW }}>
                    <figcaption>{t('번역 ')}{outPath ? '' : t('(대기)')}</figcaption>
                    {dstImg ? <img src={dstImg} alt="translated" style={{ width: colW }} />
                      : <div className="ph" style={{ width: colW, height: colW * 1.3 }}>
                          {stage === 'done' ? '' : t('번역을 시작하세요')}</div>}
                  </figure>
                </div>
              </div>
            </>
            )
          })()}
        </main>
      </div>

      <footer className="logbar">
        {logs.slice(-3).map((l, i) => (
          <div key={i} className="logline"><span className="lsrc">{l.source}</span> {l.msg}</div>
        ))}
      </footer>

      {showSettings && settings && (
        <SettingsModal initial={settings}
          onClose={() => setShowSettings(false)}
          onSaved={(s) => { setSettings(s); setShowSettings(false) }}
          onFoundUpdate={(i) => { setShowSettings(false); setUpdateInfo(i) }} />
      )}
      {showGlossary && (
        <GlossaryModal onClose={() => { setShowGlossary(false); loadGlossaries() }} />
      )}
      {showAbout && <AboutModal onClose={() => setShowAbout(false)} />}
      {updateInfo && <UpdateDialog info={updateInfo} onClose={() => setUpdateInfo(null)} />}
    </div>
    </LangProvider>
  )
}
