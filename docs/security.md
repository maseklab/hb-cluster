# Security & remote access

[← README](../README.md) · [Česky](cs/security.md)

Everything here is **optional and off by default** – a home cluster on a trusted LAN works without it. Keep in mind that without sign-in, anyone on your network can control the Manager, read any saved SMB/NAS credentials intended for Windows workers, and upload custom tools in **Settings → Tools**. Turn on web sign-in (optionally with *No sign-in from the home network* if you want passwordless access from your own PC) as soon as you store SMB credentials or share the network with untrusted devices. All settings are in **Settings → Security**; the overview at the top shows sign-in, worker approval and waiting requests at a glance.

## Web sign-in

- A password for the web interface, stored only as a **scrypt hash**.
- The sign-in page works with browser password managers. **Remember this device** keeps you signed in for a year; otherwise until the browser closes (at most 12 hours of inactivity).
- **Signed-in devices** – browser, system, address and last use; sign out a single device or all others.
- After **5 wrong passwords** from one address further attempts are delayed (1 → 5 → 15 minutes).
- **Forgot the password?** Create an empty file `reset_admin_password` in the Manager's `/data` folder – within 10 seconds sign-in turns off and the password (and passkeys) are deleted.
- Turning sign-in on also turns on **worker approval** (below); turning it off switches both off.

## Passkeys

Sign in with a **fingerprint, face or security key** instead of the password (*Add a passkey for this device*). Passkeys need **HTTPS** and belong to the domain name, so with split DNS the same passkey works at home and outside. Over a plain IP address only the password is available. The password always keeps working.

## Worker approval

Every worker creates its own random key on first start (`worker_identity.json`) and sends it with each request – nothing is copied by hand.

- Worker approval is **part of web sign-in**: it turns on and off together with it and has no switch of its own.
- An unknown worker gets **no jobs, settings or SMB credentials** until you approve it. Workers the Manager knows when you turn sign-in on are approved automatically, and for the next 15 minutes new ones are approved too.
- Requests appear as an orange notice in the header and at the top of the **Workers** tab: **Approve**, **Reject for 24 h**, **Ignore permanently**.
- **Approve automatically for 15 min** – handy when adding several machines.
- A Windows worker from **+ Add Worker** can be **pre-approved for 1 hour**; the download link and install command are valid for 1 hour as well.
- Updates stay available to every worker, so even an older worker can update itself.
- A worker can upload built tools only while it runs the system build assigned to it, and only the tools of that build. A manual upload in **Settings → Tools → Advanced** is then available only after signing in.

## Workers stay with their Manager

Each Manager has a random ID (`/data/manager_identity.json`) and sends it with every response. A worker remembers the ID on its first connection (`manager_binding.json` next to `worker_identity.json`) and from then on **accepts nothing from a Manager with a different ID** – no jobs, settings or updates. This protects against a fake Manager (for example a forged answer to *Find*) and against a mistyped address. The address itself may change (new IP, domain) – only the ID matters.

- Such a worker shows **Other Manager – accepting nothing** in its window and appears on the **Workers** tab of the other Manager under *Bound to another Manager*.
- To re-bind (e.g. after reinstalling the Manager with an empty `/data`): in the Windows worker open **Options → Bind to this Manager** (with confirmation); for a Linux worker delete `/data/manager_binding.json`, or start it once with `HB_MANAGER_REBIND=1` and then remove the variable.
- Keep the Manager's `/data` in your backups – with it the ID survives reinstalls and updates.

## Protection against other websites

The Manager refuses requests to its API that another website starts in your browser (the browser itself marks them with `Sec-Fetch-Site`). A malicious page you open therefore cannot control the Manager – even without sign-in. Workers, scripts and `curl` are not affected; links to the Windows worker package, updates and `/api/stats` keep working from anywhere.

## Access from outside – recommended setup

1. **Run the Manager with `network_mode: host`.** In a regular bridge network, some systems (e.g. a NAS like Synology, Docker Desktop, rootless Docker / Podman, IPv6 connections) show the Manager only the Docker gateway instead of the real client address. The Manager detects this; the Security page then explains it and keeps the home-network and proxy options locked.
2. **Put HTTPS in front of the Manager** – a reverse proxy with a certificate (Nginx Proxy Manager, Caddy, Traefik, nginx) or a **Cloudflare Tunnel** (optionally with Cloudflare Access / Zero Trust).
3. **Behind a reverse proxy** turn on **I use a reverse proxy** and enter the address of the machine where the proxy runs (**not** the Manager). The Manager then uses the real client address the proxy sends (`X-Forwarded-For`) – only from that address, so nobody can fake it. *Your address as the Manager sees it* lets you check the result.
4. **No sign-in from the home network** (optional) – requests from your home networks open without a password. Requests through Cloudflare (`CF-Connecting-IP`) always require sign-in, and so do requests from an unknown proxy.

## Docker socket (`/var/run/docker.sock`)

Mounting `/var/run/docker.sock` on the Manager or Linux workers is **optional** and used exclusively for one-click / automatic container updates (pulling the new image and recreating the container without interrupting running jobs). Because access to the Docker socket grants elevated control over containers on the host, omit this mount if you prefer stricter isolation and update manually with `docker compose pull && docker compose up -d`.

## Dashboards

`GET /api/stats` returns only counts (online workers, running / waiting jobs) and stays readable without a password, so widgets in **Homepage**, **Homarr** or **Dashy** keep working. Turn it off with **Status summary for dashboards without sign-in**.
