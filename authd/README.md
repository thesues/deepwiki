# authd

A standalone Go 1.26 / Gin 1.12 service for GitHub / Feishu login and central SSO.
SQLite uses `mattn/go-sqlite3` (CGO), queries are generated with sqlc 1.30,
and embedded schema migrations run through goose on startup.

## Flow and endpoints

```text
DeepWiki /auth/login -> authd (DeepWiki Host)
  -> AUTH_HOST/sso/authorize
  -> Configured provider (only if the central SSO cookie is absent/expired)
  -> AUTH_HOST/oauth/{github|feishu}/callback
  -> DEEPWIKI_HOST/auth/callback?code=...
  -> DeepWiki /
```

APIG sends `AUTH_HOST/*` and each application's `/auth/*` directly to authd.
The backend only verifies its audience-specific JWT using the JWKS endpoint.
`/.well-known/jwks.json` is public; `/healthz` is an anonymous process probe.

* `__Host-auth_sso`: central Host only, `aud=authd-sso`.
* `__Host-auth_access`: each app Host separately, `aud=<registered app ID>`.
* Both are RS256 JWTs, eight hours, `Secure; HttpOnly; SameSite=Lax; Path=/`,
  no Domain attribute and no refresh token/session.
* Temporary `__Host-auth_request` (app Host, ten minutes) and `__Host-auth_flow`
  (central Host, five minutes) bind redirects to the initiating browser. They are
  removed after callback. They are not additional long-lived login credentials.
* Application codes expire after 60 seconds; OAuth state expires after five minutes.
  SQLite stores code/state hashes plus provider name and a temporary PKCE verifier.
  Code consumption atomically checks app, Host, browser binding, expiry and
  single use. URLs and cookies are never included in request logs.

JWT claims: `iss=buda-authd`, deterministic `sub`, application `aud`, `iat`,
`nbf`, `exp`, `jti`, `tenant=default`, `ver=1`. Subject is the unpadded base64url
SHA-256 of `authd:v1\0<tenant_key>\0<union_id or open_id>`, prefixed with `u_`.
Keep the Feishu application/identifier policy stable: changing from open_id to
union_id changes the derived subject. GitHub subjects hash
`authd:v1\0<tenant_key>\0github\0<numeric GitHub user ID>` and use the same `u_`
prefix. GitHub usernames and email addresses are not identity keys; renaming a
GitHub account does not change its authd identity. Providers are separate identity
spaces and are not automatically merged. No SQLite user-ID mapping is needed.

## Configuration

| Variable | Meaning |
| --- | --- |
| `AUTH_PUBLIC_URL` | HTTPS central SSO origin, no path |
| `AUTH_APPS_JSON` | App-ID to HTTPS origin map, e.g. `{"deepwiki":"https://wiki.example.com","lerobot":"https://robot.example.com"}` |
| `AUTH_PROVIDER` | `github` or `feishu`; default `feishu` for existing deployments |
| `GITHUB_CLIENT_ID`, `GITHUB_CLIENT_SECRET` | Required when `AUTH_PROVIDER=github` |
| `GITHUB_ALLOWLIST_FILE` | Username list on persistent storage; default `/var/lib/authd/github-allowlist.txt` |
| `FEISHU_APP_ID`, `FEISHU_APP_SECRET` | Required when `AUTH_PROVIDER=feishu` |
| `JWT_PRIVATE_KEY_FILE`, `JWT_KID` | RSA PEM signing key file and unique key ID |
| `JWT_PREVIOUS_JWKS_FILE` | Optional JSON file of previous public keys for rotation |
| `AUTH_DB_PATH` | Default `/var/lib/authd/authd.db` |
| `AUTH_TENANT_KEY` | Default `default`; must match backend policy |
| `LISTEN_ADDR` | Default `:8080` |

Register `https://AUTH_HOST/oauth/feishu/callback` as the Feishu redirect URL.
Feishu v2 token exchange uses `client_id`, `client_secret`, `redirect_uri` and
`grant_type=authorization_code`; no Feishu access/refresh token is persisted.
The deployment's Feishu application visibility determines who may authorize.

### GitHub OAuth

Register a GitHub OAuth App with the application's public homepage and exact
callback `https://AUTH_HOST/oauth/github/callback`. The provider uses GitHub's
authorization code flow with PKCE S256 and browser-bound, single-use state. It
requests no scopes: public identity is enough, and repository/private email
permissions are not needed. The access token is used only for `GET /user`; neither
access nor refresh tokens are persisted. Token and user requests refuse redirects.

Select `AUTH_PROVIDER=github` and supply `GITHUB_CLIENT_ID` and
`GITHUB_CLIENT_SECRET`. The Kubernetes template reads these from the separate
`authd-github-secrets` Secret, keys `client-id` and `client-secret`. Keep credentials
out of the repository. Feishu code and secrets remain available, but only the
selected provider's callback is served. Unknown provider names fail startup.

GitHub login requires a username in `GITHUB_ALLOWLIST_FILE` (default:
`/var/lib/authd/github-allowlist.txt`). Store this file on the authd PVC, never in
Git. Use one GitHub username per line, without `@` or a profile URL. Matching is
case-insensitive; blank lines and lines starting with `#` are ignored. The file
is read on every login. Empty lists deny all users (HTTP 403); missing, unreadable
or malformed files disable GitHub login (HTTP 503), with details in authd logs.
Username changes require updating the list; data ownership still uses numeric ID.

