# Changelog
All notable changes to HB Cluster (HB Manager and HB Workers) are documented in this file. The format is based on Keep a Changelog and the project uses Semantic Versioning.

## [2.12.1] - 2026-11-27
### Changed
- **First-run wizard security:** Web sign-in and worker approval are now recommended and enabled by default in Step 1 of the onboarding wizard, with a single toggle to turn them off for a trusted home LAN (and still configurable anytime in Settings → Security).
- **Documentation & repository:** Local SVG README badges, clarified queue distribution vs. planned Turbo mode, clarified that `/var/run/docker.sock` is optional (recommended for zero-interruption self-updates), enabled GitHub Sponsors, and added structured GitHub issue templates.

## [2.12.0] - 2026-11-26
The biggest update since 2.11: optional web sign-in with worker approval, safe self-updates with automatic rollback, a Windows worker that starts before you sign in to Windows, and a reworked web interface for desktop and mobile.

### Highlights
- **Web sign-in (optional):** password, remembered devices and passkeys (fingerprint or face over HTTPS), optional access without sign-in from your home network, and support for reverse proxies and Cloudflare Tunnel.
- **Worker approval:** a new worker gets no jobs, settings or SMB credentials until you approve it. The setup wizard can pre-approve a new worker with a one-time code, and you can open a 15-minute window that approves new workers automatically.
- **Workers stay with their Manager:** every worker binds to its Manager and ignores any other Manager, including a spoofed one on the network.
- **Safe updates:** every update is checked with SHA-256, the new version is test-started first and the previous version comes back automatically if it fails. Each worker has an "Update now" button and waits longer after repeated failed attempts.
- **Windows worker before sign-in:** starts right after the PC boots, under your own Windows account, without a stored Windows password and without administrator rights.
- **New setup wizard in 4 steps:** language and shared folders, first worker, video processing options and optional web sign-in.

### Video processing
- **DTS-HD MA and DTS core:** a new option keeps only the DTS-HD MA track when the same language also has a separate DTS core track (Plex extracts the core on the fly when needed).
- **Burned-in subtitles:** HB Cluster can follow HandBrake's burn-in choice or keep all subtitles as separate tracks.
- **Smart Pre-Mux (BETA):** more reliable playlist detection on BDMV folders and ISO images, correct pairing of several tracks in the same language, and an automatic fallback to a direct encode when Pre-Mux fails.
- **Safer post-processing:** subtitles are never removed when FFmpeg cannot read them, an unreadable output fails the stream check instead of passing it, and MP4 / M4V outputs skip Matroska-only steps.
- HandBrake settings are passed through much more faithfully: multi-pass encoding, padding, Bwdif / Decomb / Comb Detect presets, custom denoise/sharpen/deblock/detelecine, Foreign Audio Scan, external SRT/SSA subtitles, audio sample rates/mixdowns/quality, WebM/MOV outputs, and queue breakpoints.
- Audio exactly as in HandBrake: "Auto Passthru" copies only the codecs ticked in HandBrake, and a passthru track that cannot be copied from its source is encoded with the fallback encoder instead of being dropped.

### Web interface
- Redesigned queue, completed jobs, workers, monitoring and settings, with tips shown once.
- Mobile layout with bottom navigation, working Back button and gesture, and "Add to Home Screen" support.
- Two-click confirmation for every action that cannot be undone, plus keyboard and screen-reader support.
- Duplicate detection in the queue, import of several queue files at once, database backups and faster history.
- All web libraries are bundled with the Manager, so the web interface works without internet access.

