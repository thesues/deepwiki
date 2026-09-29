package authd

import "context"

type OAuthRequest struct{ State string }
type Identity struct{ ID string }
type IdentityProvider interface {
	AuthorizationURL(OAuthRequest) string
	Authenticate(context.Context, string) (Identity, error)
}

// AUTH_PROVIDER selects one implementation without provider-specific booleans.
var providerFactories = map[string]func(Config) IdentityProvider{
	"feishu": func(cfg Config) IdentityProvider { return NewFeishuProvider(cfg) },
}

func NewIdentityProvider(cfg Config) IdentityProvider { return providerFactories[cfg.Provider](cfg) }
