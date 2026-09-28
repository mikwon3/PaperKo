package main

import (
	"context"
	"embed"
	"log"
	"log/slog"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"

	"github.com/wailsapp/wails/v3/pkg/application"

	"paperko/internal/engine"
	"paperko/services"
)

//go:embed all:frontend/dist
var assets embed.FS

// devRepoRoot is used in `wails3 dev` to locate the Python engine + venv.
const devRepoRoot = "/Volumes/LLM-model/App-develop/PaperKo"

func main() {
	logger := slog.New(slog.NewTextHandler(os.Stderr, &slog.HandlerOptions{Level: slog.LevelInfo}))

	dataDir := appDataDir()
	_ = os.MkdirAll(filepath.Join(dataDir, "jobs"), 0o755)

	backend := services.NewBackend(dataDir, logger)
	backend.AttachLlama(engine.NewLlamaServer(resolveLlamaBin()))

	app := application.New(application.Options{
		Name:        "PaperKo",
		Description: "학술 논문 PDF 한국어 번역",
		Services: []application.Service{
			application.NewService(&services.DocumentService{B: backend}),
			application.NewService(&services.JobService{B: backend}),
			application.NewService(&services.SettingsService{B: backend}),
			application.NewService(&services.GlossaryService{B: backend}),
			application.NewService(&services.UpdateService{B: backend}),
		},
		Assets: application.AssetOptions{
			Handler: application.AssetFileServerFS(assets),
		},
		Mac: application.MacOptions{
			ApplicationShouldTerminateAfterLastWindowClosed: true,
		},
	})

	// Engine supervisor (dev config: run the source package via the project venv).
	sup := engine.NewSupervisor(engineConfig(dataDir, backend), logger)
	backend.AttachEngine(sup)
	go func() {
		if err := sup.Start(context.Background()); err != nil {
			logger.Error("engine failed to start", "err", err)
			return
		}
		// If configured for offline use, bring up the local llama.cpp server and
		// re-point the engine at it once the sidecar is ready.
		backend.BootLocalEngine()
	}()

	app.Window.NewWithOptions(application.WebviewWindowOptions{
		Title:            "PaperKo — 논문 PDF 한국어 번역",
		Width:            1280,
		Height:           860,
		MinWidth:         960,
		MinHeight:        640,
		BackgroundColour: application.NewRGB(248, 249, 251),
		// Use the standard macOS title bar so the window is reliably movable.
		Mac: application.MacWindow{
			TitleBar: application.MacTitleBarDefault,
		},
		URL: "/",
	})

	err := app.Run()
	backend.StopLocalServer()
	sup.Stop()
	if err != nil {
		log.Fatal(err)
	}
}

// resolveLlamaBin finds the bundled llama.cpp `llama-server` binary next to the
// app (resources/llama/…), falling back to the repo tree (dev) or PATH.
func resolveLlamaBin() string {
	if p := os.Getenv("PAPERKO_LLAMA_BIN"); p != "" {
		return p
	}
	binName := "llama-server"
	if runtime.GOOS == "windows" {
		binName += ".exe"
	}
	if selfPath, err := os.Executable(); err == nil {
		dir := filepath.Dir(selfPath)
		for _, root := range []string{dir, filepath.Join(dir, "..", "Resources")} {
			cand := filepath.Join(root, "resources", "llama", binName)
			if fi, statErr := os.Stat(cand); statErr == nil && !fi.IsDir() {
				return cand
			}
		}
	}
	cand := filepath.Join(devRepoRoot, "desktop", "paperko", "resources", "llama", binName)
	if fi, err := os.Stat(cand); err == nil && !fi.IsDir() {
		return cand
	}
	if p, err := exec.LookPath(binName); err == nil {
		return p
	}
	return ""
}

func engineConfig(dataDir string, backend *services.Backend) engine.Config {
	exe, args, env := resolveEngine()
	s := backend.Settings()
	var key *string
	if s.LLMAPIKey != "" {
		key = &s.LLMAPIKey
	}
	return engine.Config{
		Python:  exe,
		Args:    args,
		Env:     env,
		DataDir: dataDir,
		LLM: engine.LLMConfig{
			BaseURL: s.LLMBaseURL, Model: s.LLMModel, APIKey: key,
			MaxConcurrency: s.MaxConcurrency, TimeoutS: 120,
		},
		LogLevel: "info",
	}
}

// resolveEngine decides how to launch the sidecar:
//  1. explicit override via PAPERKO_ENGINE_PYTHON (+ PAPERKO_ENGINE_ROOT);
//  2. a bundled, self-contained engine binary shipped next to the app
//     (resources/engine/translate-engine[.exe]) — the packaged/production path;
//  3. the project venv running the source package — the dev fallback.
func resolveEngine() (exe string, args []string, env []string) {
	// (1) explicit override
	if py := os.Getenv("PAPERKO_ENGINE_PYTHON"); py != "" {
		root := os.Getenv("PAPERKO_ENGINE_ROOT")
		if root == "" {
			root = devRepoRoot
		}
		return py, []string{"-m", "translate_engine"}, []string{"PYTHONPATH=" + filepath.Join(root, "engine-py")}
	}

	if selfPath, err := os.Executable(); err == nil {
		dir := filepath.Dir(selfPath)
		// resource roots to probe (portable layout + macOS .app bundle)
		roots := []string{dir, filepath.Join(dir, "..", "Resources")}

		// (2a) embedded Python runtime shipped next to the app
		//      (python.org "Windows embeddable package"): resources\python\python.exe
		//      running `-m translate_engine`, with the engine package on PYTHONPATH.
		pyName := filepath.Join("python", "python.exe")
		if runtime.GOOS != "windows" {
			pyName = filepath.Join("python", "bin", "python3")
		}
		for _, root := range roots {
			py := filepath.Join(root, "resources", pyName)
			if fi, statErr := os.Stat(py); statErr == nil && !fi.IsDir() {
				enginePath := filepath.Join(root, "resources", "engine")
				// -s ignores the machine's per-user site-packages so the bundled
				// runtime never mixes with a system Python install. (The embeddable
				// ._pth already ignores PYTHONHOME/PYTHONPATH and system paths.)
				return py, []string{"-s", "-m", "translate_engine"}, []string{"PYTHONPATH=" + enginePath}
			}
		}

		// (2b) self-contained engine binary (PyInstaller onedir)
		binName := "translate-engine"
		if runtime.GOOS == "windows" {
			binName += ".exe"
		}
		for _, root := range roots {
			cand := filepath.Join(root, "resources", "engine", binName)
			if fi, statErr := os.Stat(cand); statErr == nil && !fi.IsDir() {
				return cand, nil, nil
			}
		}
	}

	// (3) dev fallback: project venv + source package
	root := os.Getenv("PAPERKO_ENGINE_ROOT")
	if root == "" {
		root = devRepoRoot
	}
	py := filepath.Join(root, ".venv", "bin", "python")
	if runtime.GOOS == "windows" {
		py = filepath.Join(root, ".venv", "Scripts", "python.exe")
	}
	return py, []string{"-m", "translate_engine"}, []string{"PYTHONPATH=" + filepath.Join(root, "engine-py")}
}

func appDataDir() string {
	base, err := os.UserConfigDir()
	if err != nil || base == "" {
		base, _ = os.UserHomeDir()
	}
	return filepath.Join(base, "PaperKo")
}