To replace the list from a local file without restarting authd:

```sh
AUTHD_POD=$(kubectl -n autumn get pod -l app=authd -o jsonpath='{.items[0].metadata.name}')
kubectl -n autumn cp ./github-allowlist.txt "$AUTHD_POD:/var/lib/authd/github-allowlist.txt.new" -c authd --no-preserve
kubectl -n autumn exec "$AUTHD_POD" -c authd -- mv /var/lib/authd/github-allowlist.txt.new /var/lib/authd/github-allowlist.txt
```

Copy to `.new` then rename so concurrent logins never read a half-written file.
GitHub SSO cookies do not skip the username/allowlist check. Removing a username
blocks subsequent logins; existing application JWTs remain valid for up to eight
hours plus clock leeway. This is a login allowlist, not immediate session revocation.

Before rollout, test `github.com` and `api.github.com` from the authd Pod itself.
When replacing Feishu with GitHub exclusively, stop authd, clear pending OAuth
states/application codes (or wait for their expiry), and switch to a fresh signing
key and key ID without retaining the old public key. Refresh/restart downstream
JWKS verifiers so old Feishu cookies cannot remain authenticated. Preserve the
old signing Secret for rollback and existing user data; do not assign that data
to GitHub users. A provider setting alone does not revoke issued cookies.

For key rotation, save the current JWKS, mount it as `JWT_PREVIOUS_JWKS_FILE`,
then deploy the new private key and a new `JWT_KID`. Keep previous keys for at
least eight hours plus 30 seconds after the last old-key issuance. Removing an
old public key takes up to five minutes to reach backend caches. Old keys can
verify SSO while being retained; only the new private key signs fresh JWTs.

SQLite loss invalidates in-progress redirects. Signed JWTs, subjects and Hermes
history survive if the signing Secret and identity configuration survive. Back
up signing Secrets separately. This version has no immediate per-user revocation
or global logout: tokens expire after eight hours. Clearing a single app cookie
does not clear the central SSO cookie.

## Build and tests

```sh
cd authd
go run github.com/sqlc-dev/sqlc/cmd/sqlc@v1.30.0 generate
go test -race ./...
go build ./cmd/authd
```

Production images are built and pushed through Volcengine CP. Inspect the live
pipeline before starting a run and verify its source commit and output image.
The existing `dongmao-workspace / buda-webui` pipeline builds
`docker/Dockerfile.webui`. The user creates a **dedicated authd pipeline** for
`docker/Dockerfile.authd`, using the application's normal `main` branch.
Do not use ref/resource-reference overrides or temporary build branches, and
do not repurpose the WebUI pipeline. Deploy the image tagged with the source
commit SHA after CP confirms it has been pushed.

The builder has a C compiler; the Debian runtime supplies libc and TLS roots.
The deployment is one non-root Pod with a dedicated EBS RWO PVC and Recreate
strategy. Multiple replicas require a shared code/state store first.

## Rollout

1. Build/push authd and WebUI images. Replace `IMAGE_AUTHD`, `IMAGE_WEBUI`,
   `AUTH_HOST`, `DEEPWIKI_HOST` in the authd ConfigMap with public HTTPS Hosts.
   Keep the Ingress aliases `authd.apig.local` and `deepwiki.apig.local`: APIG
   assigns the public domains separately; using those reserved domains as
   Ingress hosts can break TLS. Add LeRobot to `AUTH_APPS_JSON` when its
   `/auth` route and audience verifier are ready; its source is not in this repo.
2. Create `authd-secrets` in `autumn` with keys `feishu-app-id`,
   `feishu-app-secret`, `jwt-kid`, `jwt-private-key`. Generate an RSA key of at
   least 2048 bits locally and import it from a file; do not put secrets in YAML.
3. Deploy authd. Configure APIG HTTPS/certificates for all public Hosts. Ingress
   YAML alone does not provision a public domain or certificate. Preserve the
   browser's original Host upstream, query parameters, Set-Cookie and Location;
   authd intentionally does not trust arbitrary X-Forwarded-Host headers.
4. Verify APIG `/auth` Prefix takes precedence over `/`, and verify the real
   Feishu callback and per-Host cookies with a browser. Do not share a Domain
   cookie on the provider's public parent domain.
5. Stop/drain WebUI before the first authenticated rollout. Back up state.db
   using SQLite's backup API and back up transcript files. Run
   `k8s/scripts/clear-anonymous-sessions.py` with Hermes' interpreter from a
   maintenance Pod mounting the WebUI PVC: first dry-run, then `--apply`.
   Never assign old anonymous sessions to a new identity.
6. Deploy WebUI with `AUTH_JWKS_URL=http://authd.autumn.svc:8080/.well-known/jwks.json`.
   Confirm two users cannot list/read/delete/stream each other's resources,
   input images and artifacts work, and SSE/Range still pass through APIG.
7. Add a second app Host and confirm it obtains its own audience-specific JWT
   through central SSO without another Feishu login.

Roll back with the retained images/config and backup, while keeping the public
endpoint protected. Do not restore an anonymous image behind an open gateway.
Local tests cover Feishu and GitHub exchanges, PKCE, identity stability, provider
isolation and callback replay. A real provider/APIG smoke test
requires actual app credentials, registered public Hosts and TLS configuration.

Python backend integration (including authentication-only services) is documented
in [webui/AUTH.md](../webui/AUTH.md).
