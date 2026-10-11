# ThinkHome Zulip MCP v1.0 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Subagent-driven development is an alternative only after the user selects it. Steps use checkbox syntax for tracking.

**Goal:** Dodat univerzální, produkčně udržovatelný MCP s úplným doloženým pokrytím aplikačního API zvolené verze Zulipu, včetně nedokumentovaných operací.

**Architecture:** Připnuté OpenAPI a auditované doplňky tvoří jeden katalog operací. Sdílený executor obsluhuje stdio i víceuživatelské Streamable HTTP; běžné pracovní nástroje používají tentýž katalog. Bez vlastního webového UI a bez správy hostitele Zulipu.

**Tech Stack:** Python >=3.12, uv, oficiální MCP SDK, httpx, Pydantic, PyYAML, jsonschema, cryptography, PyJWT, SQLite; pytest, Ruff, mypy; Docker a GitHub Actions.

**Spec:** `docs/superpowers/specs/2026-10-11-thinkhome-zulip-mcp-design.md`

## Global Constraints

- Všechny přijaté požadavky a limity ve spec platí pro každý krok. Stav dokumentu: návrh k review.
- Žádný frontend ani Build Web Apps. Ovládání prohlížeče jen po výslovném pokynu uživatele.
- Žádné SSH, shell na Zulip serveru, přímé DB změny, deployment do produkce nebo vlastní AI agenti.
- Každá identita používá vlastní Zulip oprávnění; žádné admin fallback credentials.
- Veřejné a nedokumentované endpointy se auditují proti připnutému zdroji. Nedoložená nebo neimplementovaná aplikační operace blokuje plné v1.0.
- Nikdy nepoužívat produkční instanci pro testovací zápisy. Nová implementační změna obsahuje test a odpovídající dokumentaci.
- Pro nový kód použít postup test selže → minimální implementace → test projde → review → commit.

## Review Focus

1. Záměna principalu, realm, cache nebo event queue: testy souběžných uživatelů v úloze 4 a 6.
2. Nezdokumentovaný parametr, multipart nebo odlišná autentizace: úplný inventář v úloze 2 a serializační testy v úloze 3 a 5.
3. Zápis uspěje, ale odpověď se ztratí: zákaz automatického opakování v úloze 3 a recovery dokumentace v úloze 8.
4. Podvržená URL, redirect, souborová cesta nebo obsah chatu: negativní testy v úloze 3, 5 a 7; data nikdy nemění konfiguraci/autorizaci.
5. Změna upstream schématu nebo Zulip feature level: drift kontroly v úloze 2 a kompatibilita v úloze 7.

## Stav repozitáře a práce s Gitem

Při návrhu je repozitář bez commitů a remote. Dokumentace vzniká na `codex/plan-v1`. Tento stav není založený GitHub projekt ani otevřený PR.

Jednorázový bootstrap při zahájení implementace: určit cílový GitHub repository a viditelnost; vytvořit minimální počáteční commit na main potřebný jako základ PR. Žádný aplikační kód neprojde touto výjimkou. Dokumentaci návrhu převést na samostatnou větev a PR nad tímto základem. Pokud remote už mezitím existuje, nejdřív inspect/fetch a použít jeho main; žádný reset nebo přepsání historie.

Standardní cyklus každého dalšího PR:

1. Aktualizovat lokální main a založit `codex/<tema>`; nevést dlouhodobou develop větev.
2. Udělat malou ucelenou změnu, testy a dokumentaci. Commit zprávy `feat:`, `fix:`, `docs:`, `test:` nebo `ci:`.
3. Spustit lokální kontroly a pushnout větev. Push musí předcházet vytvoření GitHub PR.
4. Otevřít draft PR: účel, rozsah, test evidence, dopad na API a kompatibilitu, migrace a návrat na předchozí verzi.
5. Dokončit CI, review a opravy; každý nový push znovu spustí kontroly. Autor si vlastní PR neschvaluje.
6. Squash merge až po požadovaném review a zelených kontrolách, pak smazat větev. Merge ani publikaci nevydávat za nasazení.