### Reliability and performance
- Stays smooth with more than 1,000 jobs in the queue.
- Finished or almost finished work is no longer lost after an outage, a cancelled job stays cancelled, and a late error report can no longer send a finished movie back to the queue.
- Existing output files are never overwritten and the Manager never writes into the Inputs folder.
- The Linux worker reconnects by itself after Docker restarts and respects Docker CPU limits (`--cpus`).
- **One installation per worker:** a second copy of the Windows worker offers to open the existing one, and uninstalling an old copy leaves the autostart and shortcuts of the new one alone. Two Linux containers can no longer share one data folder, and the Manager warns when two installations report under the same worker name.
- After a self-update, Docker Compose, Arcane and Portainer see the new image of the container again (they used to show a bare `sha256` instead of the version).
- After an update the Windows worker window comes back as it was – in the tray, minimized or open – instead of always opening.
- Workers connected through Cloudflare Tunnel or WARP no longer fill the Manager log with address changes.
- The write test in the Outputs folder no longer leaves `.hb_storage_test_*.tmp` files behind; old leftovers are removed automatically.
- The Manager uses its mounted data folder whether it is mounted at `/data` or `/app/data`, and warns in the log when the data folder is not a Docker volume (the database would be lost with the next update).

### Upgrade notes (from 2.11)
- Keep `/var/run/docker.sock` mounted on the Manager and on Linux workers: it enables one-click and automatic updates that never interrupt running jobs.
- Files in the Manager's `/data` folder now have strict permissions, and secrets are readable only by the Manager.
- Linux workers on 2.11 update automatically (through `docker.sock`) and bind to your Manager after the update.
- Windows workers on 2.11 cannot update themselves to 2.12: uninstall them once (`Uninstall_Worker.bat`) and install the new package from **Workers → +**.
- If you installed a Windows worker with version 2.11.3 to 2.11.4L, remove the old "MasekLab HB Cluster" certificate from Trusted Root Certification Authorities and Trusted Publishers (run `certmgr.msc`). The current version does not need it.

## [2.11.4] - 2026-11-16
Summary of the 2.11 series (2.11.0 to 2.11.4).

### Added
- **Two Docker images:** separate `hb-manager` and `hb-worker` images on GHCR with release channels `:latest` and `:dev`.
- **Cluster updates in two phases:** the Manager updates first and workers follow once their running jobs finish. Containers update themselves through `docker.sock`.
- **Add Worker wizard:** a ready-to-run Windows package or a Docker Compose file for Linux, with shared folders and SMB credentials taken from the Manager.
- **Windows worker package:** a clean folder layout, a small native launcher `HBWorker.exe`, a first-run wizard, desktop and Start menu shortcuts and an uninstaller.
- **Windows worker settings from the web:** autostart mode and shortcuts.
- **CPU control:** custom cores and threads (P-cores, E-cores, SMT), separate ECO and HYBRID ECO power modes and weighted job slots by resolution.
- **Linux workers:** one `/data` folder for everything, with automatic migration of older layouts.
- **Central tools** (HandBrakeCLI, FFmpeg, MKVToolNix, 7-Zip) stored on the Manager and built automatically, plus a real SMB connection test.
- New README, logos and commented Docker Compose examples.

### Fixed
- Lossless DTS core extraction and audio track pairing for Blu-ray ISO images with Smart Pre-Mux.
- Self-updates of Windows workers, recovery of jobs waiting for disk space or being copied, and no false "online" state when the NAS or VPN is down.
- Container detection on Synology (cgroup v2), a shutdown deadlock in the Linux worker and several race conditions in workers.
- Windows taskbar icon, sharp display across monitors with different scaling and CPU spikes while an update was postponed.

## [2.10.8] - 2026-11-10
### Added
- **Full `locales.js` Unification & OTA Worker Distribution:** Migrated all 267 remaining backend/worker strings (`manager_core.py`, `main.py`, `worker_core.py`, `worker.pyw`, `worker_linux.py`) and frontend notifications (`app.js`) into `locales.js` (525 keys across `en` and `cs`). Workers now download, verify, and store `locales.js` locally on disk during initial bootstrap and transactional OTA updates (`verify_and_stage_two_file_update`), allowing instant offline translation loading before network connection is established.
- **Sub-Version OTA Testing Support (`2.10.8-A`, `2.10.8-B`):** Extended version synchronization, regex stamping, and OTA update comparisons to support alphanumeric sub-version suffixes (`-A`, `-B`, etc.) for rapid iterative testing while keeping release notes consolidated under the base version.
- **Header Quick-Navigation Shortcuts:** Clicking the **HB Cluster Manager** title in the top header jumps directly to the *Queue* tab, and clicking the version badge (`v2.10.8`) opens the *Changelog* tab.
- **Video Stream Size & File Size Hover Tooltips (`videoStreamSizeTooltip`, `fileSizeTooltip`):** Hovering over the video bitrate badge or `%` savings badge in *Completed Jobs*, *Queue*, or *Global Search* displays the exact original vs. encoded video stream size in GB/TB alongside bitrates and stream-level storage savings; hovering over the file size badge displays original vs. encoded container size and total disk space saved.

