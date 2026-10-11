# API coverage — work in progress

Target: Zulip **12.3**, commit `b31982251ff82d0b02e59f63ad21f19204e37aea`.
Upstream main observed at `d7c57ed15b82b0bc8aefe2007f5ee2abeebd0ea2` (2026-10-11).
The main revision is recorded, not yet audited.

The bundled OpenAPI is byte-for-byte upstream material with its Apache-2.0
license and SHA-256 in `data/provenance.json`. This upstream license does not
select the license for our own implementation.

## Current evidence

- 158 schema operations loaded after three explicitly documented exclusions.
- 0 executable operations, 0 wire-contract-tested operations, 0 integration-tested
  operations. Catalog tests verify schema retention and duplicate/missing ID checks.
- The three documentation-only paths are listed in `data/exclusions.json`, with
  evidence from upstream `zerver/tests/test_openapi.py:241-249`.
- 158 is **not** a claim about total application API coverage. The schema includes
  development/bouncer APIs and does not describe all application operations.

## Outstanding audit

Inspect routes in `zproject/urls.py`, `zproject/tornado_urls.py`,
`zproject/dev_urls.py`, and included URL configurations; compare handlers,
authentication decorators, request validators and upstream tests. Dynamic routes,
SCIM, direct file routes, cookie-only operations and provider integrations need
explicit classifications. Preserve every request parameter and encoding.

Source inspection already identified undocumented operations including organization
updates, bot management and onboarding; their complete schemas, roles and session
requirements are not yet audited. They remain required work before v1.

No operation may be declared implemented merely because its metadata loads.
