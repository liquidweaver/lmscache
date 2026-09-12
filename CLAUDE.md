# LMS Cache — notes for working on this codebase

Read this before changing anything. The README explains the product for users; this file explains the code, the
protocols and the invariants, plus where the project currently stands. Deployment specifics for the owner's NAS live
outside this public repo (workspace `CLAUDE.md` and the git-ignored `deploy.env`); never add hostnames, share names,
accounts or secrets here.

## What it is

One shared LM Studio model library on a NAS, fed by the machines that use it. Models are downloaded and tried in
LM Studio on any machine; keepers are uploaded to the library from that machine; every other machine can then have
a quant **Cached** (a local copy), **Linked** (per-file symlinks into the SMB share) or **Not available**. The NAS
never downloads anything itself. The unit of everything is the **quant** (variant), id `publisher/repo@Q8_0`; MLX
repos are one variant, named from the repo (`@4bit`) or `@all`.

Three moving parts:

1. `lmscache/` — a FastAPI service in a Docker container on the NAS. Bind mounts: the shared folder at `/models`
   (library in `lmstudio/`, upload scratch in `.incoming/`), and a config folder at `/config` (settings, SQLite).
   Dependencies are FastAPI and uvicorn only.
2. The web UI in `lmscache/static/` — plain JS, no build step. Library matrix (quants × machines), Machines,
   Settings. No authentication by design: LAN only.
3. The **client script**, generated per machine by `GET /lmsc/<machine>.sh` from the template in `machines.py`
   and run with `bash -c "$(curl -fsSL …)"`. Nothing is installed on clients; every run fetches the current script.

## Code map

- `app.py` — routes only. `GET /api/state` is the single payload the UI renders from. `POST /api/machines/<name>/report`
  is the client's heartbeat: it takes the machine's file scan and *returns the plan* the script works from.
- `catalog.py` — the library is the folder tree (`config.LIBRARY_DIR`); `scan()` derives models and their variants
  from files, SQLite only adds metadata (`added_at`, `source`, `revision`). `place()` moves verified files from
  scratch into the library atomically (rename for a new repo, per-file replace otherwise). `detect_format()` decides
  gguf / mlx / safetensors; MLX is recognised from `config.json` (a `quantization` block with `group_size` and `bits`
  and no `quant_method`) or from "mlx" in the name.
- `util.py` — quant detection regex (`detect_quant`), `group_files` / `variants_for` (files → variants + shared files
  such as `mmproj` and README), `quant_rank` for ordering, id validation.
- `machines.py` — machine profiles (`models_dir`, `mount`, optional `smb_user`, `providers`), wanted states
  (`intents` table keyed by variant id), reports, `classify()` (per variant, per store: cached / linked / partial /
  absent, plus foreign local quants), `plan_text()` (the TSV the client parses), and `_CLIENT_TEMPLATE` (the bash
  script; `@@TOKEN@@` placeholders are substituted in `client_script()`).
- `uploads.py` — resumable uploads: `GET /api/upload/<repo>` reports bytes already received, `PUT …/<path>` appends
  from a `Content-Range` offset into `.incoming/`, `POST …/commit` verifies sizes and places the files.
- `config.py` — settings (public URL, SMB host/share/account/password) in `/config/settings.json`, mode 600.
- `db.py` — one SQLite connection behind a lock; tables `models`, `machines`, `intents`, `reports`.
- `events.py` — SSE nudges (`/api/events`); the UI refetches state on any event and every 20 s.
- `scripts/dev.sh` runs the service locally with data under `.dev/`; `scripts/deploy.sh sync|up|logs` deploys over SSH
  using values from `deploy.env` (copy `deploy.env.example`), rendering `docker-compose.yaml` with them.

## The report / plan protocol

The client scans its stores and POSTs JSON: `models` (the primary LM Studio-layout folder: one entry per
`publisher/repo` with `files: [{path, size, link}]`, `link: true` for a symlink, `hint: "mlx"` when config.json says
so) and `providers` (same shape per extra store, where `link` is `false` for data the store owns, or `"primary"`,
`"share"`, `"other"` for symlinks by target). The server stores it (`v: 2`) and answers with tab-separated lines:

