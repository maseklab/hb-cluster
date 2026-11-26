# Workers

[← README](../README.md) · [Česky](cs/workers.md)

A **worker** is a computer that encodes. You can mix Windows PCs and laptops with Linux servers in Docker; every worker gets jobs from the Manager according to its **slots** and settings.

> **Recommended:** create workers from the Manager – **Workers → +** (or step 2 of the first-run wizard). The Manager fills in its own address, the worker name, shared folders and SMB credentials for you.

## Windows worker

**From the Manager (recommended)**
1. **Workers → +** → *Windows*: set the worker name, shared folders (UNC paths, e.g. `\\nas\video\inputs`) and when the worker should start:
   - **Manually** – double-click `HBWorker.exe` when you want to encode.
   - **After sign-in** – starts after you sign in to Windows and runs in the system tray.
   - **Before sign-in (beta)** – a Task Scheduler job starts it at boot (for an unattended PC); once you sign in, the tray window takes over. Windows asks for permission once when you turn it on; the task then runs under your account with normal rights (no Windows password needed) and connects to the NAS using the Manager credentials. The rights check in the worker log shows whether everything is set up properly.
2. **Download the pre-configured `.zip`**, extract it anywhere (e.g. `C:\HBWorker`) and run `HBWorker.exe` – or copy the one-line install command into **Command Prompt (`cmd`)**.
3. With worker approval on, keep **Pre-approve this worker (valid 1 hour)** ticked and the worker is approved automatically when started within the hour.

`HBWorker.exe` is signed by your own Manager (each Manager creates its own signing key in `/data/certs/` – no private key is in this repository). **Python** is needed: the launcher finds an existing installation, and only if none is found installs the newest Python automatically (winget, otherwise python.org – only an installer with a valid Python Software Foundation signature is run).

**From GitHub Releases** – download [`HB-Worker-Windows.zip`](https://github.com/maseklab/hb-cluster/releases/latest/download/HB-Worker-Windows.zip), extract it and run `HBWorker.exe`. The first-run wizard automatically searches for the Manager on your LAN (UDP 8383) – click **Verify** (or **Find** in the main window) to retry, or enter the Manager address manually.

**In the worker window**: **⚡ Power** switches the power mode directly from the top bar; **⚙ Options** holds sleep prevention (*while encoding* / *always* / *off* – the screen can still turn off), folders, custom CPU cores and the *📂 Open worker folder* button. `Uninstall_Worker.bat` removes the worker.

## Linux worker (Docker)

In the Manager choose **Workers → +** → *Linux* and copy the generated compose (Portainer, Dockge or `docker compose`). For reference, it looks like this:

```yaml
services:
  hb-worker-1:
    image: ghcr.io/maseklab/hb-worker:latest
    container_name: hb-worker-1
    restart: unless-stopped
    environment:
      - WORKER_NAME=Linux-Worker-1             # name shown in the Manager
      - MANAGER_URL=http://192.168.1.100:8383  # address of your Manager
      - TZ=UTC                                 # set your timezone (used for NIGHT power mode 22:00-06:00)
    volumes:
      - ./data:/data                           # temp, tools and log of the worker (NAS/Portainer: absolute path)
      - /mnt/nas/inputs:/inputs:ro
      - /mnt/nas/outputs:/outputs
      # - /mnt/nvme/hb_temp:/temp              # optional: separate fast disk for temporary files
      - /var/run/docker.sock:/var/run/docker.sock  # optional: automatic worker updates
    labels:
      - "com.centurylinklabs.watchtower.enable=false"
```

**Every worker needs its own data folder (`/data`).** Two containers with the same folder would share the worker key, settings and Temp – the second one waits 60 s, writes the reason to `docker logs` and stops; Docker tries again later. For more workers on one machine give each its own folder, e.g. `/opt/hb_worker/linux-worker-1` and `/opt/hb_worker/linux-worker-2`. The same applies to a separate Temp disk (`/temp`): give each worker its own subfolder, e.g. `/mnt/nvme/hb_temp/linux-worker-1` – the compose from the Manager does this automatically.

The worker cleans up only its own temporary files. If a folder you give it as Temp (e.g. `/temp`, or a custom temp folder of a Windows worker) already contains other files, the worker creates an `HB_Temp` subfolder and works only there – the other files stay untouched.

With `docker.sock` mounted, the worker updates itself by pulling its new image and recreating its own container. Without it, update manually: `docker compose pull && docker compose up -d`.

## Slots, power and priority

- **Slots** – a job takes slots by its target resolution: **4K = 4**, **1080p = 2**, **SD / 720p = 1**. A 4K-capable worker has 4 slots by default (1× 4K, 2× 1080p, 4× SD…), a 1080p worker 2, an SD-only worker 1. You can change it on the worker card.
- **Power mode** (worker card in the Manager or the worker window):
  - **AUTO** – on Windows ECO while you use the computer and FULL after 3 minutes of inactivity; FULL on Linux.
  - **FULL** – all threads, below-normal priority.
  - **ECO** – fewer threads, lower priority; the computer stays usable.
  - **NIGHT** – FULL from 22:00 to 06:00, otherwise ECO (or E-cores).
  - **HYBRID ECO** – only efficiency cores on CPUs with P- and E-cores.
  - **CUSTOM** – choose exact cores (P-cores / E-cores / SMT).
- **Prefer 4K** – the worker takes 4K jobs first.
- **Job intake** – a worker with intake off finishes its running jobs and takes no new ones. **Stop** cancels its running jobs immediately and turns intake off.

## Updates

The Manager offers a new version (or installs it automatically) only when both the Manager and the worker images of that version are published. **Windows workers** download the new files from the Manager, **Linux workers** recreate themselves from the new image. A worker always finishes its running job first and updates before taking the next one.

Before switching, the new version is test-loaded in a separate process (Linux and Manager: a temporary container without network or volumes). After switching it has to start: the Manager must answer on port 8383 within 60 s, a Linux worker must report that it runs (or run for 60 s without crashing), a Windows worker must open its window within 2 minutes. Otherwise the previous version comes back automatically (Windows from the `app\*.prev` backups) and the next automatic attempt waits 6 hours. After a successful update the previous HB image is removed if no other container uses it.

If an update fails for another reason (no newer image, download error), the next attempt follows in 5–15 minutes; after a second failure of the same version it waits 6 hours. **Update now** on the worker card skips the wait (a busy worker first finishes its jobs). A manual Manager update in Settings → About & Updates works right away as well.

A Linux worker downloads images only from its own channel (`:latest` or `:dev`) and never switches it on its own. If its channel differs from the Manager's, the worker card shows **Different channel than Manager**; to switch, change the image tag in your stack. Use `:latest` (stable releases); `:dev` is built from every change on the `dev` branch and is meant only for testing.