Nastavit ochranu main: zákaz force push a smazání, změny přes PR, vyřešené komentáře, povinné kontroly, review jinou oprávněnou osobou. Není-li dostupný reviewer nebo příslušná funkce GitHub plánu, vyznačit blokaci; neobcházet pravidlo tichým admin mergem. CODEOWNERS naplnit skutečnými účty po určení vlastníka repo.

## Mapa souborů

| Umístění | Odpovědnost |
|---|---|
| `src/thinkhome_zulip_mcp/catalog.py`, `executor.py`, `server.py` | Operace, validace a provádění, MCP registrace a transporty |
| `src/thinkhome_zulip_mcp/auth.py`, `store.py`, `cli.py` | Principal, přihlášení, persistentní secrets a CLI |
| `src/thinkhome_zulip_mcp/files.py`, `events.py`, `workflows.py` | Binární přenos, queues, pohodlné a hromadné operace |
| `src/thinkhome_zulip_mcp/data/` | Distribuované připnuté schéma, doplňky a inventář s proveniencí |
| `scripts/catalog.py`, `tests/unit/`, `tests/contract/`, `tests/integration/` | Audit/generování/kontrola katalogu a testy |
| `.github/workflows/`, `.github/PULL_REQUEST_TEMPLATE.md`, `.github/CODEOWNERS` | CI, review a release pravidla |
| `docs/`, `README.md`, `CONTRIBUTING.md`, `SECURITY.md`, `CHANGELOG.md`, `AGENTS.md` | Užití, vývoj, bezpečnost, provoz MCP a pracovní pravidla |

## Úloha 1 — Základ projektu a pravidla přispívání

**PR:** `codex/bootstrap`; soubory `pyproject.toml`, `uv.lock`, základ balíčku, `tests/unit/test_cli.py`, `.github/workflows/ci.yml`, CONTRIBUTING a AGENTS.

**Rozhraní:** CLI `thinkhome-zulip-mcp --version`, `serve --transport stdio|http`, později `identity` a `token` podřízené příkazy. Import balíčku nesmí provádět síťová volání.

- [x] Založit repo workflow podle bootstrap pravidla, zdokumentovat vývojové prostředí a tajné údaje mimo Git.
- [x] Napsat test `test_cli_version` a `test_import_has_no_network_access`; spustit a zaznamenat očekávaný neúspěch před implementací.
- [x] Přidat minimální balíček a CI s Ruff, mypy, pytest a sestavením wheel/sdist.
- [x] Ověřit `uv sync --frozen`, `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy src`, `uv run pytest tests/unit`, `uv build`.
- [x] Aktualizovat README/CONTRIBUTING a commit `ci: bootstrap package and contribution checks`; push a PR.

**Evidence:** commits `ead2e0a`/`75f0539`, draft PR #2, CI run `38106022812` passed Python 3.12/3.14. Review/merge pending. `serve` is deferred to real transport implementation in tasks 3–4, not a no-op command.

## Úloha 2 — Úplný inventář veřejného a nedokumentovaného API

**PR:** `codex/api-catalog`; katalogová data, `scripts/catalog.py`, `catalog.py`, `tests/contract/test_inventory.py`, `docs/api-coverage.md`.

**Rozhraní:** `Operation` obsahuje pole definovaná ve spec; `load_catalog() -> dict[str, Operation]`. `python scripts/catalog.py check` je read-only deterministická kontrola; `update` vytváří reviewovatelný diff. ID odvodit z operationId, kolize a chybějící ID vyřešit explicitním stabilním mapováním.

- [ ] Připnout stabilní Zulip tag/SHA a druhé auditované SHA main, licenci a provenienci schématu.
- [ ] Napsat `test_every_route_is_classified`, `test_every_parameter_is_accounted_for`, `test_operation_ids_are_unique`, `test_catalog_generation_is_deterministic`.
- [ ] Projít routy, handlery, autentizační dekorátory a testy upstreamu; doplnit nedokumentované operace, parametry a důvody vyřazení nevolatelných rout.
- [ ] Ručně implementovat kontrolované doplňky k nepřevoditelným částem OpenAPI; nesnižovat schéma na libovolný dictionary.
- [ ] Ověřit kontrakt testy a `uv run python scripts/catalog.py check`; v coverage dokumentu uvést počty a jmenovité mezery. Samotná klasifikace není implementace.
- [ ] Commit `feat: inventory Zulip API with source provenance`; push a PR.

