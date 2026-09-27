# Reusing Python authentication

`auth.py` is a standalone authd JWT verifier. Its only third-party dependency is
`PyJWT[crypto]==2.10.1`. Copy this one file into another Python backend; it imports
no DeepWiki, Hermes, database or HTTP framework code.

Construct **one verifier per process**, then authenticate each protected request:

```python
from auth import JWTAuthenticator, AuthenticationError, AuthenticationUnavailable

verifier = JWTAuthenticator(
    "http://authd.autumn.svc:8080/.well-known/jwks.json",
    audience="lerobot",   # fixed identity of THIS backend
)

# In your framework's middleware / dependency:
try:
    identity = verifier.authenticate(request.headers.get("Cookie"))
except AuthenticationError:
    # Return 401 for APIs; page navigation may redirect to /auth/login.
    ...
except AuthenticationUnavailable:
    # Return 503 (do not send the browser through a login loop).
    ...
else:
    # Authentication-only services can now pass the request to their handler.
    # identity.user_id and identity.tenant are available if the handler needs them.
    ...
```

The verifier reads `__Host-auth_access`, uses the configured JWKS URL and JWT
`kid`, fixes the algorithm to RS256, validates issuer/audience/timestamps and
requires `iss`, `sub`, `aud`, `iat`, `nbf`, `exp`, `jti`, `tenant` and `ver=1`.
It accepts authd's protocol defaults (`issuer=buda-authd`, `tenant=default`);
other trusted deployments can supply those constructor arguments explicitly.
Keys are cached for five minutes; unknown `kid` triggers a refresh. It never
follows a JWT-supplied key URL. Missing/bad tokens raise `AuthenticationError`;
JWKS transport failure raises `AuthenticationUnavailable`.

`Principal` contains identity only. The file does not create cookies, redirect,
consume authorization codes, access a session database, or filter any history.
Routing `/auth/*` to authd and translating exceptions into HTTP responses belong
to the host application. Cookie authentication also requires the host to check
Origin/CSRF for requests that change state.

## DeepWiki integration

The only deployment environment variable is `AUTH_JWKS_URL`. `main.py` fixes
`audience="deepwiki"`. With no URL, existing anonymous/Basic Auth mode is retained;
Basic and JWT must not be configured simultaneously.

`http_shell.py` attaches the returned `Principal` to the request. `app_routes.py`
and `turns.py` implement DeepWiki-specific session, stream, approval and resource
ownership. `deepwiki_cid` is only a browser cursor; cursors are keyed by user and
browser. Agent creation passes `user_id=sub`. Hermes 0.17 does not forward this
argument to its lazy session writer or compression writer, so `UserSessionDB`
wraps those writes to stamp the existing `sessions.user_id` column. No tenant
side database is created. Pending turns hold their owner before the first write.

Each user can run one turn; endpoint concurrency remains global. Generated
artifacts must live under `/artifacts/<session-id>/...`; files at the root of
`artifacts` are inaccessible in JWT mode because they have no owner. Input image
keys use `input/webui/<user-id>/<session-id-or-draft>/<random>.png`. Authenticated
resources are private and not reusable from shared/browser caches after logout.

Hermes built-in `session_search` and global `memory` are withheld in JWT mode,
including tool refreshes, and global memory prompt injection is disabled: these
features use the shared Hermes home and otherwise bypass HTTP ownership checks.
Global “always allow” approvals are also withheld; one-time and session approvals
remain. Shared corpus MCP services still work. Terminal/file tools remain trusted
internal tools: this release is not a filesystem or container sandbox for hostile
users.
