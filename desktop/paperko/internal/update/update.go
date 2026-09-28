// Package update 는 GitHub 릴리스에서 새 판을 찾아 받아 설치한다.
//
// 흐름: 공개 릴리스 저장소의 **최신 릴리스**에 올린 manifest.json 과 그 서명(manifest.json.sig)을
// 받는다 → 서명을 앱에 심은 공개키로 확인한다 → 판 번호를 견준다 → 이 OS 의 설치본을 받아
// SHA-256 을 manifest 와 견준다 → 설치한다(install_*.go).
//
// 서명은 릴리스를 만드는 컴퓨터에만 있는 Ed25519 개인키로 한다. GitHub 계정이 뚫려도 개인키 없이
// 만든 manifest 는 앱이 받지 않고, manifest 에 적힌 해시와 다른 설치본도 받지 않는다.
package update

import (
	"context"
	"crypto/ed25519"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"
)

// Repo 는 설치본을 올리는 **공개** 배포 저장소다. 소스(mikwon3/PaperKo)와 분리해,
// 설치본과 manifest 는 이 저장소의 릴리스에 올린다.
const Repo = "mikwon3/PaperKo-releases"

// ManifestName 과 SignatureName 은 릴리스마다 올리는 두 파일의 이름이다.
const (
	ManifestName  = "manifest.json"
	SignatureName = "manifest.json.sig"
)

// BaseURL 은 릴리스 주소의 앞부분이다. 시험할 때만 PAPERKO_UPDATE_BASE 로 바꾼다 —
// 주소를 바꿔도 서명은 늘 심은 공개키로 확인하므로 가짜 판을 받지는 않는다.
func BaseURL() string {
	if b := os.Getenv("PAPERKO_UPDATE_BASE"); b != "" {
		return strings.TrimRight(b, "/")
	}
	return "https://github.com/" + Repo + "/releases"
}

// PageURL 은 사람이 보는 릴리스 페이지다.
func PageURL(version string) string {
	if version == "" {
		return "https://github.com/" + Repo + "/releases/latest"
	}
	return "https://github.com/" + Repo + "/releases/tag/v" + strings.TrimPrefix(version, "v")
}

// Asset 은 설치본 하나다.
type Asset struct {
	OS     string `json:"os"`   // darwin · windows
	Arch   string `json:"arch"` // arm64 · amd64
	Name   string `json:"name"`
	SHA256 string `json:"sha256"`
	Size   int64  `json:"size"`
}

// Manifest 는 릴리스 하나의 목록이다.
type Manifest struct {
	Version   string  `json:"version"`
	Notes     string  `json:"notes"`
	Published string  `json:"published"`
	Assets    []Asset `json:"assets"`
}

// AssetFor 는 이 OS·CPU 의 설치본이다.
func (m *Manifest) AssetFor(goos, goarch string) (Asset, bool) {
	for _, a := range m.Assets {
		if a.OS == goos && a.Arch == goarch {
			return a, true
		}
	}
	return Asset{}, false
}

// AssetURL 은 설치본을 받을 주소다.
func AssetURL(version string, a Asset) string {
	return BaseURL() + "/download/v" + strings.TrimPrefix(version, "v") + "/" + a.Name
}

// ErrBadSignature 는 서명이 맞지 않는다는 뜻이다. 그런 manifest 는 쓰지 않는다.
var ErrBadSignature = errors.New("업데이트 정보의 서명이 맞지 않습니다 — 받지 않습니다")

// VerifyManifest 는 서명을 확인한 뒤 manifest 를 읽는다. sig 는 base64 로 적은 Ed25519 서명이다.
func VerifyManifest(data, sig []byte, pub ed25519.PublicKey) (*Manifest, error) {
	raw, err := base64.StdEncoding.DecodeString(strings.TrimSpace(string(sig)))
	if err != nil || len(pub) != ed25519.PublicKeySize || !ed25519.Verify(pub, data, raw) {
		return nil, ErrBadSignature
	}
	var m Manifest
	if err := json.Unmarshal(data, &m); err != nil {
		return nil, fmt.Errorf("업데이트 정보를 읽지 못했습니다: %w", err)
	}
	if _, err := parseVersion(m.Version); err != nil {
		return nil, fmt.Errorf("업데이트 정보의 판 번호가 이상합니다: %q", m.Version)
	}
	return &m, nil
}

// Sign 은 manifest 에 서명한다(릴리스 도구가 쓴다). base64 글을 돌려준다.
func Sign(data []byte, priv ed25519.PrivateKey) string {
	return base64.StdEncoding.EncodeToString(ed25519.Sign(priv, data))
}

// ── 판 번호 ────────────────────────────────────────────────────────

type version struct {
	nums [3]int
	pre  string // "-beta.1" 처럼 붙은 것. 있으면 같은 번호의 정식 판보다 낮다.
}

func parseVersion(s string) (version, error) {
	s = strings.TrimPrefix(strings.TrimSpace(s), "v")
	var v version
	if i := strings.IndexAny(s, "-+"); i >= 0 {
		v.pre, s = s[i:], s[:i]
	}
	parts := strings.Split(s, ".")
	if len(parts) == 0 || len(parts) > 3 {
		return v, fmt.Errorf("판 번호 %q", s)
	}
	for i, p := range parts {
		n, err := strconv.Atoi(p)
		if err != nil || n < 0 {
			return v, fmt.Errorf("판 번호 %q", s)
		}
		v.nums[i] = n
	}
	return v, nil
}

