"""Keep provider fixtures and CI pins aligned with their security contract."""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_keycloak_fixture_requires_pkce_and_subject():
    realm = json.loads((ROOT / "tests/fixtures/keycloak-realm.json").read_text())
    client = realm["clients"][0]
    assert client["publicClient"] is True
    assert client["standardFlowEnabled"] is True
    assert client["attributes"]["pkce.code.challenge.method"] == "S256"
    for grant in (
        "directAccessGrantsEnabled",
        "implicitFlowEnabled",
        "serviceAccountsEnabled",
    ):
        assert client[grant] is False
    mappers = {
        mapper["protocolMapper"]: mapper["config"]
        for mapper in realm["clientScopes"][0]["protocolMappers"]
    }
    assert mappers["oidc-sub-mapper"]["access.token.claim"] == "true"
    assert mappers["oidc-audience-mapper"]["id.token.claim"] == "false"
    assert all("credentials" not in user for user in realm["users"])


def test_ci_pins_match_provider_manifest():
    manifest = json.loads((ROOT / "tests/fixtures/oauth-runtime.json").read_text())
    workflow = (ROOT / ".github/workflows/integration.yml").read_text()
    for artifact in (manifest["keycloak"], manifest["java"]["linux-x64"]):
        assert artifact["url"] in workflow
        assert artifact["sha256"] in workflow
    assert "tests/integration/test_keycloak.py" in workflow
    assert 'test -x "$KEYCLOAK_HOME/bin/kc.sh"' in workflow
