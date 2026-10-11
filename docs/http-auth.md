# HTTP authorization (development)

`serve --transport http` exposes authenticated Streamable HTTP using MCP SDK
2.3.0. Each HTTP request verifies its bearer token. Each execution resolves the
current provisioned Zulip identity again; no session stores a Zulip API key and
there is no admin fallback. The configured `ZULIP_URL` limits this HTTP instance
to one realm. The local stdio profile remains independent of the identity DB.

## Manual tokens

First follow [identity provisioning](identity-provisioning.md), then configure:

```sh
export MCP_STORE_PATH=/private/path/identities.db
export MCP_KEY_FILE=/private/path/encryption-key
export ZULIP_URL=https://zulip.example
export MCP_RESOURCE_URL=https://mcp.example/mcp
thinkhome-zulip-mcp serve --transport http --host 127.0.0.1 --port 8000
```

The public resource URL must be the exact MCP endpoint clients use. Put TLS on a
trusted reverse proxy; the bundled listener binds loopback by default. Preserve
the configured Host header and path when proxying. Do not expose the plaintext
listener publicly. The app accepts only its configured Host and same-origin
Origin; missing Origin is allowed for native MCP clients. Forwarded headers are
not trusted. Access logs are disabled. Query/body tokens are not accepted.

Clients send `Authorization: Bearer <MCP token>` on every request, including
session reuse and deletion. Supply tokens from secret storage, never through
chat/tool arguments. A session ID alone grants no access. Cross-user reuse is
rejected even when two OAuth users share a client ID. Revoked manual tokens and
removed identities are refused on the next request; already dispatched upstream
writes cannot be recalled.

A missing or invalid bearer returns 401 with a `WWW-Authenticate` metadata URL.
The resource metadata lives at `/.well-known/oauth-protected-resource/mcp` for the
example above. Manual-only mode omits `authorization_servers`, since it has no
OAuth login provider. This follows the optional field and omission rules in
[RFC 9728 sections 2 and 3.2](https://www.rfc-editor.org/rfc/rfc9728.html#section-2);
the SDK's OAuth-specific metadata model requires an authorization server, so the
manual-only metadata response is supplied separately. OAuth mode uses SDK routes.

## External OAuth

Add both settings to enable OAuth alongside manual tokens:

```sh
export MCP_OAUTH_ISSUER=https://login.example/realms/mcp
export MCP_OAUTH_JWKS_URL=https://login.example/realms/mcp/protocol/openid-connect/certs
```

These are administrator-provisioned endpoints. JWT headers never select the
issuer, key URL or Zulip origin. Configure the external provider/client to issue
RS256 access tokens with:

- exact `iss` equal to `MCP_OAUTH_ISSUER`;
- `aud` containing the exact `MCP_RESOURCE_URL`;
- required `exp`, `iat`, nonempty `sub`, and `azp` (or `client_id`);
- `scope` containing `zulip:access`; optional `nbf` must be valid.

For Keycloak, retain the `basic` client scope or add its Subject mapper so access
tokens include `sub`; an ID token containing `sub` does not prove the access token
has it. Provision each exact `(issuer, subject)` using `identity bind-oauth`. An unknown
subject has no access even if the token is otherwise valid. Email claims are not
identity mappings. Protected-resource discovery advertises the external issuer;
the OAuth provider supplies login, PKCE and its own discovery endpoints. This MCP
does not run an authorization server or login frontend.

JWKS is fetched without credentials, environment proxies or redirects, with a
10-second timeout and 1-MiB body bound. Public RSA keys are cached for five minutes.
An unknown key ID can trigger at most one refresh per 30 seconds. Providers must
overlap signing keys during rotation; a newly introduced key may be rejected
briefly during the refresh cooldown. Unavailable/invalid keys fail closed. The
resource server validates JWTs locally, so provider-side token revocation is not
instant introspection: use short access-token lifetimes; removing the local
identity denies its subsequent requests immediately.

## Limits and evidence

Requests are bounded to 1 MiB, with at most 1,000 sessions and a 30-minute idle
session timeout. JSON responses are used; event workflows will be implemented
separately. Test-only plaintext URLs require `MCP_ALLOW_INSECURE_HTTP=1`; identities
for plaintext Zulip must also be provisioned with `--allow-insecure-http`.

Tests exercise signed JWT validation, malformed and oversized JWKS, origin checks,
MCP initialize/tool calls, two concurrent identities, session theft, revocation,
termination and log exclusion using an ASGI app and synthetic upstream responses.
The [Keycloak 26.8 fixture](keycloak-testing.md) additionally exercises real PKCE
and two official SDK clients over loopback sockets. Actual Zulip integration and
production TLS/proxy validation remain pending. This is not a production-readiness claim.
