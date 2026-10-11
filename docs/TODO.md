# ThinkHome Zulip MCP — zbývající práce

Aktualizováno: 2026-10-11. Pracovní checklist pro pokračování projektu.
Nezaškrtnutá položka může být částečně implementovaná; znamená, že ještě
nesplňuje všechny podmínky dokončení. Počet testů není procento pokrytí funkcí.

Rozsah: všechny aplikační API operace připnutého Zulipu, veřejné i
nedokumentované, a všechny přijaté funkce tří referenčních MCP kromě Hermes.
Instalace a údržba serveru Zulipu, SSH, Django shell a přímé změny jeho DB jsou
mimo rozsah. Žádný vlastní frontend; prohlížeč jen na výslovný pokyn.

Podklady: [implementační plán](superpowers/plans/2026-10-11-thinkhome-zulip-mcp.md),
[návrh](superpowers/specs/2026-10-11-thinkhome-zulip-mcp-design.md),
[API coverage](api-coverage.md), [windborne parita](zulipmcp-parity.md),
[další MCP parita](additional-mcp-parity.md).
Tento checklist původní rozsah nenahrazuje ani nezmenšuje.

## 1. Bezprostřední pořadí

- [x] Dokončit již spuštěnou úplnou regresi kanálové větve; zaznamenat konečný výsledek, případné chyby opravit. Nerozbíhat duplicitní běh.
- [x] Doplnit skutečnou test evidence do kanálové dokumentace a připraveného PR popisu.
- [ ] Commit a push `codex/channel-management`, draft PR nad `codex/search-context`, připojit PR k chatu a zkontrolovat CI.
- [ ] Selektivně zapojit připravenou opravu vazby identity v `search.py` a `multi_history.py`, přidat regresní testy a dokumentaci. Nekopírovat celé dočasné balíčky přes aktuální zdroj.
- [ ] Dokončit koncepty a plánované zprávy podle bodu 3; připravené opravy schémat zatím nejsou dokončená funkcionalita.
- [ ] Sjednotit historické stavové odstavce plánu a návrhu se skutečností; zachovat historii a rozlišit původní návrh od později přijatých agentních integrací.

