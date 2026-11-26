# Third-party software

HB Cluster (HB Manager and HB Worker) is released under the [MIT License](LICENSE). It uses, downloads or redistributes the following third-party software. Each component remains the property of its authors and is covered by its own license.

HB Cluster is an independent project. It is **not affiliated with, endorsed by or sponsored by** the HandBrake, FFmpeg, MKVToolNix or 7-Zip projects. All product names and trademarks belong to their respective owners.

## Encoding tools

The Manager downloads these tools from their official release pages (or the Linux images install them from Debian) and hands them to the workers **unmodified**. When you include them in the Windows worker package, the package redistributes them as well.

| Tool | Used for | License | Source code |
|---|---|---|---|
| [HandBrake / HandBrakeCLI](https://handbrake.fr/) | video encoding | GPL-2.0 | [github.com/HandBrake/HandBrake](https://github.com/HandBrake/HandBrake) – source archive of every release on the [releases page](https://github.com/HandBrake/HandBrake/releases) |
| [FFmpeg / FFprobe](https://ffmpeg.org/) | analysis, subtitles, audio post-processing | LGPL-2.1+ or GPL-2.0+ / GPL-3.0+ depending on the build (both Linux and Windows builds used are GPL builds) | [ffmpeg.org/download.html](https://ffmpeg.org/download.html); Linux & Windows builds: [BtbN/FFmpeg-Builds](https://github.com/BtbN/FFmpeg-Builds) (build scripts and sources), [gyan.dev](https://www.gyan.dev/ffmpeg/builds/) (source links per build) |
| [MKVToolNix](https://mkvtoolnix.download/) (`mkvmerge`, `mkvpropedit`) | Pre-Mux, track fixes | GPL-2.0 | [mkvtoolnix.download/source.html](https://mkvtoolnix.download/source.html), [codeberg.org/mbunkus/mkvtoolnix](https://codeberg.org/mbunkus/mkvtoolnix) |
| [7-Zip](https://www.7-zip.org/) (`7z`, `7zz`) / p7zip | unpacking `.iso` images | LGPL-2.1+ with the unRAR restriction; some parts BSD-3-Clause | [7-zip.org/download.html](https://www.7-zip.org/download.html), [github.com/ip7z/7zip](https://github.com/ip7z/7zip) |

**Docker images** (`hb-manager`, `hb-worker`) are based on Debian (`python:3.11-slim-bookworm`) and contain Debian packages, among them `handbrake-cli`, `ffmpeg`, `mkvtoolnix` and `p7zip-full`. The source code of every Debian package is available from [sources.debian.org](https://sources.debian.org/) and [snapshot.debian.org](https://snapshot.debian.org/); the license of each package is listed in `/usr/share/doc/<package>/copyright` inside the image.

## Manager and Linux worker (Python)

| Library | License |
|---|---|
| [Python](https://www.python.org/) | PSF License |
| [FastAPI](https://fastapi.tiangolo.com/) | MIT |
| [Uvicorn](https://www.uvicorn.org/) | BSD-3-Clause |
| [SQLAlchemy](https://www.sqlalchemy.org/) | MIT |
| [Pydantic](https://docs.pydantic.dev/) | MIT |
| [python-multipart](https://github.com/Kludex/python-multipart) | Apache-2.0 |
| [Requests](https://requests.readthedocs.io/) | Apache-2.0 |
| [psutil](https://github.com/giampaolo/psutil) | BSD-3-Clause |
| [smbprotocol](https://github.com/jborean93/smbprotocol) | MIT |
| [py_webauthn](https://github.com/duo-labs/py_webauthn) (passkeys) | BSD-3-Clause |
| [osslsigncode](https://github.com/mtrojnar/osslsigncode) (signs `HBWorker.exe` on Manager; also installed in Linux worker image) | GPL-3.0 |
| [staticx](https://github.com/JonathonReinhart/staticx) (packages built Linux tools into self-contained binaries; bootloader embedded in packaged binaries) | GPL-2.0-only |
| [patchelf](https://github.com/NixOS/patchelf) (ELF patching used by `staticx` in Linux worker image) | GPL-3.0+ |
| [SCons](https://scons.org/) (build system used by `staticx` in Linux worker image) | MIT |

Their own dependencies are installed by `pip` from PyPI (or `apt` from Debian) under their respective licenses.

## Windows worker

| Component | License |
|---|---|
| [Python](https://www.python.org/) – installed from python.org only if it is missing | PSF License |
| [Requests](https://requests.readthedocs.io/), [psutil](https://github.com/giampaolo/psutil), [Pillow](https://python-pillow.org/) – installed with `pip` on first start | Apache-2.0, BSD-3-Clause, MIT-CMU (HPND) |
| `HBWorker.exe` launcher – built with [MinGW-w64](https://www.mingw-w64.org/) | MinGW-w64 runtime: ZPL-2.1 / public domain; GCC runtime library exception |

## Web interface

| Library | License |
|---|---|
| [Vue.js](https://vuejs.org/) | MIT |
| [SortableJS](https://sortablejs.github.io/Sortable/) | MIT |
| [Tailwind CSS](https://tailwindcss.com/) (build-time, generated CSS) | MIT |

## Build-time only

These tools are used while building the Docker images or bundling web assets. They are not part of what HB Cluster runs or distributes: [Node.js](https://nodejs.org/) (MIT), GitHub Actions.

## Logo font

The HB Cluster and MasekLab logo files with text use [Inter](https://rsms.me/inter/) by Rasmus Andersson (SIL Open Font License 1.1), converted to outlines.
