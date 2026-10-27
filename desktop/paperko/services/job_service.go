package services

import (
	"io"
	"os"
	"path/filepath"
)

// TranslateJobOptions mirrors 04 §1.1 TranslateJobOptions (subset for v1 slice).
type TranslateJobOptions struct {
	Style           string `json:"style"`             // formal | concise
	EnforceGlossary bool   `json:"enforce_glossary"`
	Mode            string `json:"mode"`              // replace | interleaved
	GlossaryIDs     []int  `json:"glossary_ids"`      // selected glossaries to apply
	Pages           []int  `json:"pages"`             // 0-based pages for this batch (general-doc mode); empty = whole doc
}

// JobService: translate, render, cancel and query jobs (04 §1.1 JobService).
type JobService struct{ B *Backend }

// StartTranslation runs translate → render for an analyzed job, emitting events.
// Returns immediately; watch `job:state` for RENDERING/DONE and the output path.
func (s *JobService) StartTranslation(jobID string, opts TranslateJobOptions) error {
	job := s.B.job(jobID)
	if job == nil {
		return &notFound{jobID}
	}
	if opts.Style == "" {
		opts.Style = s.B.settings.Style
	}
	if opts.Mode == "" {
		opts.Mode = s.B.settings.Mode
	}

	// Snapshot the selected glossaries into the job dir (frozen copy, 03 §8.2).
	glossaryPath := ""
	if len(opts.GlossaryIDs) > 0 {
		glossaryPath = filepath.Join(job.JobDir, "glossary.json")
		if err := s.B.Glossary.Snapshot(opts.GlossaryIDs, glossaryPath); err != nil {
			glossaryPath = ""
		}
	}
	// remember style + mode + glossary so retranslate/resume use the same options
	s.B.mu.Lock()
	job.Style = opts.Style
	job.Mode = opts.Mode
	job.GlossaryPath = glossaryPath
	s.B.mu.Unlock()

	go s.runTranslate(job, opts.EnforceGlossary, false, opts.Pages)
	return nil
}

// ResumeJob continues an interrupted/cancelled/failed translation from its saved
// checkpoint (only the remaining TUs are translated), then renders. Options come
// from the persisted job so it works across an app restart.
func (s *JobService) ResumeJob(jobID string) error {
	job := s.B.job(jobID)
	if job == nil {
		return &notFound{jobID}
	}
	if !fileExists(job.IRPath) || !fileExists(filepath.Join(job.JobDir, "translation.json")) {
		return &notFound{jobID + " (no checkpoint)"}
	}
	go s.runTranslate(job, false, true, nil)
	return nil
}

// ListResumable returns jobs that can be continued (newest first) for the startup
// resume list. Returned copies are safe to marshal to the frontend.
func (s *JobService) ListResumable() []*Job {
	return s.B.resumableJobs()
}

// runTranslate drives translate → render for a job. resume=true reuses the
// on-disk checkpoint so only the outstanding TUs are re-run. pages (0-based, may be
// nil) restricts a batch to those pages and accumulates into the shared checkpoint.
func (s *JobService) runTranslate(job *Job, enforceGlossary, resume bool, pages []int) {
	s.B.setState(job, "TRANSLATING", "")
	options := map[string]any{
		"style":            firstNonEmpty(job.Style, s.B.settings.Style),
		"enforce_glossary": enforceGlossary,
		"max_concurrency":  s.B.settings.MaxConcurrency,
		"source_lang":      s.B.settings.SourceLang,
		"target_lang":      s.B.settings.TargetLang,
	}
	if len(pages) > 0 {
		options["pages"] = pages
	}
	tctx, tcancel := bctx()
	defer tcancel()
	tr, err := s.B.Sup.Translate(tctx, job.ID, job.IRPath, job.GlossaryPath, options, resume)
	if err != nil {
		s.B.setState(job, "FAILED", err.Error())
		return
	}
	// A cancel returns a partial result (not an error): record progress and still
	// render so the pages that finished are saved to the temp output.
	cancelled := statBool(tr.Stats, "cancelled")
	done, total := statInt(tr.Stats, "tu_done"), statInt(tr.Stats, "tu_total")
	failed := statInt(tr.Stats, "tu_failed")
	s.B.mu.Lock()
	job.TranslationPath = tr.TranslationPath
	job.OutPath = filepath.Join(job.JobDir, "output.pdf")
	job.DoneTUs, job.TotalTUs = done, total
	job.DonePages = statInt(tr.Stats, "pages_done")
	job.DonePageList = statIntSlice(tr.Stats, "done_page_list")
	if job.TotalPages == 0 {
		job.TotalPages = statInt(tr.Stats, "pages_total")
	}
	s.B.mu.Unlock()
	s.B.emit("job:translated", map[string]any{"job_id": job.ID, "stats": tr.Stats})

	if done > 0 { // nothing to render if the cancel landed before any page finished
		if err := s.render(job, job.Mode); err != nil {
			s.B.setState(job, "FAILED", err.Error())
			return
		}
	}
	switch {
	case done == 0 && cancelled:
		s.B.setState(job, "CANCELLED", "")
	case total > 0 && (done+failed) < total:
		// PARTIAL only while translatable units are still PENDING (a cancelled run
		// or an unfinished page-range batch) — those can be resumed. A document whose
		// remaining units are all done or failed is complete: blank/ad tail pages have
		// no text, and failed units are handled via retranslate, not resume.
		s.B.setState(job, "PARTIAL", "")
	default:
		s.B.setState(job, "DONE", "")
	}
}

// statBool reads a boolean from the engine's JSON stats map.
func statBool(stats map[string]any, key string) bool {
	b, _ := stats[key].(bool)
	return b
}

func firstNonEmpty(a, b string) string {
	if a != "" {
		return a
	}
	return b
}

