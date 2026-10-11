"""Bearer verification and fresh, provisioned identities for HTTP requests."""

import asyncio
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import jwt
from mcp.server.auth.provider import AccessToken

from thinkhome_zulip_mcp.executor import Principal
from thinkhome_zulip_mcp.store import AuthenticationError, IdentityStore


@dataclass(frozen=True)
class AuthConfig:
    resource_url: str
    realm_url: str
    issuer: str | None = None
    jwks_url: str | None = None
    allow_insecure_http: bool = False

    def __post_init__(self) -> None:
        if bool(self.issuer) != bool(self.jwks_url):
            raise ValueError("OAuth requires both issuer and JWKS URL")
        for url in (self.resource_url, self.realm_url, self.issuer, self.jwks_url):
            if url is not None:
                Principal("config", url, "config", "config", self.allow_insecure_http)
        if not urlsplit(self.resource_url).path:
            raise ValueError("MCP resource URL must include its endpoint path")


class Authenticator:
    def __init__(
        self,
        store: IdentityStore,
        config: AuthConfig,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.store = store
        self.config = config
        self._transport = transport
        self._keys: dict[str, Any] = {}
        self._fetched_at = float("-inf")
        self._attempted_at = float("-inf")
        self._lock = asyncio.Lock()

    async def _key(self, kid: str) -> Any:
        async with self._lock:
            now = time.monotonic()
            if now - self._fetched_at < 300 and kid in self._keys:
                return self._keys[kid]
            # Unknown key IDs cannot force an unbounded stream of network requests.
            if now - self._attempted_at < 30:
                raise AuthenticationError("Signing key unavailable")
            self._attempted_at = now
            async with httpx.AsyncClient(
                transport=self._transport,
                trust_env=False,
                follow_redirects=False,
                timeout=10,
            ) as client:
                async with client.stream("GET", self.config.jwks_url or "") as response:
                    response.raise_for_status()
                    body = bytearray()
                    async for chunk in response.aiter_bytes(chunk_size=65536):
                        body.extend(chunk)
                        if len(body) > 1024 * 1024:
                            raise AuthenticationError("Signing key response too large")
            document = json.loads(body)
            if not isinstance(document, dict) or not isinstance(
                document.get("keys"), list
            ):
                raise AuthenticationError("Invalid signing key response")
            keyset = jwt.PyJWKSet.from_dict(document)
            keys = {}
            for key in keyset.keys:
                if (
                    key.key_type == "RSA"
                    and key.algorithm_name == "RS256"
                    and key.public_key_use in (None, "sig")
                    and key.key_id
                ):
                    if key.key_id in keys:
                        raise AuthenticationError("Ambiguous signing key")
                    keys[key.key_id] = key.key
            self._keys = keys
            self._fetched_at = time.monotonic()
            if kid not in keys:
                raise AuthenticationError("Signing key unavailable")
            return keys[kid]

    def principal(self, access: AccessToken) -> Principal:
        """Resolve again at tool execution; never cache credentials on a session."""
        if access.token.startswith("thm_"):
            principal = self.store.authenticate(access.token)
        else:
            claims = access.claims or {}
            if (
                not self.config.issuer
                or claims.get("iss") != self.config.issuer
                or not access.subject
                or not access.expires_at
                or access.expires_at <= time.time()
            ):
                raise AuthenticationError("Invalid credential")
            principal = self.store.oauth_principal(self.config.issuer, access.subject)
        if principal.realm_url.rstrip("/") != self.config.realm_url.rstrip("/"):
            raise AuthenticationError("Invalid credential")
        return principal

    async def verify_token(self, token: str) -> AccessToken | None:
        if not token or len(token) > 16384:
            return None
        try:
            if token.startswith("thm_"):
                principal = self.store.authenticate(token)
                access = AccessToken(
                    token=token,
                    client_id="manual:" + principal.identity_id,
                    subject=principal.identity_id,
                    scopes=["zulip:access"],
                    resource=self.config.resource_url,
                    claims={"iss": "urn:thinkhome:manual-token"},
                )
            else:
                if not self.config.issuer:
                    return None
                header = jwt.get_unverified_header(token)
                kid = header.get("kid")
                if header.get("alg") != "RS256" or not isinstance(kid, str) or not kid:
                    return None
                claims = jwt.decode(
                    token,
                    await self._key(kid),
                    algorithms=["RS256"],
                    issuer=self.config.issuer,
                    audience=self.config.resource_url,
                    options={"require": ["iss", "sub", "aud", "exp", "iat"]},
                )
                scope = claims.get("scope")
                client_id = claims.get("azp", claims.get("client_id"))
                if (
                    not isinstance(scope, str)
                    or "zulip:access" not in scope.split()
                    or not isinstance(client_id, str)
                    or not client_id
                ):
                    return None
                access = AccessToken(
                    token=token,
                    client_id=client_id,
                    subject=claims["sub"],
                    scopes=scope.split(),
                    expires_at=claims["exp"],
                    resource=self.config.resource_url,
                    claims={"iss": self.config.issuer},
                )
            self.principal(access)
            return access
        except (ValueError, TypeError, KeyError, jwt.PyJWTError, httpx.HTTPError):
            return None


def authenticator_from_environment() -> Authenticator:
    from thinkhome_zulip_mcp.provisioning import read_secret

    db_path = os.environ.get("MCP_STORE_PATH")
    key_path = os.environ.get("MCP_KEY_FILE")
    if not db_path or not key_path:
        raise ValueError("HTTP requires MCP_STORE_PATH and MCP_KEY_FILE")
    config = AuthConfig(
        os.environ.get("MCP_RESOURCE_URL", ""),
        os.environ.get("ZULIP_URL", ""),
        issuer=os.environ.get("MCP_OAUTH_ISSUER"),
        jwks_url=os.environ.get("MCP_OAUTH_JWKS_URL"),
        allow_insecure_http=os.environ.get("MCP_ALLOW_INSECURE_HTTP") == "1",
    )
    return Authenticator(
        IdentityStore(Path(db_path), read_secret(Path(key_path)).encode()), config
    )
