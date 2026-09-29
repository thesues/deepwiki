package authd

import (
	"crypto/subtle"
	"database/sql"
	"encoding/base64"
	"errors"
	"io"
	"net/http"
	"net/url"
	"strings"
	"time"

	"github.com/gin-gonic/gin"
	"go.uber.org/zap"
)

const shortTokenLifetime = 60 * time.Second
const oauthLifetime = 5 * time.Minute

type Server struct {
	cfg      Config
	store    Store
	provider IdentityProvider
	tokens   *TokenManager
	log      *zap.Logger
	now      func() time.Time
}

func NewServer(cfg Config, store Store, provider IdentityProvider, tokens *TokenManager, log *zap.Logger) *Server {
	if cfg.Provider == "" {
		cfg.Provider = "feishu"
	}
	if log == nil {
		log = zap.NewNop()
	}
	return &Server{cfg: cfg, store: store, provider: provider, tokens: tokens, log: log, now: time.Now}
}

func (s *Server) Router() *gin.Engine {
	gin.SetMode(gin.ReleaseMode)
	r := gin.New()
	_ = r.SetTrustedProxies(nil)
	r.Use(gin.CustomRecoveryWithWriter(io.Discard, func(c *gin.Context, _ any) {
		s.log.Error("request panicked")
		c.AbortWithStatus(http.StatusInternalServerError)
	}), s.accessLog())
	r.GET("/healthz", func(c *gin.Context) { c.String(http.StatusOK, "ok") })
	r.GET("/.well-known/jwks.json", func(c *gin.Context) {
		c.Header("Cache-Control", "public, max-age=300")
		c.JSON(http.StatusOK, s.tokens.JWKS())
	})
	r.GET("/auth/login", s.login)
	r.GET("/sso/authorize", s.authorize)
	r.GET("/oauth/"+s.cfg.Provider+"/callback", s.oauthCallback)
	r.GET("/auth/callback", s.appCallback)
	return r
}

func (s *Server) accessLog() gin.HandlerFunc {
	return func(c *gin.Context) {
		start := time.Now()
		c.Header("Cache-Control", "no-store")
		c.Header("Referrer-Policy", "no-referrer")
		c.Next()
		s.log.Info("http request",
			zap.String("method", c.Request.Method),
			zap.String("path", c.Request.URL.Path),
			zap.Int("status", c.Writer.Status()),
			zap.Duration("duration", time.Since(start)),
		)
	}
}

func (s *Server) login(c *gin.Context) {
	app, ok := s.cfg.AppsByHost[requestHost(c.Request.Host)]
	if !ok {
		c.AbortWithStatusJSON(http.StatusBadRequest, gin.H{"error": "unknown application host"})
		return
	}
	u := s.cfg.PublicURL.ResolveReference(&url.URL{Path: "/sso/authorize"})
	q := u.Query()
	nonce := randomString(32)
	setFlowCookie(c, "__Host-auth_request", nonce, 600)
	q.Set("browser", base64.RawURLEncoding.EncodeToString(tokenHash(nonce)))
	q.Set("app", app.ID)
	q.Set("return", cleanReturnPath(c.Query("return")))
	u.RawQuery = q.Encode()
	c.Redirect(http.StatusFound, u.String())
}

func (s *Server) authorize(c *gin.Context) {
	if !s.isPublicHost(c) {
		c.AbortWithStatusJSON(http.StatusBadRequest, gin.H{"error": "wrong host"})
		return
	}
	app, ok := s.cfg.Apps[c.Query("app")]
	if !ok {
		c.AbortWithStatusJSON(http.StatusBadRequest, gin.H{"error": "unknown application"})
		return
	}
	returnPath := cleanReturnPath(c.Query("return"))
	browserHash := c.Query("browser")
	if decoded, err := base64.RawURLEncoding.DecodeString(browserHash); err != nil || len(decoded) != 32 {
		c.AbortWithStatusJSON(http.StatusBadRequest, gin.H{"error": "start login from the application"})
		return
	}
	// GitHub must fetch the current username and recheck the file on every
	// login; an older SSO cookie must not bypass a changed allowlist.
	if raw, err := c.Cookie(SSOCookieName); err == nil && s.cfg.Provider != "github" {
		if claims, err := s.tokens.VerifySSO(raw); err == nil {
			s.redirectWithCode(c, app, claims.Subject, returnPath, browserHash)
			return
		}
	}
	flow := OAuthState{AppID: app.ID, ReturnPath: returnPath, BrowserHash: browserHash, Provider: s.cfg.Provider}
	if s.cfg.Provider == "github" {
		flow.Verifier = randomString(32)
	}
	state, err := s.store.CreateOAuthState(c.Request.Context(), flow, s.now().Add(oauthLifetime))
	if err != nil {
		s.internalError(c, "create oauth state", err)
		return
	}
	setFlowCookie(c, "__Host-auth_flow", state, int(oauthLifetime.Seconds()))
	c.Redirect(http.StatusFound, s.provider.AuthorizationURL(OAuthRequest{State: state, Verifier: flow.Verifier}))
}

