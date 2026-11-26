# Zabezpečení a přístup zvenku

[← README](../../README.cs.md) · [English](../security.md)

Všechno zde je **volitelné a ve výchozím stavu vypnuté** – domácí cluster v důvěryhodné síti funguje i bez toho. Počítejte ale s tím, že bez přihlášení může kdokoli ve vaší síti ovládat Manager, přečíst uložené SMB/NAS údaje určené pro Windows workery i nahrát vlastní nástroje v **Nastavení → Nástroje**. Jakmile uložíte SMB údaje nebo sdílíte síť s cizími zařízeními, doporučujeme zapnout přihlášení (případně s volbou *Z domácí sítě bez přihlášení*). Vše se nastavuje v **Nastavení → Zabezpečení**; přehled nahoře ukazuje přihlášení, schvalování workerů a čekající žádosti na jeden pohled.

## Přihlášení do webu

- Heslo do webového rozhraní, uložené jen jako otisk **scrypt**.
- Přihlašovací stránku umí vyplnit správce hesel v prohlížeči. **Zapamatovat toto zařízení** drží přihlášení rok; jinak do zavření prohlížeče (nejvýš 12 hodin nečinnosti).
- **Přihlášená zařízení** – prohlížeč, systém, adresa a poslední použití; odhlásit jde jedno zařízení nebo všechna ostatní.
- Po **5 chybných heslech** z jedné adresy se další pokusy pozdrží (1 → 5 → 15 minut).
- **Zapomenuté heslo?** Ve složce `/data` Manageru vytvořte prázdný soubor `reset_admin_password` – do 10 sekund se přihlášení vypne a heslo (i passkeys) se smaže.
- Se zapnutím přihlášení se zapne i **schvalování workerů** (níže); vypnutím se vypne obojí.

## Passkeys

Přihlášení **otiskem prstu, obličejem nebo bezpečnostním klíčem** místo hesla (*Přidat passkey pro toto zařízení*). Passkeys potřebují **HTTPS** a patří k názvu domény, takže se split DNS funguje stejný passkey doma i venku. Přes holou IP adresu je k dispozici jen heslo. Heslo funguje vždy dál.

## Schvalování workerů

Každý worker si při prvním spuštění vytvoří vlastní náhodný klíč (`worker_identity.json`) a posílá ho s každým požadavkem – nic se ručně nekopíruje.

- Schvalování workerů je **součástí přihlášení do webu**: zapíná a vypíná se spolu s ním a samostatný přepínač nemá.
- Neznámý worker **nedostane úlohy, nastavení ani SMB údaje**, dokud ho neschválíte. Workery, které Manager zná ve chvíli zapnutí přihlášení, se schválí samy a dalších 15 minut se schvalují i nové.
- Žádosti se objeví jako oranžový štítek v hlavičce a nahoře na záložce **Workery**: **Schválit**, **Odmítnout na 24 h**, **Ignorovat trvale**.
- **Schvalovat automaticky 15 minut** – při přidávání více strojů najednou.
- Windows worker z **+ Přidat worker** jde **předschválit na 1 hodinu**; odkaz ke stažení i instalační příkaz také platí hodinu.
- Aktualizace zůstávají dostupné všem workerům, takže se aktualizuje i starší worker.
- Sestavené nástroje smí worker nahrát jen během systémového sestavení, které mu Manager přidělil, a jen nástroje z tohoto sestavení. Ruční nahrání v **Nastavení → Nástroje → Pokročilé** je pak dostupné jen po přihlášení.

## Workery zůstávají u svého Manageru

Každý Manager má náhodné ID (`/data/manager_identity.json`) a posílá ho v každé odpovědi. Worker si ho při prvním spojení zapamatuje (`manager_binding.json` vedle `worker_identity.json`) a pak **od Manageru s jiným ID nic nepřijme** – žádné úlohy, nastavení ani aktualizace. Chrání to proti falešnému Manageru (např. podvržené odpovědi na *Najít*) i proti překlepu v adrese. Adresa se měnit může (nová IP, doména) – rozhoduje jen ID.

- Takový worker ukáže ve svém okně **Jiný Manager – nic nepřijímám** a na záložce **Workery** druhého Manageru se objeví v části *Navázané na jiný Manager*.
- Znovu navázat (např. po přeinstalaci Manageru s prázdným `/data`): ve Windows workeru **Možnosti → Navázat na tento Manager** (s potvrzením); u Linux workeru smažte `/data/manager_binding.json`, nebo ho jednou spusťte s `HB_MANAGER_REBIND=1` a proměnnou pak odstraňte.
- Složku `/data` Manageru zálohujte – s ní ID přežije přeinstalaci i aktualizace.

## Ochrana před cizími weby

Manager odmítne požadavky na své API, které ve vašem prohlížeči spustí jiná webová stránka (prohlížeč je sám označí hlavičkou `Sec-Fetch-Site`). Škodlivá stránka, kterou otevřete, proto Manager neovládne – ani bez přihlášení. Workerů, skriptů ani `curl` se to netýká; odkazy na balíček Windows workeru, aktualizace a `/api/stats` fungují odkudkoli.

## Přístup zvenku – doporučené nastavení

1. **Spusťte Manager s `network_mode: host`.** V běžné bridge síti na některých systémech (např. NAS jako Synology, Docker Desktop, rootless Docker / Podman, připojení přes IPv6) vidí Manager místo skutečné adresy klienta jen bránu Dockeru. Manager to pozná; stránka Zabezpečení to vysvětlí a volby domácí sítě a reverse proxy nechá zamčené.
2. **Před Manager dejte HTTPS** – reverse proxy s certifikátem (Nginx Proxy Manager, Caddy, Traefik, nginx) nebo **Cloudflare Tunnel** (volitelně s Cloudflare Access / Zero Trust).
3. **Za reverse proxy** zapněte **Používám reverse proxy** a zadejte adresu stroje, na kterém proxy běží (**ne** Manageru). Manager pak použije skutečnou adresu klienta, kterou proxy posílá (`X-Forwarded-For`) – jen od této adresy, takže ji nikdo nepodvrhne. Výsledek ověříte v řádku *Vaše adresa, jak ji vidí Manager*.
4. **Z domácí sítě bez přihlášení** (volitelné) – požadavky z vašich domácích sítí se otevřou bez hesla. Požadavky přes Cloudflare (`CF-Connecting-IP`) chtějí přihlášení vždy, stejně jako požadavky z neznámé proxy.

## Docker socket (`/var/run/docker.sock`)

Připojení `/var/run/docker.sock` do Manageru nebo Linux workerů je **volitelné** a slouží výhradně pro aktualizace na jedno kliknutí / automatické aktualizace (stažení nového obrazu a přetvoření kontejneru bez přerušení běžících úloh). Protože přístup k Docker socketu představuje zvýšené oprávnění na hostiteli, můžete tento mount vynechat a kontejnery aktualizovat ručně (`docker compose pull && docker compose up -d`).

## Dashboardy

`GET /api/stats` vrací jen počty (online workery, běžící / čekající úlohy) a zůstává čitelný bez hesla, takže widgety v **Homepage**, **Homarr** nebo **Dashy** fungují dál. Vypnete to volbou **Souhrn stavu pro dashboardy bez přihlášení**.
