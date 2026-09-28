package engine

import (
	"context"
	"log/slog"
	"os"
	"path/filepath"
	"testing"
)

// TestSidecarConfigure proves that Configure() updates the model the running
// engine uses: start with a bogus model (model_found=false), then reconfigure to
// a real one (model_found=true) without restarting.
func TestSidecarConfigure(t *testing.T) {
	base := os.Getenv("PAPERKO_TEST_LLM")
	if base == "" {
		t.Skip("set PAPERKO_TEST_LLM to run")
	}
	realModel := os.Getenv("PAPERKO_TEST_MODEL")
	if realModel == "" {
		realModel = "qwen3.5-9b"
	}
	root := os.Getenv("PAPERKO_ENGINE_ROOT")
	if root == "" {
		root = "/Volumes/LLM-model/App-develop/PaperKo"
	}
	sup := NewSupervisor(Config{
		Python:  filepath.Join(root, ".venv", "bin", "python"),
		Args:    []string{"-m", "translate_engine"},
		Env:     []string{"PYTHONPATH=" + filepath.Join(root, "engine-py")},
		DataDir: t.TempDir(),
		LLM:     LLMConfig{BaseURL: base, Model: "does-not-exist", MaxConcurrency: 2, TimeoutS: 30},
	}, slog.New(slog.NewTextHandler(os.Stderr, nil)))
	ctx := context.Background()
	if err := sup.Start(ctx); err != nil {
		t.Fatal(err)
	}
	defer sup.Stop()

	h1, err := sup.Health(ctx, nil)
	if err != nil {
		t.Fatal(err)
	}
	if h1.ModelFound {
		t.Fatal("bogus model should not be found")
	}
	if err := sup.Configure(ctx, LLMConfig{BaseURL: base, Model: realModel, MaxConcurrency: 2, TimeoutS: 30}); err != nil {
		t.Fatalf("configure: %v", err)
	}
	h2, err := sup.Health(ctx, nil)
	if err != nil {
		t.Fatal(err)
	}
	if !h2.ModelFound {
		t.Fatalf("after reconfigure, model %q should be found", realModel)
	}
}

// TestSidecarRetranslate exercises the UC-03 path through the supervisor:
// analyze → translate (1 page) → tu.retranslate on the first unit.
func TestSidecarRetranslate(t *testing.T) {
	base := os.Getenv("PAPERKO_TEST_LLM")
	if base == "" {
		t.Skip("set PAPERKO_TEST_LLM to run")
	}
	model := os.Getenv("PAPERKO_TEST_MODEL")
	if model == "" {
		model = "unsloth/Qwen3.6-35B-A3B-NVFP4-Fast"
	}
	root := os.Getenv("PAPERKO_ENGINE_ROOT")
	if root == "" {
		root = "/Volumes/LLM-model/App-develop/PaperKo"
	}
	pdf := filepath.Join(root, "engine-py", "tests", "fixtures", "sample.pdf")
	dataDir := t.TempDir()
	sup := NewSupervisor(Config{
		Python: filepath.Join(root, ".venv", "bin", "python"),
		Args:   []string{"-m", "translate_engine"},
		Env:    []string{"PYTHONPATH=" + filepath.Join(root, "engine-py")},
		DataDir: dataDir,
		LLM:    LLMConfig{BaseURL: base, Model: model, MaxConcurrency: 6, TimeoutS: 120},
	}, slog.New(slog.NewTextHandler(os.Stderr, nil)))
	ctx := context.Background()
	if err := sup.Start(ctx); err != nil {
		t.Fatal(err)
	}
	defer sup.Stop()

	meta, _ := sup.OpenDocument(ctx, pdf, "")
	jobDir := filepath.Join(dataDir, "jobs", "t1")
	os.MkdirAll(jobDir, 0o755)
	ar, err := sup.Analyze(ctx, meta.DocID, pdf, jobDir, "t1", []int{0}, "auto", "eng")
	if err != nil {
		t.Fatal(err)
	}
	tr, err := sup.Translate(ctx, "t1", ar.IRPath, "", map[string]any{"style": "formal", "max_concurrency": 6}, false)
	if err != nil {
		t.Fatal(err)
	}
	out, err := sup.RetranslateUnit(ctx, "t1", ar.IRPath, tr.TranslationPath, "tu_0001", "", nil)
	if err != nil {
		t.Fatalf("retranslate: %v", err)
	}
	tu, _ := out["tu"].(map[string]any)
	if tu == nil || tu["status"] != "done" || tu["target_text"] == "" {
		t.Fatalf("retranslate returned bad TU: %v", out)
	}
	t.Logf("retranslated tu_0001 -> %v", tu["target_text"])

	// re-render after retranslate (the JobService path the desktop UI uses)
	outPDF := filepath.Join(jobDir, "output.pdf")
	rr, err := sup.Render(ctx, "t1", pdf, ar.IRPath, tr.TranslationPath, outPDF,
		map[string]any{"mode": "replace", "font_family": "nanum"})
	if err != nil {
		t.Fatalf("render after retranslate: %v", err)
	}
	if _, err := os.Stat(outPDF); err != nil {
		t.Fatalf("output pdf missing after re-render: %v", err)
	}
	t.Logf("re-render report: %v", rr.Report)
}

