# ThinkHome Zulip MCP v1.0 návrh

Stav: návrh k review, bez implementace a nasazení.

## Cíl a přijaté požadavky

Univerzální MCP server pro libovolnou organizaci Zulipu. ThinkHome je první zamýšlené nasazení. Pokrytí zahrnuje veřejné i nedokumentované aplikační HTTP API, včetně operací nezpřístupněných běžným administračním UI. Pokrytí se prokazuje inventářem endpointů a testy, nikoli počtem nástrojů.

- Základ tvoří oficiální OpenAPI schéma, doplněné ručně ověřenými definicemi a praktickými nástroji.
- Lokální stdio a vzdálené Streamable HTTP sdílejí katalog a provádění operací.
- Vzdálený server podporuje více uživatelů. Každý používá svou Zulip identitu a její aktuální oprávnění; žádné automatické přepnutí na společného administrátora.
- Přístup přes ručně vydané MCP tokeny i OAuth. Žádný vlastní frontend, React aplikace ani práce s Build Web Apps. OAuth přihlášení obsluhuje externí autorizační server.
- Ovládání prohlížeče výhradně na výslovný pokyn uživatele. Protokolové testy mohou běžet bez prohlížeče.
- Mimo rozsah: instalace a aktualizace Zulipu, obnova záloh, SSH, Django shell, přímý zápis do databáze Zulipu, provoz AI agentů.
- API operace jako aplikační exporty, konfigurace organizace nebo deaktivace organizace do rozsahu patří; nejde o údržbu hostitelského serveru.

## Navržené technické volby

Python 3.12 jako minimální verze, balíčkování přes uv a pyproject.toml; oficiální MCP Python SDK, httpx, Pydantic, PyYAML a jsonschema. Pro šifrování použít cryptography a pro validaci JWT standardní knihovnu PyJWT s kryptografickou podporou. Verze závislostí vybrat z aktuálních stabilních vydání při bootstrapu a uložit přesně v uv.lock. Žádný vlastní kryptografický ani OAuth autorizační server.

Jeden Python proces, jedna sdílená implementace operací, žádné mikroslužby, Redis ani fronta úkolů. SQLite ukládá vazby identit, hashované ruční tokeny a šifrované Zulip přihlašovací údaje; obsah zpráv se nearchivuje. První vydání podporuje jednu repliku HTTP serveru s persistentním svazkem. Lokální stdio může pracovat bez databáze s nakonfigurovanou jedinou identitou.

## Hranice a měření pokrytí

První audit připne nejnovější stabilní Zulip release dostupný v době auditu přes tag i commit SHA a zaznamená zvlášť konkrétní SHA upstream main. Stabilní release je povinný integrační cíl v1.0; main slouží k odhalení změn, nikoli k neřízenému rozšiřování produkčního katalogu.

Inventář vznikne porovnáním OpenAPI, routování a handlerů zdrojového kódu. Každá kombinace HTTP metody a cesty má operation_id, zdroj/commit, stabilitu, autentizaci, požadovanou roli či policy, parametry, formát těla, typ odpovědi, verzi/feature level, vedlejší účinky, test a stav implementace. Dynamické routy a nesrovnalosti mají explicitní ruční kontrolu; regex není důkaz úplnosti.

Inventář oddělí volatelné operace od vstupních webhooků, přihlašovacích callbacků, HTML stránek a vývojových rout. Každé vyřazení musí mít konkrétní důvod a důkaz z kódu. Rozdílná autentizace nebo obtížná implementace není důvod k tichému vyřazení aplikační operace.

Oficiální schéma nemusí popisovat všechny endpointy nebo všechny parametry. Doplňky budou v samostatném katalogu s odkazy na handlery. Nové aplikační endpointy nalezené při auditu rozšíří implementaci před vydáním. Nevysvětlená mezera blokuje tvrzení o úplném pokrytí.

Release v1.0 vyžaduje všechny produkční volatelné aplikační operace cílové verze implementované včetně parametrů a autentizace. Pokud endpoint vyžaduje autentizaci nebo stav, který neumíme dodat, musí být označen jako chybějící a plné v1.0 se nevydá. Vývojové endpointy se evidují a vystavují pouze explicitním vývojovým profilem; nepočítají se jako ověřené v produkční instanci.

## MCP rozhraní