### Fixed
- **Watchdog Queue Import Cleanup (`process_watchdog_file`):** Fixed an issue where `queue*.json` files placed in `/watch_dir` were not removed or archived after import, which could cause `poll_directory()` to re-import completed jobs. Processed files are now deleted (or renamed to `.imported` / `.error` on read-only mounts).
- **Cluster Intake Gate Log Messages (`api_v2_cluster_action`):** Fixed swapped `PAUSED` and `RESUMED` log entries when toggling job intake across all workers.
- **Database Models & GitHub OTA Coverage (`models.py`, `database.py`, `perform_github_self_update`):** Added `Job.is_paused` statically to `models.py`, added `HB_DATA_DIR` fallback support in `database.py`, included `models.py`, `database.py`, and `repair_premux_movies.py` in GitHub OTA self-updates, and stripped JS `//` comments before `ast.literal_eval()` when parsing `locales.js`.
- **Linux Binary Builder Container Cleanup (`process_system_build_job_thread`):** Added automatic cleanup of `/tmp/hb_staticx_venv` and `apt-get clean` / `/var/lib/apt/lists` in the `finally` block after compiling binaries.
- **Accurate Stream Bitrate Probing (`probe_media_bitrates`):** Added fallback calculation from MKV stream tags (`NUMBER_OF_BYTES` / `DURATION`) and codec-aware audio bitrate estimation for lossless/untagged audio tracks (`TrueHD`, `DTS-HD MA`, `FLAC`, `E-AC-3`, `PCM`) so container bitrate subtraction never inflates source video bitrate.
- **Smart Pre-Mux Multi-Track Audio Matching (`match_premux_audio_stream`):** Added `(hb_track, mode)` tracking so multiple audio tracks of the same language and channel layout (e.g., main track + commentary) bind to distinct streams in `premux_source.mkv` while still allowing dual passthrough + `AC3` compatibility encodes from a single lossless source track.
- **Exact Track Order & Language Preservation for LPCM 1:1 Passthrough (`build_postproc_ffmpeg_cmd`):** LPCM/PCM tracks muxed in post-processing now interleave at their exact original HandBrake queue position (`hb_audio_idx`) instead of being appended at the end, preserving language tags, track titles, chapter markers (`-map_chapters 0`), and `verify_output_stream_contract` ordering, including when combined with Smart Pre-Mux.
- **Restored Rich Worker Cards in Sidebar Mode:** Fixed the sidebar worker panel (`workersPlacementMode === 'sidebar'`) so it displays the full interactive worker cards (status dot, active/paused/idle badges, intake toggle, pause/resume button, and quick `All` filter reset) stacked vertically, matching the header worker bubbles.
- **Restored Settings UI Controls & Live Path Preview:** Restored the Left/Right sidebar position selector (`⬅ Left` / `Right ➡`), live output path preview (`ineffectiveExamplePath`) under *Ineffective Compression Detection*, and section descriptions from `v2.8.1`.
- **Streamlined Header Intake Button Label:** Removed redundant `(Gate Open)` / `(Gate Closed)` suffix from the header intake control button.
- **Persistent Worker Intake & Settings Across Updates/Restarts (`load_manager_config`, `save_manager_config`, `worker.pyw`):** Prioritized `/data/manager_config.json` over `/app/manager_config.json` so copying update files into `/app` never resets `smart_premux` or other user settings. Added automatic backup/restore of all worker policies (`is_enabled`, `power_mode`, `max_slots`, etc.) and `maintenance_mode` in `/data/manager_config.json`, plus local `accept_jobs` persistence and `pending_accept_override` synchronization in `worker.pyw`.
- **Outer Side-Docked Sidebar & Responsive Completed Jobs Layout (`index.html`):** Docked the worker sidebar in the outer side gutter (`max-w-[2160px]`) so `<main>` keeps its full `1600px` centered width on wide monitors. Redesigned the *Completed Jobs* list to dynamically reflow and stack metadata on narrower windows/monitors without clipping the right-hand action buttons or wrapping bitrate pills.
- **3-Row `Bitrate & Size` Display with File Icon & Separate `%` Badges (`index.html`):** Standardized the *Bitrate & Size* column into three distinct rows: (1) Video stream bitrate (`🎞 Mb/s → Mb/s`) + separate `%` badge, (2) File size (`📄 GB → GB`) + separate `%` badge, and (3) technical/status badges (`MKV Stats`, `LPCM 1:1`, `Pre-Mux OK/OFF`, subtitle status) on the third line.

