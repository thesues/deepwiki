package authd

import (
	"encoding/json"
	"errors"
	"fmt"
	"net/url"
	"os"
	"strings"
)

const (
	Issuer           = "buda-authd"
	SSOAudience      = "authd-sso"
	SSOCookieName    = "__Host-auth_sso"
	AccessCookieName = "__Host-auth_access"
)

type App struct {
	ID     string
	URL    *url.URL
	Host   string
	Origin string
}

type Config struct {
	ListenAddr     string
	PublicURL      *url.URL
	PublicHost     string
	Apps           map[string]App
	AppsByHost     map[string]App
	DBPath         string
	TenantKey      string
	PrivateKeyFile string
	KeyID          string
	Provider       string
	GitHubClientID string
	GitHubSecret   string
	FeishuAppID    string
	FeishuSecret   string
	FeishuAuthURL  string
	FeishuTokenURL string
	FeishuUserURL  string
}

func LoadConfig() (Config, error) {
	publicURL, err := url.Parse(strings.TrimSpace(os.Getenv("AUTH_PUBLIC_URL")))
	if err != nil || publicURL.Scheme != "https" || publicURL.Hostname() == "" || publicURL.User != nil || publicURL.Path != "" || publicURL.RawQuery != "" || publicURL.Fragment != "" {
		return Config{}, errors.New("AUTH_PUBLIC_URL must be an https origin")
	}

	var rawApps map[string]string
	if err := json.Unmarshal([]byte(os.Getenv("AUTH_APPS_JSON")), &rawApps); err != nil {
		return Config{}, fmt.Errorf("AUTH_APPS_JSON: %w", err)
	}
	apps := make(map[string]App, len(rawApps))
	appsByHost := make(map[string]App, len(rawApps))
	for id, raw := range rawApps {
		id = strings.TrimSpace(id)
		u, err := url.Parse(strings.TrimSpace(raw))
		if err != nil || id == "" || u.Scheme != "https" || u.Hostname() == "" || u.Path != "" || u.User != nil || u.RawQuery != "" || u.Fragment != "" || id == SSOAudience {
			return Config{}, fmt.Errorf("AUTH_APPS_JSON[%q] must be an https origin without a path", id)
		}
		host := strings.ToLower(u.Host)
		if host == strings.ToLower(publicURL.Host) {
			return Config{}, errors.New("application and SSO hosts must differ")
		}
		app := App{ID: id, URL: u, Host: host, Origin: strings.TrimRight(u.String(), "/")}
		if _, exists := appsByHost[host]; exists {
			return Config{}, fmt.Errorf("duplicate application host %q", host)
		}
		apps[id] = app
		appsByHost[host] = app
	}
	if len(apps) == 0 {
		return Config{}, errors.New("AUTH_APPS_JSON must contain at least one application")
	}

	cfg := Config{
		ListenAddr:     envOr("LISTEN_ADDR", ":8080"),
		PublicURL:      publicURL,
		PublicHost:     strings.ToLower(publicURL.Host),
		Apps:           apps,
		AppsByHost:     appsByHost,
		DBPath:         envOr("AUTH_DB_PATH", "/var/lib/authd/authd.db"),
		TenantKey:      envOr("AUTH_TENANT_KEY", "default"),
		PrivateKeyFile: strings.TrimSpace(os.Getenv("JWT_PRIVATE_KEY_FILE")),
		KeyID:          strings.TrimSpace(os.Getenv("JWT_KID")),
		Provider:       envOr("AUTH_PROVIDER", "feishu"),
		GitHubClientID: strings.TrimSpace(os.Getenv("GITHUB_CLIENT_ID")),
		GitHubSecret:   strings.TrimSpace(os.Getenv("GITHUB_CLIENT_SECRET")),
		FeishuAppID:    strings.TrimSpace(os.Getenv("FEISHU_APP_ID")),
		FeishuSecret:   strings.TrimSpace(os.Getenv("FEISHU_APP_SECRET")),
		FeishuAuthURL:  envOr("FEISHU_AUTH_URL", "https://accounts.feishu.cn/open-apis/authen/v1/authorize"),
		FeishuTokenURL: envOr("FEISHU_TOKEN_URL", "https://open.feishu.cn/open-apis/authen/v2/oauth/token"),
		FeishuUserURL:  envOr("FEISHU_USER_URL", "https://open.feishu.cn/open-apis/authen/v1/user_info"),
	}
	if cfg.PrivateKeyFile == "" || cfg.KeyID == "" {
		return Config{}, errors.New("JWT_PRIVATE_KEY_FILE and JWT_KID are required")
	}
	if providerFactories[cfg.Provider] == nil {
		return Config{}, fmt.Errorf("unknown AUTH_PROVIDER %q", cfg.Provider)
	}
	if cfg.Provider == "github" && (cfg.GitHubClientID == "" || cfg.GitHubSecret == "") {
		return Config{}, errors.New("GITHUB_CLIENT_ID and GITHUB_CLIENT_SECRET are required")
	}
	if cfg.Provider == "feishu" && (cfg.FeishuAppID == "" || cfg.FeishuSecret == "") {
		return Config{}, errors.New("FEISHU_APP_ID and FEISHU_APP_SECRET are required")
	}
	return cfg, nil
}

func envOr(name, fallback string) string {
	if value := strings.TrimSpace(os.Getenv(name)); value != "" {
		return value
	}
	return fallback
}

func cleanReturnPath(raw string) string {
	if raw == "" {
		return "/"
	}
	u, err := url.Parse(raw)
	if err != nil || !strings.HasPrefix(raw, "/") || strings.HasPrefix(raw, "//") || u.IsAbs() || u.Host != "" {
		return "/"
	}
	if len(raw) > 2048 || strings.ContainsAny(u.Path, "\\\r\n\t") || strings.HasPrefix(u.Path, "//") || strings.HasPrefix(u.Path, "/auth/") {
		return "/"
	}
	return raw
}
