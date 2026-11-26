# Workery

[← README](../../README.cs.md) · [English](../workers.md)

**Worker** je počítač, který převádí. Můžete kombinovat počítače a notebooky s Windows s Linux servery v Dockeru; každý worker dostává úlohy od Manageru podle svých **slotů** a nastavení.

> **Doporučení:** workery vytvářejte z Manageru – **Workery → +** (nebo 2. krok úvodního průvodce). Manager za vás vyplní svou adresu, jméno workeru, sdílené složky i SMB přihlášení.

## Windows worker

**Z Manageru (doporučeno)**
1. **Workery → +** → *Windows*: zadejte jméno workeru, sdílené složky (UNC cesty, např. `\\nas\video\inputs`) a kdy se má worker spouštět:
   - **Ručně** – poklepáním na `HBWorker.exe`, když chcete převádět.
   - **Po přihlášení** – spustí se po přihlášení do Windows a běží v oznamovací oblasti u hodin.
   - **Před přihlášením (beta)** – spustí ho Plánovač úloh hned po zapnutí počítače (pro počítač bez obsluhy); po přihlášení převezme okno v oznamovací oblasti. Při zapnutí si Windows jednou vyžádá povolení; úloha pak běží pod vaším účtem s běžnými právy (bez hesla k Windows) a k NASu se připojí údaji od Manageru. Kontrola práv v logu workeru ukazuje, jestli je vše v pořádku.
2. **Stáhněte předvyplněný `.zip`**, rozbalte ho kamkoli (např. `C:\HBWorker`) a spusťte `HBWorker.exe` – nebo zkopírujte jednořádkový instalační příkaz do **Příkazového řádku (`cmd`)**.
3. Se zapnutým schvalováním workerů nechte zaškrtnuté **Předschválit tento worker (platí 1 hodinu)** – worker spuštěný do hodiny se schválí sám.

`HBWorker.exe` podepisuje váš vlastní Manager (každý Manager si vytvoří vlastní podpisový klíč v `/data/certs/` – v tomto repozitáři žádný soukromý klíč není). Potřeba je **Python**: spouštěč najde už nainstalovaný, a jen když žádný nenajde, nainstaluje sám nejnovější Python (přes winget, jinak z python.org – spustí se jen instalátor s platným podpisem Python Software Foundation).

