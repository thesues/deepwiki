"""Reusable authd JWT verifier; depends only on stdlib and PyJWT[crypto].

Construct once per process with the service's audience and a trusted JWKS URL.
Pass the raw Cookie header to authenticate(). The returned Principal is identity
only: callers decide whether they need resource ownership or history isolation.
No HTTP framework, database, session store or agent dependency belongs here.
"""

from __future__ import annotations

from dataclasses import dataclass
from http.cookies import SimpleCookie


COOKIE_NAME = "__Host-auth_access"
ISSUER = "buda-authd"


@dataclass(frozen=True)
class Principal:
    user_id: str
    tenant: str


class AuthenticationError(Exception):
    """The caller has no valid application JWT."""


class AuthenticationUnavailable(Exception):
    """No signing key was available to decide whether the JWT is valid."""


class JWTAuthenticator:
    def __init__(
        self, jwks_url: str, *, audience: str,
        issuer: str = ISSUER, tenant: str = "default",
    ) -> None:
        if not audience or not jwks_url:
            raise ValueError("jwks_url and audience are required")
        try:
            import jwt
        except ImportError as exc:
            raise RuntimeError("AUTH_JWKS_URL requires PyJWT[crypto]") from exc
        self._jwt = jwt
        self._audience = audience
        self._issuer = issuer
        self._tenant = tenant
        self._jwks = jwt.PyJWKClient(
            jwks_url,
            cache_keys=False,
            cache_jwk_set=True,
            lifespan=300,
            timeout=5,
        )

    def authenticate(self, cookie_header: str | None) -> Principal:
        cookies = SimpleCookie()
        try:
            cookies.load(cookie_header or "")
        except Exception as exc:
            raise AuthenticationError("invalid cookie") from exc
        item = cookies.get(COOKIE_NAME)
        if item is None or not item.value:
            raise AuthenticationError("missing access cookie")

        try:
            header = self._jwt.get_unverified_header(item.value)
            if header.get("alg") != "RS256" or not isinstance(header.get("kid"), str) or not header["kid"]:
                raise AuthenticationError("invalid JWT header")
            signing_key = self._jwks.get_signing_key_from_jwt(item.value)
        except self._jwt.PyJWKClientConnectionError as exc:
            raise AuthenticationUnavailable("JWKS unavailable") from exc
        except self._jwt.PyJWTError as exc:
            raise AuthenticationError("invalid JWT header") from exc

        try:
            claims = self._jwt.decode(
                item.value,
                signing_key.key,
                algorithms=["RS256"],
                issuer=self._issuer,
                audience=self._audience,
                leeway=30,
                options={
                    "require": ["iss", "sub", "aud", "iat", "nbf", "exp", "jti"],
                },
            )
        except (self._jwt.PyJWTError, TypeError, ValueError) as exc:
            raise AuthenticationError("invalid access token") from exc

        user_id = claims.get("sub")
        if (
            not isinstance(user_id, str)
            or not user_id
            or claims.get("tenant") != self._tenant
            or type(claims.get("ver")) is not int
            or claims["ver"] != 1
        ):
            raise AuthenticationError("invalid access claims")
        return Principal(user_id=user_id, tenant=self._tenant)