```
P   kind  path  layout                 providers of this machine (flat | hfcache)
V   vid   bytes local wanted real      one per library variant; local ∈ cached|linked|partial|absent|-
F   vid   path  size                   files of that variant
PV  vid   kind  state real wanted      state of the variant in a provider; wanted=no means "remove it from here"
S   repo  path  size                   files shared by all quants of a repo (mmproj, README)
R   repo  revision                     known Hub revision (used to name Hugging Face cache snapshots)
X   vid   bytes source                 local quant the library lacks; source = primary or a provider kind
XF  vid   path  size                   its files
```

Wanted states set in the UI are per variant and per machine; the matrix shows *reported* state, and a wanted state
that differs is "pending" until a run applies it.

## The client script

Bash 3.2 compatible (macOS ships 3.2), no `set -u`, no associative arrays. Flow: `boot_providers` → `ensure_mount`
(macOS `mount_smbfs`, Linux `mount -t cifs`; respects an existing session on macOS) → `sync_state` (scan, POST,
parse plan) → interactive menu or a flag (`--apply`, `--apply --yes`, `--report`, `--upload repo@QUANT`).

The interactive menu **stages** everything: row changes, uploads (`u<n>`), the login-time mount (`m`). `c` commits
(uploads, then state changes, then the mount, then a report) and quits; `d` discards and quits; EOF discards.

Per variant: `do_cache` rsyncs exactly that quant's files (plus missing shared files) in place, with a progress line
from bytes landed; `do_link` makes per-file symlinks into the share; `do_remove` deletes the quant's files and the
folder when only shared files remain. A repo folder that is itself a symlink is first converted to per-file links
(`ensure_real_dir`) so quants stay independent.

Providers (`omlx` flat layout, `vllm` and `hfcache` in Hugging Face cache layout `models--org--repo/refs/main` +
`snapshots/<rev>/`): `adopt_variant` moves a provider's real copy into the primary folder, `project_variant` links
the quant into each provider that should hold it, `unproject_one` removes it. `consolidate` runs after every commit.
Rule: when a machine has both oMLX and a cache store, MLX quants live in oMLX's folder only (oMLX scans the cache too
and would list models twice). Unknown revisions use the snapshot name `lmscache`; vLLM then needs `HF_HUB_OFFLINE=1`.

## Invariants (do not break)

- Every removal goes through `safe_rm` / `safe_rm_under`: non-empty absolute path strictly inside the store root, no
  `..`, never the root itself. Repo ids and relative paths from the plan are validated (`valid_repo`, `valid_rel`).
- Any `find` on a folder that may sit under a dot directory (`~/.cache`, `~/.lmstudio`) must run relative:
  `( cd "$dir" && find . … )`. An absolute path makes the hidden-file filter drop everything.
- Scans skip hidden entries and in-progress downloads (`downloading_*`, `*.part`, `*.incomplete`, `*.tmp`).
- Uploads land in `.incoming/` on the same filesystem and are placed atomically; the library never shows a half
  model. Machines only read the share (SMB read-only account); all writes go through the HTTP API.
- The config folder is chmod 700 so the SMB password is invisible over the share. The per-machine script embeds the
  SMB read-only credentials, which is as private as the unauthenticated UI itself.
- Old reports (`v` missing) are treated as unknown, never as absent; the UI says "run the command again".

## Testing

Run `scripts/dev.sh` (data under `.dev/`), point a machine profile's `mount` at a folder that contains `lmstudio/`
(a symlink to `.dev/models` works) and its `models_dir` at a scratch folder, then drive the script with
`bash -c "$(curl -fsSL http://localhost:8080/lmsc/<name>.sh)" lmsc --apply --yes` or piped menu input such as
`printf '1\n3\nu1\nc\n' |`. Fake providers: a flat folder for oMLX, a `models--org--repo/{blobs,snapshots/<rev>,refs}`
tree for the cache layout, placed under a dot directory to catch the `find` trap. Use `bash -n` on the rendered
script and `node --check` on `static/app.js` before deploying.

## Where the project stands (September 2026)

Working end to end on the owner's fleet: per-quant library, uploads with resume and progress, staged menu, per-file
linking, providers (oMLX, vLLM, Hugging Face cache) with adoption and projection, machine renaming, session-aware
macOS mounts. Hub search and downloads were deliberately removed; the library is fed only by uploads.

Not done / ideas: a Windows client (per-file symlinks need Developer Mode there); LM Studio hub `model.yaml`
manifests are not carried into the library; per-file checksums are not verified after upload (sizes are); no
auth on the UI; `link_mode` (folder symlink vs per-file) was dropped in favour of per-file only.