## Úloha 3 — Validovaný executor a stdio MCP

**PR:** `codex/api-executor`; `executor.py`, `server.py`, unit a contract testy.

**Rozhraní:** `execute(principal: Principal, operation_id: str, arguments: dict) -> OperationResult`; `Principal` je neměnný kontext identity vytvořený konfigurací/autentizací, ne argumentem od modelu. `OperationResult` nese data nebo typovanou chybu včetně informace o nejistém výsledku zápisu. Veřejné MCP nástroje mají jména a parametry ze spec.

- [ ] Napsat testy `test_wire_encoding_for_each_operation`, `test_unknown_arguments_are_rejected`, `test_read_retries_respect_retry_after`, `test_write_timeout_is_not_retried`, `test_redirect_does_not_leak_credentials`.
- [ ] Implementovat správné path/query/form/JSON serializace a upstream chyby bez ztráty detailu potřebného klientovi; validovat cílový origin a velikost odpovědi.
- [x] Přidat katalogové nástroje a MCP anotace pro čtení/zápis/destruktivitu; anotace nejsou autorizační kontrola.
- [x] Přidat procesový test skutečného stdio initialize/list/call; stdout pouze MCP, logy na stderr bez secretů.
- [ ] Ověřit unit/contract testy a CLI start; zdokumentovat konfiguraci a příklady. Commit `feat: execute catalog operations over stdio`; push a PR.

## Úloha 4 — Více uživatelů, tokeny a HTTP autorizace

**PR:** `codex/http-identities`; `auth.py`, `store.py`, `cli.py`, HTTP část serveru, migrační a auth testy.

**Rozhraní:** `authenticate(bearer: str) -> Principal`; CLI `identity add|update|remove`, `token issue|revoke`, `secrets rotate`. Každý autentizovaný request si načte platnou identitu; session ID samo neopravňuje k přístupu.

- [ ] Napsat `test_two_users_never_share_credentials`, `test_revoked_token_is_rejected`, `test_wrong_issuer_audience_expiry_is_rejected`, `test_unknown_oauth_subject_has_no_zulip_access`, `test_secret_rotation_preserves_access`.
- [x] Implementovat SQLite s verzovanou migrací, šifrovanými Zulip secrets a hashovanými MCP tokeny. Šifrovací klíč je mimo DB; záloha a obnova DB+klíče se dokumentuje bez ukládání klíče do Git.
- [x] Přidat oddělené ruční tokeny a OAuth resource server přes ověřenou knihovnu/SDK; discovery a WWW-Authenticate podle připnuté podporované MCP specifikace.
- [x] Připravit konfiguraci externího Keycloak testovacího realm a protokolové testy validních/nevalidních access tokenů; žádný vlastní frontend a žádné spuštění prohlížeče.
- [ ] Přidat adapter pro jinou autentizaci jen tam, kde ji doložil audit, včetně CSRF, expirace a shody identity u session credentials.
- [x] Ověřit souběžné HTTP klienty, ukončení session a odvolání tokenu během provozu. Popsat provisioning, rotaci a rozdíly identity/role.
- [ ] Commit `feat: isolate users and authorize HTTP clients`; push a PR.

**Partial evidence:** SQLite schema v1, Fernet-encrypted identities, expiring hashed
manual tokens, exact OAuth mappings, offline atomic key rotation and provisioning
CLI are implemented. HTTP request authentication, signed JWT validation and
resource discovery have synthetic protocol tests. A real Keycloak 26.8 fixture
now covers PKCE and two official SDK network clients. Alternate upstream auth and
actual Zulip integration remain pending; Task 4 is not complete.

## Úloha 5 — Binární operace a zbývající aplikační endpointy

**PR:** `codex/full-api-operations`; `files.py`, doplňky katalogu/executoru a testy po API oblastech. Pokud je diff velký, rozdělit podle oblastí níže; každá má vlastní PR a test evidence.

