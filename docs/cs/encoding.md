# Kódování a funkce

[← README](../../README.cs.md) · [English](../encoding.md)

## Úlohy vznikají v HandBrake

HB Cluster si nevymýšlí vlastní nastavení kódování. Úlohy připravíte v desktopovém **HandBrake** – film po filmu s vlastním presetem, RF, ořezem, filtry, zvukovými stopami a titulky, nebo celou složku s jedním presetem – a frontu vyexportujete jako `.json`. Každá úloha si nese přesně tohle nastavení.

Frontu importujete přetažením souboru `.json` kamkoli na stránku Manageru, tlačítkem **+** nad frontou, nebo automaticky z volitelné sledované složky (`/watch_dir`). Manager umí ve frontě najít duplicitní úlohy a uklidit je.

## Vážené sloty

Každý worker má počet **slotů**; úloha zabere sloty podle cílového rozlišení – **4K = 4**, **1080p = 2**, **SD / 720p = 1**. Worker pro 4K (4 sloty) tak naráz převádí 1× 4K, 2× 1080p, 1× 1080p + 2× SD nebo 4× SD. Manager rozděluje úlohy podle pořadí ve frontě, volných slotů a volby *Upřednostnit 4K*, takže silné stroje dostanou náročné tituly.

## Kódování na procesoru do `.mkv`

HB Cluster je stavěný na softwarové kódování (**x265, x264, SVT-AV1**), kde nejvíc záleží na kvalitě vzhledem k velikosti. Po dokončení HandBrake umí volitelné dodatečné zpracování přes FFmpeg / MKVToolNix:

- **LPCM** – bezztrátově zkopírovat původní LPCM zvuk.
- **DTS-HD MA a DTS jádro** – ve výchozím stavu ponechá stopy přesně podle HandBrake (v Pre-Muxu vyextrahuje DTS jádro); volitelně sloučí duplicitní dvojici DTS jádro + DTS-HD MA téhož jazyka do jedné DTS-HD MA stopy (beta; úspora ~1,3 GB na dvouhodinový film, Plex si jádro vytáhne za běhu).
- **Čištění titulků** – analyzovat pakety titulků a odstranit prázdné nebo duplicitní stopy.
- **Vynucené titulky** – při kódování vytvořit samostatnou stopu forced a odstranit ji, pokud zůstane prázdná.
- **ASS / SSA → SRT** – pro přehrávače, které ASS neumí a vypalují ho do obrazu (např. Plex na televizi). *Ponechat ASS a přidat SRT* dá SRT na první místo a převezme původní příznaky výchozí / vynucené; *Nahradit* ponechá jen SRT.
- **Statistiky MKV** – zapsat přesné statistiky stop do hlavičky MKV (`mkvpropedit`).

**Vypalované titulky** (zaškrtnuté *Burn In* v HandBrake) se ve výchozím stavu vypálí do obrazu přesně jako v HandBrake; můžete je ale přepnout tak, aby zůstaly jako samostatná vypínatelná stopa. Úloha s těmito titulky má ve frontě štítek **Burn-in**.

Vše se nastavuje v **Nastavení → Zpracování videa**.

## Blu-ray složky a `.iso`

Zdrojem může být jeden video soubor, **Blu-ray složka (`BDMV`)** nebo **obraz `.iso`**. Worker čte vybraný playlist (`.MPLS`). Se **Smart Pre-Mux** (beta, ve výchozím stavu vypnuté) worker nejdřív vyjme vybraný playlist a jeho informace o klipech (z `.iso` přes 7-Zip), spojí je přes `mkvmerge` na svém lokálním disku a teprve výsledek převede.

## Úložiště: cesty na NAS a HTTP relay

- Workery s přístupem ke sdíleným `/inputs` a `/outputs` čtou zdroj přímo z NASu, převádějí na rychlém lokálním disku (temp) a hotový soubor nahrají zpět.
- Worker bez přístupu k NASu (např. notebook jinde) posílá zdroj i výsledek **přes Manager (HTTP relay)** – bez dalšího nastavování.
- Když je lokální temp na úlohu malý, worker čte nebo zapisuje přímo přes síť.
- **Ochrana místa** – když na výstupním disku klesne volné místo pod minimum, workery si hotové filmy nechají u sebe (dokud mají v temp aspoň 25 GB volno) a nahrají je, jakmile místo uvolníte.

## Kódovací nástroje

Manager rozdává všem workerům **HandBrakeCLI, FFmpeg / FFprobe, MKVToolNix a 7-Zip**:

- Když je online **Linux worker**, ten nejsilnější sestaví statické nástroje pro Linux ze zdrojových kódů.
- V **čistě Windows clusteru** stáhne Manager oficiální sestavení pro Windows sám.
- Volitelně Manager hlídá nové verze nástrojů a aktualizuje je automaticky (**Nastavení → Nástroje**).

## Fronta, monitoring a statistiky

- Řazení přetažením, posun na začátek / konec, odložení, vrácení do fronty, zrušení – u nevratných akcí s potvrzením dvojím klepnutím.
- **Odhad doby** pro celou frontu, živé FPS a průběh, živé logy Manageru i každého workeru.
- **Statistiky**, které přežijí i promazání historie: celková úspora místa a rychlostní profily každého workeru podle rozlišení, kodéru, presetu, RF a datového toku.
- Souhrn `GET /api/stats` (online workery, běžící / čekající úlohy) pro dashboardy jako Homepage, Homarr nebo Dashy.
