package authd

import (
	"crypto/rand"
	"crypto/rsa"
	"crypto/sha256"
	"crypto/x509"
	"encoding/base64"
	"encoding/binary"
	"encoding/json"
	"encoding/pem"
	"errors"
	"fmt"
	"math/big"
	"os"
	"time"

	"github.com/golang-jwt/jwt/v5"
)

const tokenLifetime = 8 * time.Hour

type Claims struct {
	Tenant string `json:"tenant"`
	Ver    int    `json:"ver"`
	jwt.RegisteredClaims
}

type JWK struct {
	Kty string `json:"kty"`
	Use string `json:"use"`
	Alg string `json:"alg"`
	Kid string `json:"kid"`
	N   string `json:"n"`
	E   string `json:"e"`
}

type TokenManager struct {
	private  *rsa.PrivateKey
	kid      string
	tenant   string
	now      func() time.Time
	previous []JWK
	public   map[string]*rsa.PublicKey
}

func LoadTokenManager(path, kid, tenant string) (*TokenManager, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("read JWT private key: %w", err)
	}
	block, _ := pem.Decode(raw)
	if block == nil {
		return nil, errors.New("JWT private key is not PEM")
	}
	var key *rsa.PrivateKey
	if parsed, err := x509.ParsePKCS8PrivateKey(block.Bytes); err == nil {
		key, _ = parsed.(*rsa.PrivateKey)
	}
	if key == nil {
		key, err = x509.ParsePKCS1PrivateKey(block.Bytes)
		if err != nil {
			return nil, fmt.Errorf("parse JWT private key: %w", err)
		}
	}
	if key.N.BitLen() < 2048 {
		return nil, errors.New("JWT RSA key must be at least 2048 bits")
	}
	return NewTokenManager(key, kid, tenant), nil
}

func NewTokenManager(key *rsa.PrivateKey, kid, tenant string) *TokenManager {
	return &TokenManager{private: key, kid: kid, tenant: tenant, now: time.Now, public: map[string]*rsa.PublicKey{kid: &key.PublicKey}}
}

func (m *TokenManager) Mint(subject, audience string) (string, error) {
	now := m.now().UTC()
	claims := Claims{
		Tenant: m.tenant,
		Ver:    1,
		RegisteredClaims: jwt.RegisteredClaims{
			Issuer:    Issuer,
			Subject:   subject,
			Audience:  jwt.ClaimStrings{audience},
			ExpiresAt: jwt.NewNumericDate(now.Add(tokenLifetime)),
			NotBefore: jwt.NewNumericDate(now.Add(-5 * time.Second)),
			IssuedAt:  jwt.NewNumericDate(now),
			ID:        randomString(16),
		},
	}
	token := jwt.NewWithClaims(jwt.SigningMethodRS256, claims)
	token.Header["kid"] = m.kid
	return token.SignedString(m.private)
}

func (m *TokenManager) VerifySSO(raw string) (*Claims, error) {
	claims := &Claims{}
	token, err := jwt.ParseWithClaims(
		raw,
		claims,
		func(token *jwt.Token) (any, error) {
			kid, _ := token.Header["kid"].(string)
			key := m.public[kid]
			if key == nil {
				return nil, errors.New("unknown kid")
			}
			return key, nil
		},
		jwt.WithValidMethods([]string{jwt.SigningMethodRS256.Alg()}),
		jwt.WithIssuer(Issuer),
		jwt.WithAudience(SSOAudience),
		jwt.WithExpirationRequired(),
		jwt.WithIssuedAt(),
		jwt.WithLeeway(30*time.Second),
	)
	if err != nil || !token.Valid || claims.Subject == "" || claims.Tenant != m.tenant || claims.Ver != 1 || claims.IssuedAt == nil || claims.NotBefore == nil || claims.ID == "" {
		return nil, errors.New("invalid SSO token")
	}
	return claims, nil
}

func (m *TokenManager) JWKS() map[string]any {
	e := make([]byte, 4)
	binary.BigEndian.PutUint32(e, uint32(m.private.PublicKey.E))
	for len(e) > 1 && e[0] == 0 {
		e = e[1:]
	}
	return map[string]any{"keys": append([]JWK{{
		Kty: "RSA", Use: "sig", Alg: "RS256", Kid: m.kid,
		N: base64.RawURLEncoding.EncodeToString(m.private.PublicKey.N.Bytes()),
		E: base64.RawURLEncoding.EncodeToString(e),
	}}, m.previous...)}
}

func deterministicSubject(tenant, providerID string) string {
	sum := sha256.Sum256([]byte("authd:v1\x00" + tenant + "\x00" + providerID))
	return "u_" + base64.RawURLEncoding.EncodeToString(sum[:])
}

func randomString(bytes int) string {
	raw := make([]byte, bytes)
	if _, err := rand.Read(raw); err != nil {
		panic("crypto/rand failed: " + err.Error())
	}
	return base64.RawURLEncoding.EncodeToString(raw)
}

func tokenHash(raw string) []byte {
	sum := sha256.Sum256([]byte(raw))
	return sum[:]
}

// LoadPreviousJWKS retains verification keys during the eight-hour rotation window.
// The file contains public keys only and is never used to sign tokens.
func (m *TokenManager) LoadPreviousJWKS(path string) error {
	if path == "" {
		return nil
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		return err
	}
	var set struct {
		Keys []JWK `json:"keys"`
	}
	if err := json.Unmarshal(raw, &set); err != nil {
		return err
	}
	for _, jwk := range set.Keys {
		if jwk.Kty != "RSA" || jwk.Alg != "RS256" || jwk.Use != "sig" || jwk.Kid == "" || m.public[jwk.Kid] != nil {
			return errors.New("invalid or duplicate previous JWK")
		}
		n, err := base64.RawURLEncoding.DecodeString(jwk.N)
		if err != nil {
			return err
		}
		e, err := base64.RawURLEncoding.DecodeString(jwk.E)
		if err != nil || len(e) > 4 {
			return errors.New("invalid RSA exponent")
		}
		exp := new(big.Int).SetBytes(e).Int64()
		modulus := new(big.Int).SetBytes(n)
		if exp < 3 || exp%2 == 0 || modulus.BitLen() < 2048 {
			return errors.New("invalid RSA public key")
		}
		m.public[jwk.Kid] = &rsa.PublicKey{N: modulus, E: int(exp)}
		m.previous = append(m.previous, jwk)
	}
	return nil
}