**Rozhraní:** `file_handle` patří principalu; HTTP upload/download probíhá přes autentizované streamované endpointy bez UI. Stejné operace jsou dostupné z katalogu i pohodlných nástrojů.

- [ ] Napsat `test_multipart_upload_preserves_bytes`, `test_file_handle_cannot_cross_users`, `test_local_path_cannot_escape_allowed_root`, `test_size_limit_is_enforced_while_streaming`.
- [ ] Implementovat upload/download včetně obrázků, příloh a případných branding endpointů z auditu; handle expiruje po 15 minutách, odstranění při expiraci a ukončení instance.
- [ ] Dokončit operace oblastí: zprávy/témata/reakce/flags; koncepty/plánování/připomínky/snippets; kanály/složky/odběry; uživatelé/status/presence/nastavení/skupiny; pozvánky/boti/klíče; organizace/profilová pole/domény/linkifiery/emoji/exporty; hovory/push a další položky nalezené auditem.
- [ ] Pro každou operaci mít test správné HTTP metody, cesty, všech parametrů a formátu; pro speciální handler navíc pozitivní a chybový test. Operace vracející secrets testovat syntetickými daty bez logování skutečných klíčů.
- [ ] Ověřit kontrakt suite a znovu coverage kontrolu; nevydávat generickou HTTP funkci za otestované pokrytí všech operací.
- [ ] Commit(y) podle ucelených oblastí, push a PR s aktualizací matice pokrytí.

**Partial evidence:** internal FileStore and multipart executor cover owner binding,
15-minute handle expiry, bounded streaming, local path/symlink checks and seven
multipart schemas. Authenticated HTTP staging/retrieval/deletion, stdio local
import and lifecycle cleanup are implemented. Signed-URL attachment downloads
are implemented with caller authorization and bounded credential-free transfers.
Live Zulip/storage verification and the remaining API audit are pending; Task 5
is not complete.

## Úloha 6 — Události a praktické pracovní nástroje

**PR:** `codex/events-workflows`; `events.py`, `workflows.py`, odpovídající testy a příklady.

**Rozhraní:** Přímé nástroje volají `execute`; batch maximálně 50 položek, sekvenčně a stop-on-error; historie používá cursor, nejvýše 200 zpráv na stránku, žádné skryté neomezené cykly.

- [ ] Napsat `test_queue_is_principal_scoped`, `test_queue_expiry_is_explicit`, `test_cancel_releases_wait`, `test_batch_reports_partial_success`, `test_history_cursor_does_not_skip_messages`.
- [ ] Implementovat queue lifecycle a čekání maximálně 25 sekund na jedno volání; připojení po výpadku nesmí předstírat úplnou historii. Klient dostane informaci o nutnosti synchronizace.
- [ ] Přidat pohodlné nástroje pro fulltext, historii, zprávy, přesun/resolve tématu a soubory bez duplikace autorizace a validace.
- [ ] Přidat batch s explicitními dílčími výsledky, žádný rollback nebo neověřená idempotence. Změny více uživatelů používají jen upstream dostupné operace a oprávnění volajícího.
- [ ] Ověřit testy a ukázkové scénáře proti testovacímu Zulipu; commit `feat: add event handling and bounded workflows`; push a PR.

**Partial evidence:** caller-owned managed queues expose registration, explicit
cursor acknowledgements, 25-second polling, expiration/resync reporting,
cancellation and bounded shutdown. Unit/SDK/HTTP fixture tests cover identity
isolation and wire encoding. Reference listen/session behavior, workflows and live
Zulip integration remain pending; Task 6 is not complete.

**Additional partial evidence:** `zulip_batch` runs up to 50 catalog operations,
refreshes caller access for each, stops on error and reports partial results.
`zulip_history` reads one page up to 200 messages using exclusive ID cursors.
Unit, executor-wire, stdio and authenticated HTTP fixtures cover pagination,
identity selection and token revocation during a batch. Live Zulip workflow
acceptance and the remaining convenience tools are still pending.

Message tool progress: dedicated read/send/DM/edit/reaction/move/resolve tools
use the shared executor. Move and resolve have explicit notification/propagation
behavior and channel lookup validation. Wire tests cover the basic operations;
reference normalization, configurable channel policies and full workflow parity
remain incomplete.

