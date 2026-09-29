package authd

import (
	"bufio"
	"errors"
	"fmt"
	"os"
	"regexp"
	"strings"
)

var errGitHubNotAllowed = errors.New("GitHub account is not allowed to sign in")
var errGitHubAllowlistUnavailable = errors.New("GitHub login allowlist is unavailable")
var githubUsername = regexp.MustCompile(`^[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,37}[a-zA-Z0-9])?$`)

// Open on each login so an atomic file replacement takes effect without a restart.
// Missing, unreadable or malformed files never grant access.
func checkGitHubAllowlist(path, login string) error {
	f, err := os.Open(path)
	if err != nil {
		return fmt.Errorf("%w: %v", errGitHubAllowlistUnavailable, err)
	}
	defer f.Close()
	allowed := false
	scanner := bufio.NewScanner(f)
	for line := 1; scanner.Scan(); line++ {
		name := strings.TrimSpace(scanner.Text())
		if name == "" || strings.HasPrefix(name, "#") {
			continue
		}
		if !githubUsername.MatchString(name) || strings.Contains(name, "--") {
			return fmt.Errorf("%w: invalid username on line %d", errGitHubAllowlistUnavailable, line)
		}
		if strings.EqualFold(name, login) {
			allowed = true
		}
	}
	if err := scanner.Err(); err != nil {
		return fmt.Errorf("%w: %v", errGitHubAllowlistUnavailable, err)
	}
	if !allowed {
		return errGitHubNotAllowed
	}
	return nil
}
