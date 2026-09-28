package engine

import (
	"context"
	"log/slog"
	"os"
	"path/filepath"
	"testing"
	"time"
)

// TestSidecarBridge spawns the real Python sidecar via the project venv and drives
// the no-LLM path (initialize → open → analyze → page.preview → shutdown).
// Set PAPERKO_ENGINE_ROOT to override the repo root.
func TestSidecarBridge(t *testing.T) {
	root := os.Getenv("PAPERKO_ENGINE_ROOT")
	if root == "" {
		root = "/Volumes/LLM-model/App-develop/PaperKo"
	}
	python := filepath.Join(root, ".venv", "bin", "python")
	if _, err := os.Stat(python); err != nil {
		t.Skipf("venv python not found at %s", python)
	}
	pdf := filepath.Join(root, "engine-py", "tests", "fixtures", "sample.pdf")
	if _, err := os.Stat(pdf); err != nil {
		t.Skipf("fixture pdf missing (run tests/make_fixture.py): %v", err)
	}

	dataDir := t.TempDir()
	sup := NewSupervisor(Config{
		Python:  python,
		Args:    []string{"-m", "translate_engine"},
		Env:     []string{"PYTHONPATH=" + filepath.Join(root, "engine-py")},
		DataDir: dataDir,
		LLM:     LLMConfig{BaseURL: "http://localhost:1", Model: "x", MaxConcurrency: 2, TimeoutS: 5},
	}, slog.New(slog.NewTextHandler(os.Stderr, nil)))

	statuses := make(chan string, 8)
	sup.OnStatus = func(s string) { statuses <- s }

	ctx := context.Background()
	if err := sup.Start(ctx); err != nil {
		t.Fatalf("start: %v", err)
	}
	defer sup.Stop()

	// open
	meta, err := sup.OpenDocument(ctx, pdf, "")
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	if meta.PageCount != 2 {
		t.Fatalf("expected 2 pages, got %d", meta.PageCount)
	}

	// analyze page 1 only
	jobDir := filepath.Join(dataDir, "jobs", "t1")
	if err := os.MkdirAll(jobDir, 0o755); err != nil {
		t.Fatal(err)
	}
	res, err := sup.Analyze(ctx, meta.DocID, pdf, jobDir, "t1", []int{0}, "auto", "eng")
	if err != nil {
		t.Fatalf("analyze: %v", err)
	}
	if res.IRPath == "" {
		t.Fatal("analyze returned empty ir_path")
	}
	if _, err := os.Stat(res.IRPath); err != nil {
		t.Fatalf("ir file missing: %v", err)
	}

	// page preview (base64 png)
	prev, err := sup.PagePreview(ctx, pdf, 0, 1.0)
	if err != nil {
		t.Fatalf("preview: %v", err)
	}
	if len(prev.PNGBase64) < 100 || prev.Width == 0 {
		t.Fatalf("preview looks empty: len=%d w=%d", len(prev.PNGBase64), prev.Width)
	}

	// error path: opening a missing file yields a structured code
	if _, err := sup.OpenDocument(ctx, filepath.Join(dataDir, "nope.pdf"), ""); err == nil {
		t.Fatal("expected error opening missing file")
	} else if ee, ok := err.(*EngineError); !ok || ee.Code == "" {
		t.Fatalf("expected EngineError with code, got %v", err)
	}

	// we should have seen ready
	deadline := time.After(2 * time.Second)
	sawReady := false
	for !sawReady {
		select {
		case s := <-statuses:
			if s == "ready" {
				sawReady = true
			}
		case <-deadline:
			t.Fatal("did not observe engine 'ready' status")
		}
	}
}