## Úloha 6A — Povinná úplná parita tří MCP referencí kromě Hermes

**Doplněný požadavek uživatele:** výsledný projekt musí mít nejméně všechny funkce
referenčního projektu, nejen stejné REST endpointy. Závazný inventář a připnuté SHA
jsou v [zulipmcp-parity.md](../../zulipmcp-parity.md) a
[additional-mcp-parity.md](../../additional-mcp-parity.md): windborne/zulipmcp,
unfoldingWord/uw-zulip-mcp a akougkas/zulipchat-mcp. Hermes je jediná výjimka;
kompletní aplikační API je další současný požadavek.

- [ ] Doplnit všech 27 MCP nástrojů a jejich chování včetně context/reply/listen/end_session, formátování, viditelnosti, allowlistů, normalizace a souborových wrapperů.
- [ ] Dodat caller-scoped Python knihovnu pokrývající všechny veřejné schopnosti referenční knihovny, konfiguraci a hooky.
- [ ] Dodat autentizovaný legacy SSE transport vedle stdio a Streamable HTTP.
- [ ] Dodat explicitní direct-launch CLI pro Claude/Codex a mention listener pro Claude/Codex/OpenCode, překlady MCP konfigurace, životní cyklus a všechny doložené volby.
- [ ] Zachovat omezené jednotlivé event polly (25 s), přidat obnovitelné dlouhé čekání s keepalive/cancel pro referenční listen workflow; žádné sdílení stavů mezi identitami.
- [ ] Dodat sjednocení nástrojů a chování dalších dvou referencí: agentní approval/inbox/task workflows, plánování/koncepty, analytiku a fallback, policies/filtry, hosted enrollment/secret provider, povolené identity, skills/resources a export klientských balíčků.
- [ ] Pro každý řádek inventáře doložit implementaci a regresní/integrační test; ověřit také scénáře z upstream testů. Zvlášť označit skutečné provider testy versus testy příkazů.
- [ ] Aktualizovat dokumentaci, kompatibilitu, provenance/notice při případném převzetí kódu, push a draft PR po ucelených částech.

## Úloha 7 — Integrační kompatibilita a bezpečnostní review

**PR:** `codex/integration-coverage`; `tests/integration/`, `docs/compatibility.md`, bezpečnostní a coverage workflow.

- [ ] Připravit izolovaný Zulip cílové připnuté verze s běžným uživatelem, adminem, vlastníkem, botem, soukromým kanálem a další organizací. Testovací credentials se vytvářejí jen pro tento běh.
- [ ] Ověřit stdio a HTTP end-to-end; admin změny, zakázanou změnu cizího nastavení, privátní obsah, odebrání oprávnění během session, revoke tokenu a souběh uživatelů.
- [ ] Ověřit nedokumentované operace proti skutečným handlerům cílové verze, nejen mocked HTTP; běžné CRUD včetně úklidu, destruktivní lifecycle jen v oddělené jednorázové organizaci.
- [ ] U externích integrací (push/video) rozlišit otestovaný wire kontrakt a reálný provider test; uvést požadované externí služby bez nepravdivého tvrzení end-to-end ověření.
- [ ] Provést bezpečnostní review tokenů, izolace, SSRF, cest, logů, nepřímých prompt instrukcí v obsahu a dependency audit. Přidat regresní test ke každému nalezenému problému.
- [ ] Spustit celou CI sadu, aktualizovat matici: implementováno / contract-tested / integration-tested / omezení. Commit `test: verify API coverage and identity isolation`; push a PR.

## Úloha 8 — Distribuce, dokumentace a vydání

**PR:** `codex/release`; Dockerfile, release workflow, README, SECURITY, CHANGELOG, provozní a release dokumentace.