**Z GitHub Releases** – stáhněte [`HB-Worker-Windows.zip`](https://github.com/maseklab/hb-cluster/releases/latest/download/HB-Worker-Windows.zip), rozbalte, spusťte `HBWorker.exe` a klepněte na **Najít** – worker najde Manager v síti (UDP 8383), nebo zadejte jeho adresu.

**V okně workeru** (*⚙ Možnosti*): režim výkonu, bránění uspání (*při převodu* / *vždy* / *vypnuto* – obrazovka se může vypnout), složky a tlačítko *📂 Otevřít složku workeru*. `Uninstall_Worker.bat` worker odinstaluje.

## Linux worker (Docker)

V Manageru zvolte **Workery → +** → *Linux* a zkopírujte vygenerovaný compose (Portainer, Dockge nebo `docker compose`). Pro představu vypadá takto:

```yaml
services:
  hb-worker-1:
    image: ghcr.io/maseklab/hb-worker:latest
    container_name: hb-worker-1
    restart: unless-stopped
    environment:
      - WORKER_NAME=Linux-Worker-1             # jméno zobrazené v Manageru
      - MANAGER_URL=http://192.168.1.100:8383  # adresa vašeho Manageru
      - TZ=Europe/Prague
    volumes:
      - ./data:/data                           # temp, nástroje a log workeru (NAS/Portainer: absolutní cesta)
      - /mnt/nas/inputs:/inputs:ro
      - /mnt/nas/outputs:/outputs
      # - /mnt/nvme/hb_temp:/temp              # volitelné: samostatný rychlý disk pro dočasné soubory
      - /var/run/docker.sock:/var/run/docker.sock  # volitelné: automatické aktualizace workeru
    labels:
      - "com.centurylinklabs.watchtower.enable=false"
```

**Každý worker potřebuje vlastní datovou složku (`/data`).** Dva kontejnery se stejnou složkou by sdílely klíč workeru, nastavení i Temp – druhý proto 60 s počká, důvod zapíše do `docker logs` a skončí; Docker ho později zkusí spustit znovu. Pro víc workerů na jednom stroji dejte každému vlastní složku, např. `/opt/hb_worker/linux-worker-1` a `/opt/hb_worker/linux-worker-2`. Totéž platí pro samostatný disk na Temp (`/temp`): každý worker potřebuje vlastní podsložku, např. `/mnt/nvme/hb_temp/linux-worker-1` – compose z Manageru to udělá sám.

Worker uklízí jen své vlastní dočasné soubory. Když složka, kterou mu dáte jako Temp (např. `/temp` nebo vlastní složka Temp u Windows workeru), už obsahuje jiné soubory, worker si v ní založí podsložku `HB_Temp` a pracuje jen v ní – ostatní soubory zůstanou netknuté.

S namapovaným `docker.sock` se worker aktualizuje sám – stáhne nový obraz a znovu vytvoří svůj kontejner. Bez něj aktualizujte ručně: `docker compose pull && docker compose up -d`.

## Sloty, výkon a priorita

- **Sloty** – úloha zabere sloty podle cílového rozlišení: **4K = 4**, **1080p = 2**, **SD / 720p = 1**. Worker pro 4K má ve výchozím stavu 4 sloty (1× 4K, 2× 1080p, 4× SD…), worker pro 1080p 2, worker jen pro SD 1. Změníte to na kartě workeru.
- **Režim výkonu** (karta workeru v Manageru nebo okno workeru):
  - **AUTO** – na Windows ECO, když s počítačem pracujete, a FULL po 3 minutách nečinnosti; na Linuxu FULL.
  - **FULL** – všechna vlákna, snížená priorita.
  - **ECO** – méně vláken a nižší priorita; počítač zůstane použitelný.
  - **NIGHT** – FULL od 22:00 do 6:00, jinak ECO (nebo úsporná jádra).
  - **HYBRID ECO** – jen úsporná jádra u procesorů s P- a E-jádry.
  - **CUSTOM** – výběr konkrétních jader (P-jádra / E-jádra / SMT).
- **Upřednostnit 4K** – worker bere nejdřív úlohy ve 4K.
- **Příjem úloh** – worker s vypnutým příjmem dokončí rozpracované úlohy a nové už nebere. **Zastavit** jeho rozpracované úlohy hned zruší a příjem vypne.

## Aktualizace

Manager nabídne novou verzi (nebo ji sám nainstaluje), až jsou vydané obrazy Manageru i workeru stejné verze. **Windows workery** si nové soubory stáhnou z Manageru, **Linux workery** se znovu vytvoří z nového obrazu. Worker vždy dokončí rozpracovanou úlohu a aktualizuje se dřív, než si vezme další.

Před přepnutím se nová verze zkusmo načte v samostatném procesu (Linux a Manager: dočasný kontejner bez sítě a svazků). Po přepnutí musí naběhnout: Manager do 60 s odpovídat na portu 8383, Linux worker ohlásit, že běží (nebo 60 s běžet bez pádu), Windows worker do 2 minut otevřít okno. Jinak se automaticky vrátí předchozí verze (Windows ze záloh `app\*.prev`) a další automatický pokus přijde až za 6 hodin. Po úspěšné aktualizaci se smaže předchozí obraz HB, pokud ho nepoužívá jiný kontejner.

Když se aktualizace nepovede z jiného důvodu (žádný novější obraz, chyba stažení), další pokus přijde za 5–15 minut; po druhém neúspěchu stejné verze až za 6 hodin. **Aktualizovat teď** na kartě workeru čekání přeskočí (pracující worker nejdřív dokončí převody). Ruční aktualizace Manageru v Nastavení → O aplikaci a aktualizace funguje také hned.

Linux worker stahuje obrazy jen ze svého kanálu (`:latest` nebo `:dev`) a sám ho nikdy nepřepne. Když se jeho kanál liší od Manageru, karta workeru ukáže **Jiný kanál než Manager**; pro přepnutí změňte tag obrazu ve svém stacku. Používejte `:latest` (stabilní vydání); `:dev` se sestavuje z každé změny ve větvi `dev` a je jen na testování.
