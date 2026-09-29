package authd

import (
	"errors"
	"os"
	"path/filepath"
	"testing"
)

func TestGitHubAllowlistReloadAndFailClosed(t *testing.T) {
	path := filepath.Join(t.TempDir(), "allowlist.txt")
	if err := checkGitHubAllowlist(path, "demo-user"); !errors.Is(err, errGitHubAllowlistUnavailable) {
		t.Fatalf("missing file: %v", err)
	}
	for _, tc := range []struct {
		file, login string
		want        error
	}{
		{"# team\r\n  Demo-User \r\n\r\n", "demo-user", nil},
		{"demo-user\n", "someone-else", errGitHubNotAllowed},
		{"other-user\n", "demo-user", errGitHubNotAllowed},
		{"# empty\n", "demo-user", errGitHubNotAllowed},
		{"demo-user\ninvalid name\n", "demo-user", errGitHubAllowlistUnavailable},
		{"*\n", "demo-user", errGitHubAllowlistUnavailable},
		{"https://github.com/demo-user\n", "demo-user", errGitHubAllowlistUnavailable},
		{"demo-user\n", "", errGitHubNotAllowed},
	} {
		if err := os.WriteFile(path+".new", []byte(tc.file), 0600); err != nil {
			t.Fatal(err)
		}
		if err := os.Rename(path+".new", path); err != nil {
			t.Fatal(err)
		}
		if err := checkGitHubAllowlist(path, tc.login); !errors.Is(err, tc.want) {
			t.Fatalf("file %q login %q: got %v want %v", tc.file, tc.login, err, tc.want)
		}
	}
}
