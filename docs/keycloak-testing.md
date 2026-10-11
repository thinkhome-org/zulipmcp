# Isolated Keycloak integration

The fixture uses Keycloak 26.8.0 and Amazon Corretto 25.0.4.10.1. Download URLs
and SHA-256 values are recorded in
[`oauth-runtime.json`](../tests/fixtures/oauth-runtime.json). The Linux CI workflow
checks both archives before extraction. No system Java installation or Docker
service is required. These runtime pins are test infrastructure, not a deployment
recommendation.

## Run locally

Download the archives for your OS from the manifest, verify SHA-256, and extract
them to an ignored directory outside source files. Set `JAVA_HOME` to the JDK home
(on macOS, the bundle's `Contents/Home` directory), and `KEYCLOAK_HOME` to the
extracted Keycloak distribution:

```sh
JAVA_HOME=/absolute/path/to/jdk/home \
KEYCLOAK_HOME=/absolute/path/to/keycloak-26.8.0 \
uv run pytest tests/integration/test_keycloak.py -q
```

The test copies the provider distribution into pytest's temporary directory,
imports a fresh realm and starts Keycloak and MCP as separate processes on random
loopback ports. Passwords, signing keys, H2 data and MCP credentials are disposable.
The test terminates its own processes on exit; pytest may retain temporary files
for failure diagnosis. Treat those directories as test secrets and remove them
when diagnostics are no longer needed. No real Zulip request is sent.

The fixture is a preregistered **public client requiring S256 PKCE**. Password
grants, implicit grants, service accounts, registration and password reset are
disabled. The checked-in realm contains no passwords; each run generates one. The fixture
explicitly maps `sub` into access tokens, which modern Keycloak normally provides
through its `basic` client scope.
Actual login form requests are sent through HTTPX, not browser automation. Keycloak
recognizes loopback as a secure context and sets Secure cookies even over HTTP;
the test's cookie adapter is restricted to `127.0.0.1`, matching that local secure
context behavior. It is not used by the MCP application. See the pinned
[Keycloak secure-context implementation](https://github.com/keycloak/keycloak/blob/26.8.0/services/src/main/java/org/keycloak/utils/SecureContextResolver.java).

## Assertions

- Real issuer discovery advertises S256 and the expected issuer.
- Missing PKCE, an incorrect verifier of valid length, and authorization-code
  replay are refused. Missing PKCE is an OAuth error redirect to the registered
  callback, not a successful login.
- Two different users receive actual signed access tokens through Authorization
  Code + PKCE with exact MCP audience and `zulip:access` scope.
- The MCP fetches the provider's actual JWKS and validates the access tokens.
- Two official SDK network clients initialize with MCP `2025-11-25`, call a tool
  and terminate sessions. ASGI tests also cover the `2025-03-26` handshake; the
  SDK's newer discovery protocol is not yet certified by this fixture.
- Removing a local identity denies its subsequent requests.
- Generated passwords and access tokens do not appear in provider or MCP logs.

The normal fast suite skips this one test unless `KEYCLOAK_HOME` is configured.
`.github/workflows/integration.yml` always configures it and runs it explicitly.
Its current `integration-gate` covers OAuth/provider integration only; the actual
Zulip integration jobs remain to be added before the full release gate is enabled.
ASGI tests separately cover stolen sessions and concurrent upstream credential
isolation. This fixture does not prove actual Zulip API coverage or production
TLS/proxy configuration.

Sources: [Keycloak OpenJDK quickstart](https://www.keycloak.org/getting-started/getting-started-zip),
[Corretto downloads](https://docs.aws.amazon.com/corretto/latest/corretto-25-ug/downloads-list.html).
