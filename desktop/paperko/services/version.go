package services

// Version is the single source of truth for the app version (SemVer). Keep it in
// sync with engine-py/translate_engine/__init__.py __version__ and
// build/windows/info.json on each release.
const Version = "1.8.7"

// AppVersion returns the running app version (shown in the UI top bar).
func (s *SettingsService) AppVersion() string { return Version }
