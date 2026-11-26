# Encoding & features

[← README](../README.md) · [Česky](cs/encoding.md)

## Jobs come from HandBrake

HB Cluster does not invent its own encoding settings. You prepare jobs in desktop **HandBrake** – one movie at a time with its own preset, RF, crop, filters, audio and subtitle tracks, or a whole folder with one preset – and export the queue as `.json`. Every job keeps exactly those settings.

Import the queue by dropping the `.json` file anywhere on the Manager page, with the **+** button above the queue, or automatically from an optional watch folder (`/watch_dir`). The Manager can find duplicate jobs in the queue and clean them up.

## Weighted slots

Each worker has a number of **slots**; a job takes slots by its target resolution – **4K = 4**, **1080p = 2**, **SD / 720p = 1**. A 4K-capable worker (4 slots) can therefore run 1× 4K, 2× 1080p, 1× 1080p + 2× SD or 4× SD at once. The Manager hands out jobs by queue order, free slots and the *Prefer 4K* setting, so strong machines get the heavy titles.

## CPU encoding into `.mkv`

HB Cluster is built for software encoding (**x265, x264, SVT-AV1**) where quality per bit matters most. After HandBrake finishes, an optional post-processing step with FFmpeg / MKVToolNix can:

- **LPCM** – copy the original LPCM audio losslessly.
- **DTS-HD MA & DTS core** – by default keeps tracks exactly as selected in HandBrake (extracting the DTS core when Pre-Mux is enabled); optionally merges duplicate DTS Core + DTS-HD MA pairs of the same language into a single DTS-HD MA track (beta; ~1.3 GB saved per 2h movie, Plex extracts the core on the fly).
- **Clean subtitles** – analyse subtitle packets and drop empty or duplicate tracks.
- **Forced subtitles** – create a separate forced track during encoding and remove it again if it stays empty.
- **ASS / SSA → SRT** – for players that cannot render ASS and burn it in (e.g. Plex on a TV). *Keep ASS and add SRT* puts SRT first and keeps the original default / forced flags; *Replace* keeps only SRT.
- **MKV statistics** – write exact track statistics into the MKV header (`mkvpropedit`).

**Burned-in subtitles** (*Burn In* ticked in HandBrake) are burned into the picture by default, exactly as in HandBrake; you can keep them as a separate track instead. A job with such subtitles shows a **Burn-in** badge in the queue.

All of this is set in **Settings → Video Processing**.

## Blu-ray folders and `.iso`

Sources can be single video files, **Blu-ray folders (`BDMV`)** or **`.iso` images**. The worker reads the selected playlist (`.MPLS`). With **Smart Pre-Mux** (beta, off by default) the worker first extracts the selected playlist and its clip info (from an `.iso` with 7-Zip), joins it with `mkvmerge` on its local disk and then encodes the result.

## Storage: NAS paths and HTTP relay

- Workers with access to the shared `/inputs` and `/outputs` read the source directly from the NAS, encode on a fast local disk (temp) and upload the finished file.
- A worker without NAS access (e.g. a laptop elsewhere) streams the source and the result **through the Manager (HTTP relay)** – no extra setup.
- When the local temp disk is too small for a job, the worker reads or writes directly over the network instead.
- **Storage guard** – when the output disk gets below the minimum free space, workers keep finished movies on their own disk (as long as they have at least 25 GB free in temp) and upload them once you free up space.

## Encoding tools

The Manager distributes **HandBrakeCLI, FFmpeg / FFprobe, MKVToolNix and 7-Zip** to all workers:

- With a **Linux worker** online, the strongest one builds static Linux tools from source.
- In a **Windows-only cluster** the Manager downloads the official Windows builds itself.
- Optionally the Manager checks for new tool versions and updates them automatically (**Settings → Tools**).

## Queue, monitoring and statistics

- Drag & drop ordering, move to top / bottom, postpone, requeue, cancel – with two-click confirmation for destructive actions.
- **ETA** for the whole queue, live FPS and progress, live logs of the Manager and every worker.
- **Statistics** that survive clearing the history: total space saved and speed profiles of every worker by resolution, encoder, preset, RF and bitrate.
- A `GET /api/stats` summary (online workers, running / waiting jobs) for dashboards such as Homepage, Homarr or Dashy.