## [2.10.7] - 2026-11-08
### Added
- **Unified Single-Source Localization (`/api/locales` & `locales.js`):** Consolidated all UI and worker translations into a single dictionary, eliminating duplicate bilingual strings across Python scripts.
- **3-Island Navigation & Expandable Header Controls:** Renamed the main tab to *Queue*. Both header action buttons (*Pause Running Encodes* and *Job Intake Gate*) now use compact monochrome vector icons that change color by state and smoothly expand with text labels on hover.
- **3-State Central Tool Manager Button & Relocated Clean Reinstall:** The main tool build button now accurately reflects three states: *All Tools Up to Date ✓*, *Update Outdated / Missing Tools*, and *Waiting for Workers to Finish Active Jobs to Sync*. Moved the *Force Complete Reinstall of All Tools* button into the *Expert Settings* section.
- **Direct Pause Button in Windows Worker GUI (`worker.pyw`):** Added a live *Pause / Resume* button directly inside the Windows Worker window and System Tray menu to freeze/unfreeze `HandBrakeCLI` in RAM without opening the web UI.

### Fixed
- **Strict AMD/Intel Multi-Core `ECO` Mode Enforcement (22/24 Threads):** Fixed an issue where `x265` / `HandBrakeCLI` ignored initial process affinity on high-core-count AMD CPUs. `ECO` mode now continuously enforces the `(cores - 2)` bitmask (`0x3FFFFF` on 24-thread CPUs) across the entire process tree (including `staticx` child PIDs on Linux and all worker handles on Windows) and injects `pools=N-2` when starting in `ECO` mode.
- **Enhanced Laptop Wake-Lock & Instant Reconnect (laptop workers):** Added `ES_AWAYMODE_REQUIRED` execution state flags and immediate heartbeat recovery after Windows sleep/wake events.
- **Codebase Optimization & Refactoring:** Streamlined database session handling, JSON payload parsing, and header manifests across `manager_core.py`, `main.py`, `worker_core.py`, and frontend assets while preserving 100% of existing features.

## [2.10.6] - 2026-11-07
### Added
- **3-Island Navigation Bar:** Grouped navigation tabs into *Video Jobs* (`Queue`, `Completed`, `Queue Import`), *Compute Nodes* (`Workers`, `Monitoring`), and *System* (`Live Logs`, `Settings`, `Changelog`).
- **Global Search Across Queue & Completed Jobs:** The top search bar now filters both the active/waiting queue and completed job history simultaneously, showing live match counters (`Queue: X | Completed: Y`) and an inline *Found in Completed Jobs* section.
- **Interactive Per-Tool Update Cards & Auto-Update Toggle (`auto_update_binaries`):** Added 4 dedicated tool cards (`HandBrakeCLI`, `MKVToolNix`, `FFmpeg & FFprobe`, `7-Zip`) showing installed vs. online versions with amber update highlights.

