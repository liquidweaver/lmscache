# LMS Cache

Keep the open-weight models you want to keep **once**, on a NAS, in the exact folder layout [LM Studio](https://lmstudio.ai)
uses, and give every Mac or Linux machine on the LAN a single command that uploads keepers to the library and copies,
links or removes library quants locally. Try models in LM Studio as usual; promote the good ones to the library from
the machine that has them; the NAS holds the library and the machines hold only what they are using.

```
                       NAS (any Docker host with an SMB share; developed on a UGREEN NAS running UGOS Pro)
                    ┌────────────────────────────────────────────────────────────────┐
                    │  lmscache container (:8080)          /volume1/models             │
                    │   web UI: library, machines,          lmstudio/<publisher>/<repo>/…  ◀── SMB share, read-only
                    │   settings                            .incoming/  (uploads land here first)
                    │   resumable uploads from machines     lmscache/config/ (settings, database; mode 700)
                    └────────────────────────────────────────────────────────────────┘
                                          ▲                          ▲
     any browser ─── http://nas:8080 ─────┘                          │ SMB
                                                                     │
     each Mac / Linux box:  bash -c "$(curl -fsSL http://nas:8080/lmsc/<machine>.sh)"
       a small interactive script, fetched fresh every run: mounts the share, shows local vs wanted state,
       copies (Cached), symlinks (Linked) or removes models in ~/.lmstudio/models, uploads models the
       library lacks, and reports what is on the machine back to the NAS.
```

## What it does

- **One library, LM Studio's layout.** Models live at `lmstudio/<publisher>/<repo>/…` on the share, the same
  `publisher/repo` structure LM Studio keeps under `~/.lmstudio/models`, so a folder from the library drops straight
  into LM Studio with no conversion. GGUF and MLX both work.
- **Fed by the machines.** Download and try models in LM Studio as you always have. A quant that sits on one of your
  computers but not in the library shows up in that machine's script as `u1`, `u2`, and so on; one keystroke pushes it
  to the NAS, resumably, with a progress line. Nothing on the NAS talks to Hugging Face.
- **Quants are the unit.** A library entry is one loadable quant, `publisher/repo@Q8_0`, the same idea as LM Studio's
  own `@q8_0` variants. A GGUF repo can hold several; an MLX repo is one quant named in the repo.
- **A quants × machines matrix.** For every quant and every machine, the UI shows what is there and lets you pick
  what you want: **Not available**, **Cached locally** (a copy on that machine's disk) or **Linked** (per-file
  symlinks into the share, so the files load straight off the NAS). One folder can hold a cached quant next to a
  linked one.
- **One command per machine, nothing installed.** The command fetches a script generated for that machine and runs
  it. The script mounts the share, applies the wanted states, and reports back. Every run uses the current version.
- **No agents, no daemons.** The only long-running piece is the container on the NAS.

## How it works

### The library is the folder tree

The catalog is derived from the folders under `lmstudio/`. Anything you copy in by hand appears after a rescan.
A SQLite database in the config volume only adds what the folder cannot tell: when a model arrived and which machine
uploaded it, plus each machine's profile, wanted states and last report.

### Uploads

The client streams each file with `curl -T` to `PUT /api/upload/<publisher>/<repo>/<path>`. The server writes into
`.incoming/`, and `GET /api/upload/<publisher>/<repo>` tells the client how many bytes of each file it already has,
so an interrupted upload resumes with a `Content-Range` from that offset. When every file is complete the client
sends a manifest of paths and sizes; the server checks them, moves the files into the library atomically (a new repo
is a single rename, a new quant of an existing repo is placed file by file), and records which machine it came from.
Dotfiles are skipped and paths are confined to the model folder.

### Machines, wanted state and reported state

A machine is just a profile: a name, macOS or Linux, the LM Studio models folder, and where the share is mounted.
Clicking a state in the matrix records what you **want** for a quant there. The script on that machine is what
makes it true. Every run starts by scanning the models folder, file by file, and posting that **report**; the NAS
classifies each library quant from its own files: all present as real files is Cached, all present as symlinks is
Linked, some missing or short is *partial*, none is Not available. A wanted state that differs shows as pending.
Files in a repo folder that belong to no library quant, say a Q4 you downloaded yourself next to the library's
Q8, appear as a local quant you can upload. Empty folders are ignored.

### The client script

`GET /lmsc/<machine>.sh` renders a bash script with the machine's paths, the share name and the read-only SMB
account baked in. It

1. mounts the share if needed (`mount_smbfs` on macOS, `mount -t cifs` on Linux),
2. scans the local models folder and posts it (`POST /api/machines/<name>/report`), receiving the plan: every library
   quant with its files, local and wanted state, plus local quants the library lacks,
3. prints a table of Local versus Wanted per quant,
4. offers a menu: apply all wanted changes, change one quant, upload a local quant, install a login-time mount, report only,
5. reports the resulting state back.

Cached copies rsync exactly that quant's files (plus shared files such as a vision projector) in place, with resume.
Linked creates per-file symlinks into the share; a folder that is itself a symlink is first turned into a folder of
per-file links so quants stay independent. Removing real files always asks first, and every removal is confined to
paths inside the models folder that the plan named. Uploads and copies show one updating line with percent, bytes,
speed and time left, measured from bytes that actually landed.

### What is protected

- The share is read-only for the account machines mount with; only the container writes to it.
- The config folder with the SMB password and the database is mode 700, so it is invisible over SMB.
- The script only touches paths inside the machine's models folder, and only for library entries.
- The web UI has no authentication. Keep it on the LAN. Anyone on the LAN can queue downloads or delete library
  entries, and can read the per-machine script, which carries the read-only SMB credentials.

## How to best use it

**Setting up**

1. Deploy (below), open the UI, and in Settings enter the SMB share name, the read-only account and its password.
2. Add each computer on the Machines page. Defaults are right for a standard LM Studio install.
3. On each computer, run its one-liner once and choose **m** so the share mounts at login and is re-checked every
   five minutes. Linked models depend on that mount.

**Choosing a state per machine**

| Machine | Good default | Why |
|---|---|---|
| Wired at 10GbE, or models too large to keep locally | Linked | Loads at network speed; a 250 GB model takes a few minutes from an HDD pool |
| Wired at 1GbE | Cached | Loading over the wire would take about a minute per 6 GB, every time |
| Wi-Fi laptops | Cached | Copy once, run from the local SSD |
| Anything you are done with | Not available | Frees the local disk; the library still has it |

**Adding models**

- Download in LM Studio on whichever machine you are testing on. When a model earns its keep, run that machine's
  one-liner: the quant appears under "Local quants not in the library" and on the machine's card. Type its label, such
  as `u2`, to upload it. The copy you already have then counts as Cached, and every other machine can now Cache or
  Link it.
- MLX repos upload as one quant; GGUF repos upload one quant at a time, plus a vision projector if the folder has one.

**Keeping the matrix truthful**

- Run the one-liner after changing things by hand in LM Studio, or `lmsc --report` from a cron job or login script.
- Renaming a machine on the Machines page keeps its wanted states and last report.
- Deleting a model from the library warns you which machines link to it; their symlinks would dangle.

**Non-interactive flags** (append after the one-liner):

| Flag | Effect |
|---|---|
| `lmsc --apply` | apply wanted changes without the menu, still asking before deleting local copies |
| `lmsc --apply --yes` | the same without asking |
| `lmsc --report` | only refresh this machine's column in the web UI |
| `lmsc --upload publisher/repo@QUANT` | upload a local quant the library lacks; rerun to resume |

**Troubleshooting**

- *Mount fails or the library folder is empty*: check that the SMB account has read access to the share
  (`smbutil view //account@nas` on macOS lists what it can see) and that the share name in Settings matches.
- *On a Mac, Finder's connection to the NAS switched to the read-only account*: macOS keeps one SMB session per
  server. Set that machine's SMB account override to the account you normally use; the script then mounts with it
  (password from Keychain or a prompt). With the global account, the script also detects an existing session and
  reuses its account automatically.
- *A model shows partial*: set Cached again; rsync resumes the copy.
- *Upload interrupted*: run the same command again; it continues from what the NAS already has.
- *Deploying says transfers are in progress*: an upload is running; wait, or `deploy.sh up --force`.

## Deploy on the NAS (UGOS Pro)

Layout on the NAS, everything inside one shared folder:

```
/volume1/models/                the SMB share
  lmstudio/                     the library, LM Studio's publisher/repo layout
  .incoming/                    download and upload scratch, same filesystem so the final move is atomic
  lmscache/
    src/                        this repository; UGOS's Docker project points at src/docker-compose.yaml
    config/                     settings.json (SMB password), lmscache.sqlite; chmod 700
```

1. **Shared folder.** In UGOS, create the shared folder and give a dedicated read-only account (here `lmscache`)
   read permission on it under Control Panel → Shared Folder → Permissions. Admin accounts keep read/write. The
   service itself writes through the bind mount, not SMB.
2. **Docker access over SSH.** UGOS only lets root talk to Docker. Once, as your admin user:
   `sudo usermod -aG docker $USER` and log in again.
3. **Copy and start.** Copy `deploy.env.example` to `deploy.env` and fill in your NAS host and paths. `scripts/deploy.sh sync`
   copies this folder to the NAS with tar over SSH and renders `docker-compose.yaml` there with those values;
   `scripts/deploy.sh up` builds the image and starts the container.
4. **Show it in the UGOS Docker app.** UGOS lists only projects created through its own UI. In Docker → Project →
   Create, name it `lmscache`, pick `<share>/lmscache/src` as the storage path (click the share row once so its
   subfolders load), paste `docker-compose.yaml`, and deploy. Later `deploy.sh up` rebuilds in place and the project
   stays registered.
5. Open `http://<your-nas>:8080`.

`LMSCACHE_UID` and `LMSCACHE_GID` in `deploy.env` must be able to write to the share; `1000:10` is the first UGOS admin
user, which is also what UGOS suggests as PUID and PGID. The container needs no internet access at all.

## Development

```
scripts/dev.sh            # runs on http://localhost:8080 with data under ./.dev (needs uv)
scripts/deploy.sh sync    # copy to the NAS (values from deploy.env)
scripts/deploy.sh up      # build and (re)start on the NAS; refuses while transfers are in flight, --force overrides
scripts/deploy.sh logs    # follow the container log
```

Layout: `lmscache/app.py` (API routes), `uploads.py` (resumable uploads from machines), `catalog.py` (folder scan,
quant grouping, atomic placement into the library), `machines.py` (profiles, wanted states, reports, classification and
the generated client script), `util.py` (quant detection and grouping), `static/` (the UI, plain JavaScript, no build
step). Dependencies: FastAPI and uvicorn, nothing else.

API in one breath: `GET /api/state` is everything the UI shows; `PUT /api/machines/<name>/models/<owner>/<repo>@<quant>`
records a wanted state; `POST /api/machines/<name>/report` takes the client's file scan and returns its plan; `GET`,
`PUT`, `POST …/commit` under `/api/upload/<owner>/<repo>` handle uploads; `DELETE /api/library/<owner>/<repo>?variant=`
removes a quant; `GET /api/events` streams change notifications; `GET /lmsc/<machine>.sh` is the client script.