// TestSidecarTranslate runs the full translate→render path through the supervisor
// against a live OpenAI-compatible server. Guarded by PAPERKO_TEST_LLM to keep CI
// offline; set it to the base URL (e.g. http://localhost:1234/v1) to run.
func TestSidecarTranslate(t *testing.T) {
	base := os.Getenv("PAPERKO_TEST_LLM")
	if base == "" {
		t.Skip("set PAPERKO_TEST_LLM to run the live translate test")
	}
	model := os.Getenv("PAPERKO_TEST_MODEL")
	if model == "" {
		model = "qwen/qwen3.6-35b-a3b"
	}
	root := os.Getenv("PAPERKO_ENGINE_ROOT")
	if root == "" {
		root = "/Volumes/LLM-model/App-develop/PaperKo"
	}
	python := filepath.Join(root, ".venv", "bin", "python")
	pdf := filepath.Join(root, "engine-py", "tests", "fixtures", "sample.pdf")

	dataDir := t.TempDir()
	sup := NewSupervisor(Config{
		Python:  python,
		Args:    []string{"-m", "translate_engine"},
		Env:     []string{"PYTHONPATH=" + filepath.Join(root, "engine-py")},
		DataDir: dataDir,
		LLM:     LLMConfig{BaseURL: base, Model: model, MaxConcurrency: 6, TimeoutS: 120},
	}, slog.New(slog.NewTextHandler(os.Stderr, nil)))

	ctx := context.Background()
	if err := sup.Start(ctx); err != nil {
		t.Fatal(err)
	}
	defer sup.Stop()

	// health
	h, err := sup.Health(ctx, nil)
	if err != nil || !h.OK {
		t.Fatalf("health failed: %+v err=%v", h, err)
	}

	meta, err := sup.OpenDocument(ctx, pdf, "")
	if err != nil {
		t.Fatal(err)
	}
	jobDir := filepath.Join(dataDir, "jobs", "t1")
	os.MkdirAll(jobDir, 0o755)

	ar, err := sup.Analyze(ctx, meta.DocID, pdf, jobDir, "t1", []int{0}, "auto", "eng")
	if err != nil {
		t.Fatalf("analyze: %v", err)
	}
	tr, err := sup.Translate(ctx, "t1", ar.IRPath, "", map[string]any{
		"style": "formal", "enforce_glossary": false, "max_concurrency": 6,
	}, false)
	if err != nil {
		t.Fatalf("translate: %v", err)
	}
	t.Logf("translate stats: %v", tr.Stats)

	out := filepath.Join(jobDir, "output.pdf")
	rr, err := sup.Render(ctx, "t1", pdf, ar.IRPath, tr.TranslationPath, out, map[string]any{
		"mode": "replace", "font_family": "noto", "mark_machine_translated": true,
	})
	if err != nil {
		t.Fatalf("render: %v", err)
	}
	if _, err := os.Stat(rr.OutPath); err != nil {
		t.Fatalf("output pdf missing: %v", err)
	}
	t.Logf("render report: %v", rr.Report)
}
