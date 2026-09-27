"""A standalone backend can authenticate without importing any WebUI logic."""
import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from auth import AuthenticationError, AuthenticationUnavailable, JWTAuthenticator


@pytest.fixture
def authority():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    state = {"keys": [dict(json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key())), kid="one", use="sig", alg="RS256")], "requests": 0}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            state["requests"] += 1
            body = json.dumps({"keys": state["keys"]}).encode()
            self.send_response(200)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield key, f"http://127.0.0.1:{server.server_port}/jwks", state
    server.shutdown()
    server.server_close()
    thread.join()


def cookie(key, **changes):
    now = int(time.time())
    claims = dict(iss="buda-authd", sub="test-user", aud="deepwiki", tenant="default", ver=1,
                  iat=now, nbf=now, exp=now + 3600, jti="test")
    claims.update(changes)
    return "__Host-auth_access=" + jwt.encode(claims, key, algorithm="RS256", headers={"kid": "one"})


def test_backend_only_needs_auth_file_and_its_audience(authority):
    key, url, state = authority
    backend = JWTAuthenticator(url, audience="lerobot")
    principal = backend.authenticate(cookie(key, aud="lerobot"))
    assert principal.user_id == "test-user"
    assert principal.tenant == "default"
    backend.authenticate(cookie(key, aud="lerobot"))
    assert state["requests"] == 1
    with pytest.raises(AuthenticationError):
        backend.authenticate(cookie(key))


@pytest.mark.parametrize("changes", [
    {"aud": "lerobot"}, {"iss": "attacker"}, {"exp": 0},
    {"nbf": 9999999999}, {"iat": 9999999999}, {"tenant": "other"},
    {"ver": True}, {"sub": ""}, {"exp": None},
])
def test_invalid_claims_rejected(authority, changes):
    key, url, _ = authority
    with pytest.raises(AuthenticationError):
        JWTAuthenticator(url, audience="deepwiki").authenticate(cookie(key, **changes))


def test_missing_and_forged_credentials(authority):
    key, url, _ = authority
    verifier = JWTAuthenticator(url, audience="deepwiki")
    for value in (None, "", "__Host-auth_access=invalid"):
        with pytest.raises(AuthenticationError):
            verifier.authenticate(value)
    wrong = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises(AuthenticationError):
        verifier.authenticate(cookie(wrong))


def test_jwks_network_error_is_not_bad_credentials(authority, monkeypatch):
    key, url, _ = authority
    verifier = JWTAuthenticator(url, audience="deepwiki")
    def unavailable(*args):
        raise jwt.PyJWKClientConnectionError("offline")
    monkeypatch.setattr(verifier._jwks, "get_signing_key_from_jwt", unavailable)
    with pytest.raises(AuthenticationUnavailable):
        verifier.authenticate(cookie(key))


def test_single_file_copy_has_no_business_dependencies(authority, tmp_path):
    import os
    import shutil
    import subprocess
    key, url, _ = authority
    shutil.copyfile(Path(__file__).resolve().parents[1] / "auth.py", tmp_path / "auth.py")
    code = '''
import sys
from auth import JWTAuthenticator
principal = JWTAuthenticator(sys.argv[1], audience="lerobot").authenticate(sys.argv[2])
assert principal.user_id == "test-user"
assert not {"hermes_agent", "http_shell", "app_routes", "turns"}.intersection(sys.modules)
'''
    subprocess.run([sys.executable, "-c", code, url, cookie(key, aud="lerobot")],
                   cwd=tmp_path, env=os.environ.copy(), check=True, capture_output=True, timeout=10)