- [ ] Napsat package smoke test instalace wheel do čistého prostředí a container test stdio/HTTP; proces běží bez root, image neobsahuje secrets a testovací data.
- [ ] Dodat README s lokálním/HTTP quickstartem, konfigurací klientů a limity; CONTRIBUTING s příkazy a Git postupem; SECURITY s hlášením chyb; ADR pro katalog a autentizaci; API coverage, compatibility, token provisioning, migrace, rollback a troubleshooting.
- [ ] Dokumentovat odlišení dev/staging/production pro tento MCP a návrat k předchozímu image digestu. Před migrací záloha MCP DB, kompatibilitu downgrade ověřit; neobnovovat slepě starou binárku nad nekompatibilním schématem.
- [ ] Nastavit release pipeline níže a ověřit prerelease v čistém testovacím prostředí. Publikační cíle a licenci doplnit až podle skutečného vlastníka projektu.
- [ ] Zkontrolovat v1.0 gate, vydat changelog a tag pouze ze schváleného commitu main. Produkční nasazení je následný explicitní krok.
- [ ] Commit `build: prepare documented versioned releases`; push a PR.

## GitHub Actions a repository pravidla

| Workflow | Spouštění | Kontroly a oprávnění |
|---|---|---|
| `ci.yml` | PR a push do main | Ruff, format-check, mypy, unit/contract tests, determinismus katalogu, wheel/sdist smoke test; Python 3.12 a aktuální stabilní kompatibilní Python připnutý při bootstrapu. Required job `ci-gate` agreguje výsledky. |
| `integration.yml` | PR, main, ruční spuštění | Izolovaný Zulip a protokolový OAuth fixture, HTTP/stdio, identity, migrations a bezpečné lifecycle testy. Žádné produkční secrets; required `integration-gate`. |
| `security.yml` | PR, main a týdně | Dependency audit, secret scan a SAST; required `security-gate`. Případná výjimka má důvod, vlastníka a expiraci. |
| `upstream-drift.yml` | Týdně a ručně | Porovnání nového schématu/rout vůči pinům; report jako artifact. Nemění main ani automaticky nevydává nové operace. |
| `release.yml` | Tag `v*` | Ověřit verzi a commit na main, znovu gate, sestavit balíček/image, SBOM a provenance; publikovat přes chráněné prostředí a OIDC, kde je podporované. |

Actions připnout na plné commit SHA, nástroje/images na ověřené verze/digesty. Workflow default `permissions: contents: read`; zápis/package/id-token práva jen konkrétním release jobům. Nepoužívat `pull_request_target` pro spuštění kódu PR. Fork PR nemá přístup k secrets. Nepřenášet neověřené PR artefakty do privilegovaného release jobu. Dependabot navrhuje aktualizace dependencies a Actions přes PR. Rušení starších běhů concurrency jen pro CI, ne uprostřed publikace release.

Při nedostupnosti required checku se PR nemerguje s tvrzením, že je ověřený. Staging může používat `0.x`/`1.0.0-rc.N`; plné `1.0.0` až po splnění gate.

## Podmínky pro v1.0

- [ ] Úplná funkční parita se všemi třemi připnutými MCP referencemi kromě Hermes podle `docs/zulipmcp-parity.md` a `docs/additional-mcp-parity.md`, doložená po jednotlivých funkcích.
- [ ] Kompletní audit produkčních aplikačních rout a parametrů cílového Zulipu, veřejných i nedokumentovaných, bez nevysvětlených mezer.
- [ ] Všechny in-scope operace implementované; 100 % položek má serializační/kontrakt test. Coverage není jen řádkové procento Pythonu.
- [ ] Integračně ověřené autentizační a transportní cesty, izolace identit, hlavní CRUD/lifecycle a všechny vlastní nedokumentované adaptéry.
- [ ] Publikovaný pravdivý seznam omezení verze, role a externích providerů; žádné neověřené tvrzení, že admin může libovolně měnit cizí účet.
- [ ] Zelené required checks, uzavřené review, čistá instalace balíčku/containeru a zdokumentovaná rotace secrets/migrace/rollback.
- [ ] Určená licence, repo owner, publikační cíle a schválené vydání. Žádné automatické nasazení do produkce.

## Review a další krok

Dokumenty jsou připravené k review. Před implementací potvrdit návrhové volby Python/external OAuth a určit cílový GitHub repozitář. Doporučené provedení je postupně v této relaci po uvedených PR; subagenti pouze po výslovném výběru uživatele. U každého kroku aktualizovat checkboxy a skutečnou test evidence, neplést plán s dokončenou prací.