- `zulip_search_operations(query, limit=20)` vrací malé výsledky katalogu.
- `zulip_describe_operation(operation_id)` vrací kompletní vstupní schéma, oprávnění, verzi a vedlejší účinky.
- `zulip_execute(operation_id, arguments)` provádí přesně katalogovou operaci pod identitou daného klienta. Nepřijímá libovolnou cílovou URL ani nové přihlašovací údaje.
- `zulip_capabilities()` popíše nakonfigurovanou instanci a známou kompatibilitu. Neznámé oprávnění označí jako neznámé; skutečné rozhodnutí dělá Zulip při volání.
- Přímé běžné nástroje: hledání zpráv, načtení historie tématu, odeslání/úprava/přesun zprávy, vyřešení tématu a přenos souboru. Používají stejný validátor a executor jako katalog.
- `zulip_batch(operations)` provádí nejvýše 50 operací postupně; výchozí chování je zastavení při první chybě. Vrací výsledky provedených, chybu a neprovedené položky. Žádné tvrzení o transakci nebo automatickém rollbacku.
- Výstupy obsahují strukturovaná data a stručný text. Stránkování je explicitní a omezené; žádné neomezené načítání historie do kontextu modelu.
- Registrace/čtení/rušení event queues přes katalog, plus pohodlné čekání s omezeným timeoutem a zrušením. Queue patří jednomu uživateli; expirovaná queue nesmí způsobit tichou ztrátu událostí.

## Autentizace a ochrana dat

Ruční MCP token je náhodný, uložený pouze jako hash, odvolatelný a s expirací. CLI obsluhuje založení identity, nastavení tajných údajů, vydání/odvolání tokenu a rotaci šifrovacího klíče. Tajné údaje přijímá skrytým vstupem nebo souborem secretu, nikoli argumentem shellu nebo běžným MCP tool výstupem.

OAuth funguje jako MCP resource server s důvěryhodným externím poskytovatelem: discovery, issuer, audience/resource, podpis, expirace a scopes se validují. Vazba na uživatele používá `(issuer, subject)`, nikoli neověřený e-mail. První podporovaný ověřovací provider bude Keycloak přes zdokumentovaný testovací realm a předregistrovaného veřejného klienta s PKCE. Nevytváříme vlastní přihlašovací stránku. Provozní výběr poskytovatele ani veřejné nasazení nejsou součástí tohoto návrhu dokumentace.

Obě autentizační cesty se mapují na tentýž interní Principal. MCP token nikdy není Zulip API klíčem a nepředává se upstreamu. HTTP profil má pevnou povolenou Zulip URL a identitu nastavenou správcem. Pokud audit prokáže nezbytnost Zulip session autentizace, přidá se odděleně provisionovaný session secret s CSRF podporou a kontrolou shody uživatele; žádné získávání cizí identity nebo automatizace prohlížeče.

Izolovat klienty, cookies, cache, soubory a event queues podle principalu a organizace. Neukládat tajné údaje, obsah zpráv nebo celé request/response payloady do logů. Explicitní API operace vracející tajné údaje musí mít definovaný výstup pro oprávněného volajícího; nelze je potichu rozbít globální redakcí, ale jejich výstupy nesmějí do logů ani běžných fixtures.

Síť: HTTPS v produkci, povolené originy/hosty, žádné libovolné URL fetch, redirect nesmí přenést Zulip credentials na jiný origin. Self-hosted privátní adresy jsou možné jako explicitně nakonfigurovaná instance, nikdy odvozené z nedůvěryhodného argumentu nástroje.

Soubory: stdio používá explicitní povolený adresář; HTTP nemá obecný přístup k filesystemu hostitele. Vzdálený přenos používá omezené autentizované upload/download endpointy s náhodnými handle vázanými na uživatele a expirací. Výchozí limit přenosu 25 MiB, konfigurovatelný a vždy omezený také limitem Zulipu. Upload je streamovaný; žádné libovolné cesty nebo shell.

Retry: respektovat 429 a Retry-After, omezené retry pouze pro prokazatelně bezpečné čtení. Neopakovat automaticky zápisy po nejistém timeoutu; vrátit stav nejistého výsledku a identifikátor požadavku.

## Vývoj a produkce

Chráněná main, krátké větve codex/<tema>, malé commity a PR s dokumentací a testy. Vývojové a testovací Zulip instance mají jiné účty, secrets, databáze a svazky než produkce. Destruktivní integrační testy pouze v jednorázové testovací organizaci.

CI na PR nepoužívá produkční secrets. Publikování až z verzovaného schváleného commitu na main. SemVer, CHANGELOG, immutable image digest a migrační poznámky. Release neznamená automatické nasazení do ThinkHome.

## Zdroje návrhu

- https://zulip.com/api/rest
- https://github.com/zulip/zulip/blob/main/zerver/openapi/zulip.yaml
- https://github.com/modelcontextprotocol/python-sdk
- https://modelcontextprotocol.io/specification/latest/basic/authorization
- https://docs.github.com/en/actions/reference/security/secure-use

## Předpoklady k review

Python a externí OAuth provider jsou návrhové volby pro toto vydání. Odmítnutí frontendové práce vykládáme jako zachování OAuth přes externí provider a provisionování přes CLI. Cílový GitHub owner/repository, licence, publikační účty a produkční adresa zatím nebyly určeny; neodvozovat je z názvu ThinkHome a nic nezveřejňovat bez jejich určení.
