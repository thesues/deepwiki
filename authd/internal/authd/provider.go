package authd

import "context"

type OAuthRequest struct{ State, Verifier string }
type Identity struct{ ID string }
type IdentityProvider interface {
	AuthorizationURL(OAuthRequest) string
	Authenticate(context.Context, string, string) (Identity, error)
}

// AUTH_PROVIDER selects one implementation; inactive providers need no credentials.
var providerFactories = map[string]func(Config) IdentityProvider{
	"feishu": func(cfg Config) IdentityProvider { return NewFeishuProvider(cfg) },
	"github": func(cfg Config) IdentityProvider { return NewGitHubProvider(cfg) },
}

func NewIdentityProvider(cfg Config) IdentityProvider { return providerFactories[cfg.Provider](cfg) }