### Fixed
- **Smart Differential Tool Updates (`get_outdated_or_missing_build_targets`):** Updating tools now compiles/downloads only the specific outdated or missing binaries instead of rebuilding all tools.
- **Fixed `7-Zip` (`sevenzip_latest`) version forwarding** in system build tasks.

## [2.10.5] - 2026-11-05
### Added
- **Live Running-Job Freeze / Unfreeze (`⏸/▶` in RAM) & Local Temp Disk Guard (`check_live_pause_and_disk_guard`):** Individual and cluster-wide pause/resume of running `HandBrakeCLI` processes via `NtSuspendProcess` (Windows) and `SIGSTOP` (Linux). Automatically freezes encoding if local `HB_Temp` drops below `3.5 GB` and resumes once `5.0 GB` is available.
- **Dynamic 32-bit RGBA System Tray & Window Status Icon (`render_tray_status_icon`)** with active job counter and state color coding.
- **Combined All-Workers Live Log Stream (`ALL`)** with automatic `[WorkerName]` prefixing.

### Fixed
- **Leak-Proof SQLite Session Management (`QueuePool` expansion to 60 connections):** Wrapped all database sessions in `try ... except ... finally` blocks with automatic rollback.

## [2.10.4] - 2026-11-01
### Added
- **Native Win32 Kernel Copy (`CopyFileExW`) with SMB3 Pipelining:** Replaced Python chunk loops on Windows with native `kernel32.CopyFileExW` and non-blocking progress callbacks for maximum NAS throughput.
- **Output Stream Contract Verification (`verify_output_stream_contract`):** Verifies output audio/subtitle track counts, codecs, languages, and duration via `ffprobe` before uploading to target storage.

### Fixed
- **Binary Blu-ray `.MPLS` `STN_table` Parser (`parse_mpls_stn_table_streams`):** Eliminates track index shifts and duplicate embedded AC3/DTS core tracks during `mkvmerge` Pre-Mux.

## [2.10.3] - 2026-10-28
### Added
- **Smart Disk Space Strategy (`evaluate_disk_space_strategy` with 85 GB 4K reserve) & Post-Processing Rescue (`[RESCUE]`):** Automatically falls back to direct network I/O when local temp space is insufficient and rescues finished encodes into `WAITING_DISK` if space runs out during post-processing.

### Fixed
- **Preserved tail of `HandBrakeCLI` error output (`str(e)[-250:]`)** so the actual failure reason is always visible in tooltips and logs.

## [2.10.2] - 2026-10-24
### Added
- **Real-Time Disk-Based Pre-Mux Progress (`Pre-Mux 1/2` & `Pre-Mux 2/2`):** Measures actual byte growth of extracted `.M2TS` clips and `premux_source.mkv` on disk every second, displaying live `%`, `MB/s`, and `GB / GB`.
- **Expandable 5-Minute CPU & RAM History SVG Graphs** on worker cards in *Monitoring*.
- **Collapsible Expert Settings & GitHub OTA Self-Updater (`os.execv`)** with syntax verification.

### Fixed
- **Smart Retry Counter (`1/3`) with Failure Reason Tooltip:** Worker restarts no longer penalize `attempt_count`.
- **Unified `inputs` and `outputs` directory naming** across Manager and Workers.

## [2.10.1] - 2026-10-19
### Added
- **Dynamic Browser Tab Favicon (`#hb-favicon`):** Renders a live 256×256 canvas badge with remaining job count and status color.
- **Direct `.M2TS` Concatenation Fallback for UHD Blu-ray Pre-Mux:** Automatically merges extracted `.M2TS` clips in playlist order if `mkvmerge` returns exit code `2` on Dolby Vision UHD discs.

### Fixed
- **1:1 Source Audio Track Title Preservation:** Reads original audio track names via `ffprobe` and writes them via `mkvpropedit`, stripping synthetic HandBrake `"Stereo"` / `"Surround"` labels.
- **Fixed `.ISO` Input Bitrate Detection (`parse_mpls_header_info` offsets `pos+14` / `pos+18`)** and enabled SQLite WAL mode.