func (s *Server) oauthCallback(c *gin.Context) {
	if !s.isPublicHost(c) {
		c.AbortWithStatusJSON(http.StatusBadRequest, gin.H{"error": "wrong host"})
		return
	}
	flow, _ := c.Cookie("__Host-auth_flow")
	if flow == "" || subtle.ConstantTimeCompare([]byte(flow), []byte(c.Query("state"))) != 1 {
		c.AbortWithStatusJSON(http.StatusBadRequest, gin.H{"error": "invalid login flow"})
		return
	}
	setFlowCookie(c, "__Host-auth_flow", "", -1)
	state, err := s.store.ConsumeOAuthState(c.Request.Context(), c.Query("state"), s.now())
	if err != nil || state.Provider != s.cfg.Provider {
		c.AbortWithStatusJSON(http.StatusBadRequest, gin.H{"error": "invalid or expired oauth state"})
		return
	}
	app, ok := s.cfg.Apps[state.AppID]
	if !ok {
		c.AbortWithStatusJSON(http.StatusBadRequest, gin.H{"error": "unknown application"})
		return
	}
	if c.Query("error") != "" || c.Query("code") == "" {
		c.AbortWithStatusJSON(http.StatusBadRequest, gin.H{"error": "login was not authorized; please start again"})
		return
	}
	identity, err := s.provider.Authenticate(c.Request.Context(), c.Query("code"), state.Verifier)
	if err != nil {
		s.log.Warn("authentication failed", zap.String("provider", s.cfg.Provider), zap.Error(err))
		if errors.Is(err, errGitHubNotAllowed) {
			c.AbortWithStatusJSON(http.StatusForbidden, gin.H{"error": errGitHubNotAllowed.Error()})
			return
		}
		if errors.Is(err, errGitHubAllowlistUnavailable) {
			c.AbortWithStatusJSON(http.StatusServiceUnavailable, gin.H{"error": errGitHubAllowlistUnavailable.Error()})
			return
		}
		c.AbortWithStatusJSON(http.StatusBadGateway, gin.H{"error": "authentication failed"})
		return
	}
	if identity.ID == "" {
		c.AbortWithStatusJSON(http.StatusBadGateway, gin.H{"error": "identity missing"})
		return
	}
	tenant := s.cfg.TenantKey
	if s.cfg.Provider != "feishu" {
		tenant += "\x00" + s.cfg.Provider
	}
	subject := deterministicSubject(tenant, identity.ID)
	sso, err := s.tokens.Mint(subject, SSOAudience)
	if err != nil {
		s.internalError(c, "mint sso token", err)
		return
	}
	setHostCookie(c, SSOCookieName, sso)
	s.redirectWithCode(c, app, subject, state.ReturnPath, state.BrowserHash)
}

func (s *Server) appCallback(c *gin.Context) {
	host := requestHost(c.Request.Host)
	app, ok := s.cfg.AppsByHost[host]
	if !ok {
		c.AbortWithStatusJSON(http.StatusBadRequest, gin.H{"error": "unknown application host"})
		return
	}
	nonce, _ := c.Cookie("__Host-auth_request")
	if nonce == "" {
		c.AbortWithStatusJSON(http.StatusBadRequest, gin.H{"error": "missing login flow"})
		return
	}
	browserHash := base64.RawURLEncoding.EncodeToString(tokenHash(nonce))
	code, err := s.store.ConsumeAuthCode(c.Request.Context(), c.Query("code"), app.ID, host, browserHash, s.now())
	if err != nil {
		c.AbortWithStatusJSON(http.StatusBadRequest, gin.H{"error": "invalid or expired authorization code"})
		return
	}
	if code.AppID != app.ID || code.TargetHost != host {
		c.AbortWithStatusJSON(http.StatusBadRequest, gin.H{"error": "authorization code is for another application"})
		return
	}
	access, err := s.tokens.Mint(code.Subject, app.ID)
	if err != nil {
		s.internalError(c, "mint access token", err)
		return
	}
	setFlowCookie(c, "__Host-auth_request", "", -1)
	setHostCookie(c, AccessCookieName, access)
	c.Redirect(http.StatusFound, cleanReturnPath(code.ReturnPath))
}

func (s *Server) redirectWithCode(c *gin.Context, app App, subject, returnPath, browserHash string) {
	raw, err := s.store.CreateAuthCode(c.Request.Context(), AuthCode{
		Subject: subject, AppID: app.ID, TargetHost: app.Host, ReturnPath: cleanReturnPath(returnPath), BrowserHash: browserHash,
	}, s.now().Add(shortTokenLifetime))
	if err != nil {
		s.internalError(c, "create authorization code", err)
		return
	}
	u, _ := url.Parse(app.Origin + "/auth/callback")
	q := u.Query()
	q.Set("code", raw)
	u.RawQuery = q.Encode()
	c.Redirect(http.StatusFound, u.String())
}

func (s *Server) isPublicHost(c *gin.Context) bool {
	return requestHost(c.Request.Host) == s.cfg.PublicHost
}

func (s *Server) internalError(c *gin.Context, operation string, err error) {
	if !errors.Is(err, sql.ErrNoRows) {
		s.log.Error(operation, zap.Error(err))
	}
	c.AbortWithStatusJSON(http.StatusServiceUnavailable, gin.H{"error": "authentication service unavailable"})
}

func setHostCookie(c *gin.Context, name, value string) {
	c.SetSameSite(http.SameSiteLaxMode)
	c.SetCookie(name, value, int(tokenLifetime.Seconds()), "/", "", true, true)
}

func requestHost(raw string) string {
	return strings.ToLower(strings.TrimSpace(raw))
}

func setFlowCookie(c *gin.Context, name, value string, maxAge int) {
	c.SetSameSite(http.SameSiteLaxMode)
	c.SetCookie(name, value, maxAge, "/", "", true, true)
}