Evidence prvních dvou kroků: `uv run pytest` — 786 passed in 871.57s;
[kanálový audit](channel-api-audit.md#test-evidence-map), [dokumentace](channels.md).
Jde o lokální/mock důkaz, ne o živý Zulip.

## 2. Úplnost API — hlavní podmínka v1.0

- [ ] Dokončit audit připnuté stabilní verze a samostatného SHA upstream main; evidovat licence a provenienci.
- [ ] Klasifikovat každou nalezenou routu a HTTP metodu podle handleru, autentizace a účelu. Výchozí inventář obsahuje 451 rout a 187 schématových operací; tyto počty nejsou důkaz dokončení.
- [ ] Každé vyřazení podložit konkrétním důvodem; obtížná nebo odlišná autentizace není důvod vyřazení.
- [ ] Doplnit všechny nedokumentované endpointy, parametry, odpovědi a vedlejší účinky; evidovat feature level a podporované verze.
- [ ] Dokončit alternativní autentizační adaptéry tam, kde jsou skutečně nutné, včetně CSRF a shody identity.
- [ ] Pro každou operaci doložit metodu, cestu, path/query/form/JSON/multipart serializaci, všechny parametry, validaci a chybové odpovědi.
- [ ] Udržovat source-backed opravy schémat odděleně od upstream dat; detekovat neplatnost oprav při změně upstreamu.
- [ ] Doplnit nullable barvu odběru a připravené opravy timestampu konceptu, povinného seznamu konceptů a nullable `read_by_sender`.
- [ ] U každé položky rozlišit: inventarizovaná / implementovaná / testovaná se simulovaným serverem / ověřená na Zulipu. Odstranit všechny nevysvětlené mezery před v1.0.

## 3. Aplikační oblasti k dokončení a ověření

- [ ] Zprávy, témata, přesuny, reakce, příznaky a historie: doplnit zbývající parametry a okrajové případy podle úplného auditu.
- [ ] Koncepty: seznam, vytvoření včetně dávky, úplná editace a odstranění; příjemci, prázdné téma, timestamp a zákaz přístupu k cizím konceptům.
- [ ] Plánované zprávy: seznam, vytvoření, změna a zrušení; změny typu/příjemce, čas, `read_by_sender`, kompatibilní názvy a dispatchery.
- [ ] Trvalý lokální scheduler: životní cyklus, restart/recovery a nejisté výsledky zápisů; odlišit jej od nativního plánování Zulipu.
- [ ] Připomínky a snippets: všechny operace a parametry nalezené auditem.
- [ ] Kanály, složky, členství, odběry, notifikace a výchozí kanály: dokončit audit a reálné ověření rolí i soukromých kanálů.
- [ ] Uživatelé, osobní nastavení, status/presence a skupiny: doplnit všechny operace, včetně podporovaných změn jiným uživatelům.
- [ ] Pozvánky, boti a API klíče: kompletní lifecycle a bezpečné zacházení s výstupy obsahujícími secrets.
- [ ] Organizace, profilová pole, domény, linkifiery, emoji, branding a aplikační exporty: kompletní API pokrytí.
- [ ] Hovory, push a všechny další aplikační operace nalezené auditem.
- [ ] Soubory: dokončit všechny binární endpointy a reálné ověření upload/download, podepsaných URL, expirace a izolace handle.
- [ ] Hromadné operace a cross-post: přesné částečné výsledky, přerušení a zákaz automatického opakování nejistých zápisů.
- [ ] Události: zbývající typy, expirace, zrušení čekání, obnova po výpadku a pravdivá informace o mezerách historie.

## 4. Funkce referenčních MCP nad samotným API

- [ ] Projít každou položku obou matic parity: název, signatura, chování, konfigurace a test evidence.
- [ ] Dokončit knihovní exporty, kompatibilní aliasy/fasády, `.zuliprc`, cache a konfigurační chování.
- [ ] Agenti: registrace, relace, uzavření, vazba na téma, teleport, instance, zprávy a status.
- [ ] Agentní úlohy: lifecycle, čekání, řetězce příkazů a operace agentních kanálů/témat.
- [ ] Trvalá schránka zmínek a navazujících zpráv: event ingestion, poll/ack a odpovědi do zdrojového tématu.
- [ ] Schvalování: konkrétní request ID, oprávněný schvalovatel, approve/deny, čekání a Claude hooks; text zprávy sám není autorizace.
- [ ] Spouštění a adaptéry Claude/Codex; mention listenery Claude/Codex/OpenCode podle přijaté parity.
- [ ] Analytika: denní souhrny, analýza kanálu/týmu, reporty, volitelný Anthropic provider a strukturovaný fallback; ověřit skutečné provider volání odděleně.
- [ ] Čtyři balené skills, lokální soubory, MCP resources a podporované `skills/list` / `skills/get` včetně integrity.
- [ ] Instalační konfigurace a exporty pro Claude, Codex, OpenCode, Gemini, VS Code, Cursor, Windsurf a Antigravity; ověřit generované balíčky.

## 5. Identity, bezpečnost a provoz MCP

- [ ] Dokončit Google/OIDC provisioning, pravidla e-mailů/domén, expiraci enrollmentu, ochranu před replay a cooloff.
- [ ] OpenBao: KV konfigurace, TLS/CA, dostupnost, cache, invalidace a opětovná registrace po selhání autentizace.
- [ ] Přepínat pouze mezi operátorem předem povolenými lidskými/bot identitami konkrétního volajícího.
- [ ] Dokončit izolaci identity napříč requesty, cache, soubory, queues a dlouhými operacemi, včetně změny klíče za běhu.
- [ ] Dokončit core/extended/admin/agent profily a skrytí zakázaných nástrojů; ověřit zákaz zápisu i přes obecný executor.
- [ ] Dokončit pravidla kanálů, ignorovaných streamů, neznámých ID, soukromých kanálů/DM a počítadla blokací pro všechny operace a události.
- [ ] Doplnit legacy SSE a ověřit lifecycle všech podporovaných transportů.
- [ ] Dokončit migrace a vazby databáze na účet, health, metriky, limity požadavků/backoff, timeouty a konfigurovatelné cache.
- [ ] Dokončit audit a ochranu soukromí: žádné credentials nebo obsah zpráv v běžných logách, jasná retence a nastavení.

## 6. Důkaz funkčnosti

- [ ] Izolovaný skutečný Zulip: člen, host, administrátor, vlastník a bot; veřejné/soukromé kanály, povolené i zamítnuté operace.
- [ ] Reálné CRUD/lifecycle testy hlavních oblastí a všech vlastních nedokumentovaných adaptérů.
- [ ] End-to-end stdio/HTTP/OAuth, více současných uživatelů, revokace, změna oprávnění a rotace secrets.
- [ ] Negativní bezpečnostní testy: obejití pravidel, záměna identity, redirect/SSRF, cesty/symlinky, velikost přenosů a nedůvěryhodný obsah.
- [ ] Restart, persistence, zrušení, výpadek upstreamu, 429 a nejistý výsledek zápisu bez duplicit.
- [ ] Pro každý milník úplné relevantní kontroly podle README, nezávislé review, opravy a zaznamenaná evidence.
- [ ] Závěrečný audit celého rozsahu proti všem třem referencím a Zulip API; těsně před vydáním zkontrolovat změny upstreamu od pinů.

## 7. GitHub, dokumentace a distribuce

- [ ] Každý milník: `codex/*` větev → implementace/testy/docs → commit → push → draft PR → CI → nezávislé review. U závislých PR uvést základ a pořadí integrace.
- [ ] Ověřit ochranu main, required checks a CODEOWNERS. Autor nesmí nahrazovat nezávislé schválení vlastním souhlasem.
- [ ] Dokončit/ověřit CI, integration, security, upstream-drift a release workflows; minimální oprávnění a připnuté Actions.
- [ ] Izolovaná integrační CI bez produkčních secrets; dependency/secret/SAST kontroly a aktualizace přes PR.
- [ ] Wheel/sdist a Docker: čistá instalace, smoke test stdio/HTTP, non-root a žádné secrets v artefaktech.
- [ ] Dokončit quickstart, klientské konfigurace, API coverage, kompatibilitu, ADR, provisioning, troubleshooting, migrace a rollback MCP.
- [ ] Zachovat MIT vlastního kódu a Apache provenienci převzatých materiálů; doplnit changelog.
- [ ] Určit publikační cíle, ověřit prerelease, SBOM, provenance a publikaci přes chráněné prostředí/OIDC.
- [ ] Před v1.0 uzavřít všechny povinné mezery a review, mít zelené kontroly a získat schválení vydání. Žádný automatický merge, release nebo produkční deployment.

## Pravidlo odškrtávání

U dokončené položky připsat odkaz na kód/PR, test a jeho skutečný výsledek,
odpovídající dokumentaci a případná omezení. Mock test dokládá lokální kontrakt,
nikoli chování skutečného Zulipu. Připravená změna mimo aktivní zdroj není
dokončená implementace. Milník může mít hotový kód a přesto čekat na ověření či review.
