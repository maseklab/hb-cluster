# FAQ & troubleshooting

[← README](../README.md) · [Česky](cs/faq.md)

**A worker shows as offline.**
Check that the worker can reach the Manager address (`MANAGER_URL` for Linux, the address in the Windows worker window) and that port **8383** is not blocked by a firewall. Look at the worker log – **Live Logs** in the Manager, `docker logs <container>` for a Linux worker, or the log in the Windows worker window.

**A new worker does not get any jobs.**
If sign-in is on (worker approval goes with it), it waits for you – see the orange notice in the header or the top of the **Workers** tab. Also check that its **job intake** is on and that it has free slots.

**A Windows worker cannot access the NAS folders.**
Enter the SMB account in **Settings → Storage & Network** (*Set / Change SMB Account → Save & test*, or *Test SMB & Save Paths*). Folders are UNC paths, e.g. `\\nas\video\inputs`. A worker without NAS access still works through the Manager's HTTP relay, just more slowly.

**Windows SmartScreen warns about `HBWorker.exe`.**
The package from **your Manager** (*+ Add Worker*) is signed by your Manager's self-signed certificate. Because that certificate is not in Windows' Trusted Root store by default, Windows SmartScreen may still warn on a `.zip` downloaded via a browser (*More info → Run anyway*, or right-click the `.zip` → *Properties → Unblock* before extracting). The one-line `cmd` install avoids the browser download warning automatically; optionally you can import `app\maseklab_codesign.cer` into Windows' Trusted Root / Trusted Publishers store.

**Settings → Security says the Manager does not see real device addresses.**
The Manager runs in a bridge network on a system that hides client addresses behind the Docker gateway. Run it with `network_mode: host` – see [Security](security.md#access-from-outside--recommended-setup).

**An update is "still being prepared".**
The new Manager version is published but the worker image of the same version is not yet. The Manager offers the update once both exist; nothing is needed from you.

**I forgot the web password.**
Create an empty file `reset_admin_password` in the Manager's `/data` folder and wait 10 seconds – sign-in turns off.

**Where are the logs?**
In the **Live Logs** tab (Manager and all workers). On disk: `cluster_manager.log` in the Manager's `/data`, `worker.log` in a Linux worker's `/data`.

**Can I use it without a NAS?**
Yes. Workers without access to shared folders transfer the source and the result through the Manager (HTTP relay).

**My Homepage / Homarr widget stopped working after enabling sign-in.**
`GET /api/stats` is readable without sign-in unless you turned off *Status summary for dashboards without sign-in* in **Settings → Security**.

**A worker says "Other Manager – accepting nothing".**
The worker is bound to a different Manager ID than the Manager now answering at its address (for example after reinstalling the Manager with an empty `/data`). If this Manager is the right one, re-bind the worker – see [Workers stay with their Manager](security.md#workers-stay-with-their-manager).

**A finished file has `[ID 1234]` in its name.**
A file of the same name already existed in Outputs. HB Cluster never overwrites an existing file, so the new one got the job ID in its name; the real name is in the log and in **Completed**.

**How do I restore the database from a backup?**
The Manager keeps daily backups (last 7) and backups made before each update (last 3) in `/data/backup` – also downloadable in **Settings → About & Updates → Database backups**. To restore: stop the Manager container, delete `cluster.db`, `cluster.db-wal` and `cluster.db-shm` in `/data`, copy the chosen backup there as `cluster.db` and start the Manager again.