// Compare 는 a 가 b 보다 새로우면 양수, 같으면 0, 낮으면 음수다. 읽을 수 없는 번호는 가장 낮게 본다.
func Compare(a, b string) int {
	va, ea := parseVersion(a)
	vb, eb := parseVersion(b)
	switch {
	case ea != nil && eb != nil:
		return 0
	case ea != nil:
		return -1
	case eb != nil:
		return 1
	}
	for i := 0; i < 3; i++ {
		if va.nums[i] != vb.nums[i] {
			return va.nums[i] - vb.nums[i]
		}
	}
	switch {
	case va.pre == vb.pre:
		return 0
	case va.pre == "":
		return 1
	case vb.pre == "":
		return -1
	}
	return strings.Compare(va.pre, vb.pre)
}

// ── 언제 확인하는가 ────────────────────────────────────────────────

// CheckInterval 은 자동 확인 사이의 최소 간격이다. 앱을 자주 켜도 하루 한 번만 묻는다.
const CheckInterval = 24 * time.Hour

// DueForCheck 는 지금 자동으로 확인할 때인가. 사람이 설정에서 부르면(manual) 언제나 확인한다.
func DueForCheck(manual, autoOff bool, last string, now time.Time) bool {
	if manual {
		return true
	}
	if autoOff {
		return false
	}
	t, err := time.Parse(time.RFC3339, last)
	return err != nil || now.Sub(t) >= CheckInterval
}

// ShouldOffer 는 받은 판을 사용자에게 권할 것인가. 자동 확인에서는 "이 판 건너뛰기" 를 지킨다.
func ShouldOffer(current, latest, skipped string, manual bool) bool {
	if Compare(latest, current) <= 0 {
		return false
	}
	return manual || Compare(latest, skipped) != 0 || skipped == ""
}

// ── 받기 ───────────────────────────────────────────────────────────

// Client 는 업데이트에 쓰는 HTTP 클라이언트다. GitHub 은 받을 파일을 다른 주소로 넘기므로
// 넘김은 따라간다(기본 동작).
var Client = &http.Client{Timeout: 0}

func get(ctx context.Context, url string, limit int64) ([]byte, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return nil, err
	}
	res, err := Client.Do(req)
	if err != nil {
		return nil, err
	}
	defer res.Body.Close()
	if res.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("%s: %s", url, res.Status)
	}
	return io.ReadAll(io.LimitReader(res.Body, limit))
}

// Fetch 는 최신 릴리스의 manifest 를 받아 서명을 확인한다.
func Fetch(ctx context.Context, pub ed25519.PublicKey) (*Manifest, error) {
	data, err := get(ctx, BaseURL()+"/latest/download/"+ManifestName, 1<<20)
	if err != nil {
		return nil, fmt.Errorf("업데이트 정보를 받지 못했습니다: %w", err)
	}
	sig, err := get(ctx, BaseURL()+"/latest/download/"+SignatureName, 4<<10)
	if err != nil {
		return nil, fmt.Errorf("업데이트 서명을 받지 못했습니다: %w", err)
	}
	return VerifyManifest(data, sig, pub)
}

// ErrHashMismatch 는 받은 설치본이 manifest 와 다르다는 뜻이다. 그 파일은 지운다.
var ErrHashMismatch = errors.New("받은 설치본이 릴리스 정보와 다릅니다 — 설치하지 않습니다")

// Download 는 설치본을 dir 에 받고 크기와 SHA-256 을 manifest 와 견준다. progress 는 nil 이어도 된다.
func Download(ctx context.Context, url string, a Asset, dir string, progress func(done, total int64)) (string, error) {
	if a.Name == "" || strings.ContainsAny(a.Name, `/\`) || strings.HasPrefix(a.Name, ".") {
		return "", fmt.Errorf("설치본 이름이 이상합니다: %q", a.Name)
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return "", err
	}
	res, err := Client.Do(req)
	if err != nil {
		return "", fmt.Errorf("설치본을 받지 못했습니다: %w", err)
	}
	defer res.Body.Close()
	if res.StatusCode != http.StatusOK {
		return "", fmt.Errorf("설치본을 받지 못했습니다: %s", res.Status)
	}
	dst := filepath.Join(dir, a.Name)
	f, err := os.Create(dst)
	if err != nil {
		return "", err
	}
	h := sha256.New()
	var done int64
	buf := make([]byte, 256<<10)
	for {
		n, rerr := res.Body.Read(buf)
		if n > 0 {
			if _, werr := f.Write(buf[:n]); werr != nil {
				f.Close()
				os.Remove(dst)
				return "", werr
			}
			h.Write(buf[:n])
			done += int64(n)
			if progress != nil {
				progress(done, a.Size)
			}
			if a.Size > 0 && done > a.Size {
				break // 적힌 크기보다 크면 더 받을 까닭이 없다
			}
		}
		if rerr == io.EOF {
			break
		}
		if rerr != nil {
			f.Close()
			os.Remove(dst)
			return "", fmt.Errorf("설치본을 받다가 끊겼습니다: %w", rerr)
		}
	}
	if err := f.Close(); err != nil {
		os.Remove(dst)
		return "", err
	}
	if (a.Size > 0 && done != a.Size) || !strings.EqualFold(hex.EncodeToString(h.Sum(nil)), a.SHA256) {
		os.Remove(dst)
		return "", ErrHashMismatch
	}
	return dst, nil
}

// ErrUnsupported 는 이 OS 에서는 스스로 설치하지 못한다는 뜻이다. 릴리스 페이지를 연다.
var ErrUnsupported = errors.New("이 운영체제에서는 자동 설치를 하지 않습니다")

// Install 은 받은 설치본으로 새 판을 설치한다. 성공하면 **앱을 곧 끝내야 한다** — 설치가 실행 중인
// 앱 파일을 바꾸기 때문이다(install_*.go).
func Install(path string) error { return install(path) }
