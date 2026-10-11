import json
import time
from pathlib import Path

import httpx
import jwt
import pytest
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.asymmetric import rsa

from thinkhome_zulip_mcp.auth import AuthConfig, Authenticator
from thinkhome_zulip_mcp.executor import Principal
from thinkhome_zulip_mcp.store import IdentityStore


@pytest.fixture
def auth_fixture(tmp_path: Path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    public.update(kid="test-key", alg="RS256", use="sig")
    requests = []

    def jwks(request):
        requests.append(request)
        return httpx.Response(200, json={"keys": [public]})

    store = IdentityStore(tmp_path / "db", Fernet.generate_key())
    principal = Principal(
        "alice", "https://zulip.example", "alice@example", "synthetic-key"
    )
    store.add_identity(principal)
    store.bind_oauth("alice", issuer="https://issuer.example", subject="alice-sub")
    config = AuthConfig(
        "https://mcp.example/mcp",
        "https://zulip.example",
        issuer="https://issuer.example",
        jwks_url="https://issuer.example/keys",
    )
    auth = Authenticator(store, config, transport=httpx.MockTransport(jwks))

    def token(**changes):
        now = int(time.time())
        claims = dict(
            iss=config.issuer,
            sub="alice-sub",
            aud=config.resource_url,
            exp=now + 300,
            iat=now,
            scope="zulip:access",
            azp="test-client",
        )
        claims.update(changes)
        return jwt.encode(claims, key, algorithm="RS256", headers={"kid": "test-key"})

    return auth, store, principal, token, requests


@pytest.mark.asyncio
async def test_manual_and_oauth_map_to_same_principal(auth_fixture):
    auth, store, principal, token, requests = auth_fixture
    manual = store.issue_token("alice", expires_at=int(time.time()) + 100)
    for bearer in (manual, token()):
        access = await auth.verify_token(bearer)
        assert access is not None
        assert auth.principal(access) == principal
        assert access.resource == auth.config.resource_url
    assert len(requests) == 1
    assert "authorization" not in requests[0].headers
    store.revoke_token(manual)
    assert await auth.verify_token(manual) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes",
    [
        {"iss": "https://attacker.example"},
        {"aud": "other-resource"},
        {"exp": 1},
        {"iat": 9999999999},
        {"nbf": 9999999999},
        {"scope": "other"},
        {"sub": "unmapped"},
        {"azp": None},
    ],
)
async def test_wrong_issuer_audience_expiry_is_rejected(auth_fixture, changes):
    auth, _, _, token, _ = auth_fixture
    assert await auth.verify_token(token(**changes)) is None


@pytest.mark.asyncio
async def test_unknown_oauth_subject_has_no_zulip_access(auth_fixture):
    auth, store, _, token, _ = auth_fixture
    assert await auth.verify_token(token(sub="unknown")) is None
    valid = token()
    assert await auth.verify_token(valid) is not None
    store.remove_identity("alice")
    assert await auth.verify_token(valid) is None


@pytest.mark.asyncio
async def test_signature_algorithm_and_untrusted_jku_are_rejected(auth_fixture):
    auth, _, _, token, requests = auth_fixture
    valid = token()
    claims = jwt.decode(valid, options={"verify_signature": False})
    forged = jwt.encode(
        claims,
        "attacker-key-that-is-at-least-32-bytes",
        algorithm="HS256",
        headers={"kid": "test-key", "jku": "https://attacker.example/keys"},
    )
    assert await auth.verify_token(forged) is None
    assert not requests
    assert await auth.verify_token("broken") is None
    assert await auth.verify_token("x" * 20000) is None


@pytest.mark.asyncio
async def test_identity_cannot_escape_configured_realm(auth_fixture):
    auth, store, _, token, _ = auth_fixture
    store.update_identity(
        Principal("alice", "https://other.example", "alice@example", "key")
    )
    assert await auth.verify_token(token()) is None


@pytest.mark.asyncio
async def test_manual_only_profile_does_not_fetch_jwks(auth_fixture):
    auth, store, principal, _, requests = auth_fixture
    manual_only = Authenticator(
        store, AuthConfig(auth.config.resource_url, principal.realm_url)
    )
    bearer = store.issue_token("alice", expires_at=int(time.time()) + 100)
    assert await manual_only.verify_token(bearer) is not None
    assert await manual_only.verify_token("some.jwt.token") is None
    assert not requests


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [[], {"keys": [None]}, {"keys": "invalid"}])
async def test_invalid_jwks_fails_closed(auth_fixture, body):
    auth, store, _, token, _ = auth_fixture
    invalid = Authenticator(
        store,
        auth.config,
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body)),
    )
    assert await invalid.verify_token(token()) is None


@pytest.mark.asyncio
async def test_jwks_redirect_not_followed_and_refresh_is_bounded(auth_fixture):
    auth, store, _, token, _ = auth_fixture
    requests = []

    def redirect(request):
        requests.append(request)
        return httpx.Response(
            302, headers={"location": "https://attacker.example/keys"}
        )

    verifier = Authenticator(
        store, auth.config, transport=httpx.MockTransport(redirect)
    )
    assert await verifier.verify_token(token()) is None
    assert await verifier.verify_token(token()) is None
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_wrong_rsa_signature_and_oversized_jwks_are_rejected(auth_fixture):
    auth, store, _, token, _ = auth_fixture
    claims = jwt.decode(token(), options={"verify_signature": False})
    wrong_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    forged = jwt.encode(
        claims, wrong_key, algorithm="RS256", headers={"kid": "test-key"}
    )
    assert await auth.verify_token(forged) is None
    oversized = Authenticator(
        store,
        auth.config,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=b" " * (1024 * 1024 + 1))
        ),
    )
    assert await oversized.verify_token(token()) is None


def test_resource_url_must_identify_an_explicit_endpoint():
    with pytest.raises(ValueError, match="path"):
        AuthConfig("https://mcp.example", "https://zulip.example")