## [2.10.0] - 2026-10-14
### Added
- **Bidirectional Cluster Language Sync (`CZ` <-> `EN`)** across web UI, Windows/Linux workers, and live logs.
- **16MB Streaming HTTP Relay Mode (`/relay_download` & `/relay_upload`)** for remote workers without direct NAS mounts.
- **Passive UDP Manager Discovery (`UDP 8383`)** via the *Find* button in Windows Worker and `MANAGER_URL=AUTO` on Linux.

## [2.9.3] - 2026-10-08
### Added
- **Live Log Filtering (Search, Errors & Warnings Only, Hide % Progress)** and smart Forced subtitle extraction status (`FORCED_OK`).
- **Automatic Version Synchronizer (`ensure_workers_subdir_and_migrate`)** across all Manager and Worker scripts.

### Fixed
- **Accurate HandBrake Title-to-`.MPLS` Playlist Matching (`--previews 1:0 --scan`)** for multi-title Blu-ray discs.

## [2.9.2] - 2026-10-03
### Fixed
- **Fixed `4K` resolution & duration detection during batch queue imports**, added binary probe caching (`_bin_probe_cache`), deduplicated log progress lines, and enforced `0777` file permissions.

## [2.9.1] - 2026-09-27
### Added
- **Target Storage Protection (`Storage Guard`) with Atomic Upload Reservations & Local Hold (`WAITING_DISK` / `held_job_meta.json`)**.
- **Central *Live Logs* Tab** and modular backend/frontend architecture (`/app/workers/`).

### Fixed
- **Accurate Linux CPU & RAM Telemetry for `staticx` Child Process Trees (`/proc/<pid>/task/*/children`)** and automatic UTF-8 BOM stripping.

## [2.9.0] - 2026-09-21
### Added
- **Shared `worker_core.py` Engine with Immortal Bootstrap Guard** and transactional two-file OTA updates.
- **9-Step System Compilation Checklist & Live Build Console**.

## [2.8.4] - 2026-09-14
### Added
- **Live CPU & RAM Mini-Bars and 4× Supersampled Anti-Aliased Icons in Windows Worker (`worker.pyw`)**.
- **Expandable Changelog Accordion** and HandBrake Preset badges in *Completed* and *Job Detail*.

### Fixed
- **Eliminated synthetic `2500 kb/s` fallback bitrate (`In: Unmeasured`)** and excluded unmeasured jobs from `EncodingStats`.
- **Window-level Drag & Drop `.json` queue import** and locked worker trash button tooltips.

## [2.8.3] - 2026-09-08
### Added
- **Unified Monochrome Vector SVG Icons**, Worker Stop button with two-click confirmation, and dark Windows 11 title bar (`DwmSetWindowAttribute`).

### Fixed
- **Full Windows `7z.exe` + `7z.dll` distribution** for UDF `.ISO` extraction.

## [2.8.2] - 2026-09-02
### Added
- **Native Win32 System Tray Icon (`Shell_NotifyIconW`)**, dark Windows Worker theme, universal `ECO` CPU affinity mode, and laptop sleep prevention.

## [2.8.1] - 2026-08-26
### Added
- **`Manager Engine` Status Badge & Multi-Line Tooltips** in *Monitoring*, plus automatic 2 MB log rotation (`cluster_debug.log`).

### Fixed
- **Eliminated 5-minute Storage Handshake log spam (`handshake_lock`)** and capped TrueHD audio bitrate deduction on Blu-ray `.ISO` images.

## [2.8.0] - 2026-08-19
### Added
- **Unified `/app/binaries/{linux,windows}` & Worker `bin/` Directories**, forced clean binary rebuilds (`binary_generation`), 6-hour upstream version watcher, two-phase `.ISO` Pre-Mux with `CLIPINF/*.clpi` metadata preservation, and torrent-style `#1..N` queue numbering.