// statInt reads an integer from the engine's JSON stats map (values arrive as float64).
func statInt(stats map[string]any, key string) int {
	switch v := stats[key].(type) {
	case float64:
		return int(v)
	case int:
		return v
	case int64:
		return int(v)
	}
	return 0
}

// statIntSlice reads a []int from the JSON stats map (a JSON array of numbers).
func statIntSlice(stats map[string]any, key string) []int {
	arr, ok := stats[key].([]any)
	if !ok {
		return nil
	}
	out := make([]int, 0, len(arr))
	for _, v := range arr {
		if f, ok := v.(float64); ok {
			out = append(out, int(f))
		}
	}
	return out
}

// DiscardJob deletes a job's temporary translation and rendered output so the user
// can start the document over from scratch. The analyzed IR is kept (no re-analyze);
// progress counters and state are reset. The job then drops out of the resume list.
func (s *JobService) DiscardJob(jobID string) error {
	job := s.B.job(jobID)
	if job == nil {
		return &notFound{jobID}
	}
	_ = os.Remove(filepath.Join(job.JobDir, "translation.json"))
	_ = os.Remove(filepath.Join(job.JobDir, "output.pdf"))
	s.B.mu.Lock()
	job.TranslationPath = ""
	job.OutPath = ""
	job.DoneTUs, job.TotalTUs, job.DonePages = 0, 0, 0
	job.DonePageList = nil
	s.B.mu.Unlock()
	s.B.setState(job, "PENDING", "") // persists; resumableJobs skips it (no checkpoint)
	return nil
}

// render runs the renderer for a job and emits job:rendered.
func (s *JobService) render(job *Job, mode string) error {
	s.B.setState(job, "RENDERING", "")
	if mode == "" {
		mode = s.B.settings.Mode
	}
	rOpts := map[string]any{
		"mode": mode, "font_family": s.B.settings.FontFamily,
		"min_font_scale": 0.55, "mark_machine_translated": true, "preview_png": false,
		"target_lang": s.B.settings.TargetLang,
	}
	rctx, rcancel := bctx()
	defer rcancel()
	rr, err := s.B.Sup.Render(rctx, job.ID, job.PDFPath, job.IRPath, job.TranslationPath, job.OutPath, rOpts)
	if err != nil {
		return err
	}
	s.B.emit("job:rendered", map[string]any{
		"job_id": job.ID, "out_path": rr.OutPath, "report": rr.Report,
	})
	return nil
}

// RetranslateUnit re-runs one failed/edited TU, then re-renders the PDF (UC-03).
func (s *JobService) RetranslateUnit(jobID, tuID string) error {
	job := s.B.job(jobID)
	if job == nil {
		return &notFound{jobID}
	}
	style := job.Style
	if style == "" {
		style = s.B.settings.Style
	}
	options := map[string]any{
		"style":           style,
		"max_concurrency": s.B.settings.MaxConcurrency,
		"source_lang":     s.B.settings.SourceLang,
		"target_lang":     s.B.settings.TargetLang,
	}
	go func() {
		ctx, cancel := bctx()
		defer cancel()
		out, err := s.B.Sup.RetranslateUnit(ctx, job.ID, job.IRPath, job.TranslationPath, tuID, job.GlossaryPath, options)
		if err != nil {
			s.B.emit("job:tu_retranslated", map[string]any{"job_id": job.ID, "tu_id": tuID, "error": err.Error()})
			return
		}
		// surface the retranslated unit's status so the UI only clears the chip
		// when it actually succeeded (a unit can fail the quality check again).
		status := ""
		if tu, ok := out["tu"].(map[string]any); ok {
			status, _ = tu["status"].(string)
		}
		s.B.emit("job:tu_retranslated", map[string]any{"job_id": job.ID, "tu_id": tuID, "status": status})
		// re-render regardless (translation.json was updated) so the page reflects it
		if err := s.render(job, ""); err != nil {
			s.B.setState(job, "FAILED", err.Error())
			return
		}
		s.B.setState(job, "DONE", "")
	}()
	return nil
}

// CancelJob signals the engine to cancel; completed work is preserved.
func (s *JobService) CancelJob(jobID string) error {
	return s.B.Sup.CancelJob(jobID)
}

// GetJob returns the current job record (state, paths).
func (s *JobService) GetJob(jobID string) *Job {
	return s.B.job(jobID)
}

// ExportOutput writes the document to a user-chosen destination as a flowing .docx
// or .hwpx file (generated on demand from the IR + translation). Translation is
// optional: once the document has been analyzed (IRPath set) it can be exported even
// without translating — blocks are emitted as their original text. When a translation
// exists it is used; the engine falls back to source for any untranslated block.
func (s *JobService) ExportOutput(jobID, dst, format string) error {
	job := s.B.job(jobID)
	if job == nil {
		return &notFound{jobID}
	}
	if job.IRPath == "" {
		return &notFound{jobID + " (not analyzed yet)"}
	}
	ctx, cancel := bctx()
	defer cancel()
	_, err := s.B.Sup.Export(ctx, job.IRPath, job.TranslationPath, job.PDFPath, dst, format)
	return err
}

// SaveOutput copies a finished job's output.pdf to a user-chosen destination.
func (s *JobService) SaveOutput(jobID, dst string) error {
	job := s.B.job(jobID)
	if job == nil {
		return &notFound{jobID}
	}
	in, err := os.Open(job.OutPath)
	if err != nil {
		return err
	}
	defer in.Close()
	out, err := os.Create(dst)
	if err != nil {
		return err
	}
	defer out.Close()
	_, err = io.Copy(out, in)
	return err
}

type notFound struct{ id string }

func (e *notFound) Error() string { return "job not found: " + e.id }
