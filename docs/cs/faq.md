# Časté otázky a řešení potíží

[← README](../../README.cs.md) · [English](../faq.md)

**Worker se hlásí jako offline.**
Zkontrolujte, že worker dosáhne na adresu Manageru (`MANAGER_URL` u Linuxu, adresa v okně Windows workeru) a že port **8383** neblokuje firewall. Podívejte se do logu workeru – **Živé logy** v Manageru, `docker logs <kontejner>` u Linux workeru nebo log v okně Windows workeru.

**Nový worker nedostává žádné úlohy.**
Pokud je zapnuté přihlášení (a s ním schvalování workerů), čeká na vás – viz oranžový štítek v hlavičce nebo horní část záložky **Workery**. Zkontrolujte také, že má zapnutý **příjem úloh** a volné sloty.

**Windows worker nemá přístup ke složkám na NASu.**
Zadejte SMB účet v **Nastavení → Úložiště a síť** (*Nastavit / Změnit SMB účet → Uložit a otestovat*, případně *Otestovat SMB & Uložit cesty*). Složky se zadávají jako UNC cesty, např. `\\nas\video\inputs`. Worker bez přístupu k NASu funguje i tak přes HTTP relay Manageru, jen pomaleji.

**Windows SmartScreen varuje před `HBWorker.exe`.**
Balíček z **vašeho Manageru** (*+ Přidat worker*) je podepsaný vlastním (self-signed) certifikátem vašeho Manageru. Protože tento certifikát není ve Windows ve výchozím stavu v důvěryhodných kořenových certifikátech, SmartScreen může u `.zip` staženého prohlížečem stále varovat (*Další informace → Přesto spustit*, nebo před rozbalením u `.zip` přes *Vlastnosti → Odblokovat*). Jednořádková instalace přes `cmd` se tomuto varování prohlížeče vyhne automaticky; případně můžete certifikát `app\maseklab_codesign.cer` naimportovat do důvěryhodných certifikátů Windows.

**Nastavení → Zabezpečení píše, že Manager nevidí skutečné adresy zařízení.**
Manager běží v bridge síti na systému, který adresy klientů skrývá za bránou Dockeru. Spusťte ho s `network_mode: host` – viz [Zabezpečení](security.md#přístup-zvenku--doporučené-nastavení).

**Aktualizace se „ještě připravuje“.**
Nová verze Manageru je vydaná, ale obraz workeru stejné verze zatím ne. Manager aktualizaci nabídne, jakmile budou oba; nemusíte nic dělat.

**Zapomněl jsem heslo do webu.**
Ve složce `/data` Manageru vytvořte prázdný soubor `reset_admin_password` a počkejte 10 sekund – přihlášení se vypne.

**Kde najdu logy?**
Na záložce **Živé logy** (Manager i všechny workery). Na disku: `cluster_manager.log` v `/data` Manageru, `worker.log` v `/data` Linux workeru.

**Jde to i bez NASu?**
Ano. Workery bez přístupu ke sdíleným složkám přenášejí zdroj i výsledek přes Manager (HTTP relay).

**Po zapnutí přihlášení přestal fungovat widget v Homepage / Homarr.**
`GET /api/stats` je čitelný bez přihlášení, pokud jste v **Nastavení → Zabezpečení** nevypnuli *Souhrn stavu pro dashboardy bez přihlášení*.

**Worker píše „Jiný Manager – nic nepřijímám“.**
Worker je navázaný na jiné ID Manageru, než má Manager, který teď odpovídá na jeho adrese (např. po přeinstalaci Manageru s prázdným `/data`). Je-li tento Manager ten správný, navažte worker znovu – viz [Workery zůstávají u svého Manageru](security.md#workery-zůstávají-u-svého-manageru).

**Hotový soubor má v názvu `[ID 1234]`.**
V Outputs už byl soubor stejného jména. HB Cluster existující soubor nikdy nepřepíše, nový proto dostal do názvu ID úlohy; skutečné jméno je v logu i v **Dokončených**.

**Jak obnovím databázi ze zálohy?**
Manager drží denní zálohy (posledních 7) a zálohy před každou aktualizací (poslední 3) ve složce `/data/backup` – stáhnout jdou i v **Nastavení → O aplikaci a aktualizace → Zálohy databáze**. Obnova: zastavte kontejner Manageru, ve složce `/data` smažte `cluster.db`, `cluster.db-wal` a `cluster.db-shm`, nakopírujte tam vybranou zálohu jako `cluster.db` a Manager znovu spusťte.