## [2.7.0] - 2026-08-11
### Added
- **`Pri:MAX` System Build Task (`SYSTEM_BUILD`)** for automated Linux source compilation (`HandBrakeCLI` + `staticx`) and official Windows binary downloads (`MKVToolNix`, `FFmpeg`, `7-Zip`), plus full `.ISO` Pre-Mux support.

## [2.6.0] - 2026-08-03
### Added
- **Local NVMe `mkvmerge` Pre-Mux (`PREMUX` state)** for `BDMV` folders, bulk worker controls, pure-Python UDF 2.50 `.ISO` playlist reader, and central `mkvmerge` distribution.

## [2.5.0] - 2026-07-26
### Added
- **Complete 1:1 Linux (`libhb`) & Windows (`Task`) HandBrake JSON Translator**, ineffective compression detection (`[VYSSI_BITRATE]_` / `_VYSSI_BITRATE_NEZ_ZDROJ`), and instant source metadata prefill on import.

## [2.4.0] - 2026-07-18
### Added
- **Native Linux HandBrake GUI JSON Support**, live pending binary sync badges, and on-the-fly helper tool downloads during active jobs.

## [2.3.0] - 2026-07-10
### Added
- **Per-Title `.ISO` / `BDMV` Resolution & Bitrate Scanning (`HandBrakeCLI --scan`)**, packet-level subtitle analyzer & deduplicator (`framemd5`), and `--subtitle-forced=` syntax fix.

## [2.2.0] - 2026-07-02
### Added
- **Smart Recursive Path Resolver (`resolve_input_path_smart`)**, universal `inputs`/`outputs` mounts, and bidirectional Storage Handshake (`/api/workers/handshake`).

## [2.1.0] - 2026-06-24
### Added
- **Input/Output Video & Audio Bitrate Measurement**, `mkvpropedit` track statistics tagging, lossless 1:1 LPCM/PCM audio passthrough, and persistent `EncodingStats` analytics table.

## [2.0.0] - 2026-06-15
### Added
- **Decoupled FastAPI + Vue 3 + Tailwind CSS SPA Architecture (`/api/v2/*`)**, structured numeric database columns, automatic history migration, Drag & Drop queue reordering (`SortableJS`), and Job Inspector modal.

## [1.12.5] - 2026-06-06
### Added
- **Plex-Style Live CPU & RAM Telemetry**, central binary distribution, Windows 11 Hybrid CPU Power Manager (`AUTO` / `FULL` / `ECO`), and Linux in-place auto-update (`os.execv`).

## [1.12.0] - 2026-05-28
### Added
- **Permanent Worker Version Stamping**, MD5 duplicate job detector, resilient network copy with seek resume, extended `HandBrakeCLI` error tail logging, and background `ffprobe` resolution cache.

## [1.11.0] - 2026-05-19
### Added
- **Full-Text Queue Search**, chronological Completed history sorting, resolution conversion badges, and GUI toggles for Forced subtitle extraction.

## [1.10.0] - 2026-05-11
### Added
- **Missing Source File Detection (`ERR_SOURCE_MISSING`)**, SD resolution classification (`480p` / `576p`), automated empty subtitle cleanup, strict 4K FIFO reservation, and built-in Changelog viewer.

## [1.9.0] - 2026-05-02
### Added
- **Local NVMe Staging Mode** with format filters (`Never`, `ISO Only`, `ISO & BDMV`, `All Formats`) and automatic OTA worker script updates.

## [1.8.0] - 2026-04-23
### Added
- **Automatic `ASS/SSA -> SRT` Subtitle Conversion** and `/watch_dir` folder watchdog for automatic `queue*.json` imports.

## [1.5.0] - 2026-04-14
### Added
- **Multi-Slot Worker Execution**, resolution capability routing (`1080p` vs. `4K`), and persistent SQLite database via SQLAlchemy.

## [1.0.0] - 2026-04-04
### Added
- **Initial Release of HB Cluster Manager & Multi-Platform Workers (Windows Tkinter GUI & Linux Headless CLI)** with live heartbeat telemetry.