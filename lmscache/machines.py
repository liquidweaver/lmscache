"""Machine profiles, per-variant wanted states, machine reports, classification of what each machine holds,
and the per-machine client script. A variant is one loadable quant of a repo: publisher/repo@Q4_K_M."""

from __future__ import annotations

import re
import time
from urllib.parse import quote, urlsplit

from . import config, db
from .catalog import detect_format
from .util import is_mmproj, valid_repo_id, valid_variant_id, variants_for

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$")
STATES = ("absent", "cached", "linked")
# Other model stores on a machine. They never hold a second copy: quants are projected into them as symlinks to the
# primary (LM Studio layout) store, in each store's own layout.
PROVIDER_KINDS = {
    "omlx": {"label": "oMLX", "layout": "flat", "formats": ["mlx"], "default": "~/.omlx/models"},
    "vllm": {"label": "vLLM (Hugging Face cache)", "layout": "hfcache", "formats": ["safetensors"], "default": "~/.cache/huggingface/hub"},
    "hfcache": {"label": "Hugging Face cache (mlx-lm, transformers)", "layout": "hfcache", "formats": ["mlx", "safetensors"], "default": "~/.cache/huggingface/hub"},
}
OS_DEFAULTS = {
    "mac": {"models_dir": "~/.lmstudio/models", "mount": "/Users/Shared/lmscache"},
    "linux": {"models_dir": "~/.lmstudio/models", "mount": "/mnt/lmscache"},
}


# ----- profiles -----
def list_machines() -> list[dict]:
    rows = db.all_json("machines", "name", "data")
    return sorted(rows.values(), key=lambda m: m.get("created_at", 0))


def get(name: str) -> dict | None:
    return db.get_json("machines", "name", name)


def upsert(data: dict) -> dict:
    name = (data.get("name") or "").strip()
    if not NAME_RE.match(name):
        raise ValueError("machine name: letters, digits, dot, dash or underscore, up to 32 chars")
    os_ = data.get("os") or "mac"
    if os_ not in OS_DEFAULTS:
        raise ValueError("os must be mac or linux")
    existing = get(name) or {}
    profile = {
        "name": name,
        "os": os_,
        "models_dir": (data.get("models_dir") or existing.get("models_dir") or OS_DEFAULTS[os_]["models_dir"]).strip(),
        "mount": (data.get("mount") or existing.get("mount") or OS_DEFAULTS[os_]["mount"]).strip().rstrip("/"),
        "smb_user": (data.get("smb_user") or "").strip() or None,
        "providers": _clean_providers(data.get("providers") if data.get("providers") is not None else existing.get("providers")),
        "created_at": existing.get("created_at") or time.time(),
    }
    for key in ("models_dir", "mount"):
        if not profile[key].startswith(("/", "~")):
            raise ValueError(f"{key} must be an absolute path or start with ~")
    db.put_json("machines", "name", name, "data", profile)
    return profile


def _clean_providers(raw) -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()
    for entry in raw or []:
        kind = str((entry or {}).get("kind") or "")
        if kind not in PROVIDER_KINDS or kind in seen:
            continue
        path = (str((entry or {}).get("path") or "").strip() or PROVIDER_KINDS[kind]["default"]).rstrip("/")
        if not path.startswith(("/", "~")):
            raise ValueError(f"{PROVIDER_KINDS[kind]['label']} path must be absolute or start with ~")
        seen.add(kind)
        out.append({"kind": kind, "path": path})
    return out


def rename(old: str, new: str) -> dict:
    if not NAME_RE.match(new or ""):
        raise ValueError("machine name: letters, digits, dot, dash or underscore, up to 32 chars")
    if old == new:
        return get(old) or {}
    if get(new):
        raise ValueError(f"a machine named {new} already exists")
    profile = get(old)
    if not profile:
        raise KeyError(old)
    profile["name"] = new
    db.put_json("machines", "name", new, "data", profile)
    db.execute("DELETE FROM machines WHERE name = ?", (old,))
    db.execute("UPDATE intents SET machine = ? WHERE machine = ?", (new, old))
    db.execute("UPDATE reports SET machine = ? WHERE machine = ?", (new, old))
    return profile


def delete(name: str) -> None:
    db.execute("DELETE FROM machines WHERE name = ?", (name,))
    db.execute("DELETE FROM intents WHERE machine = ?", (name,))
    db.execute("DELETE FROM reports WHERE machine = ?", (name,))


# ----- wanted states (per variant) -----
def set_intent(machine: str, vid: str, state: str) -> None:
    if state not in STATES:
        raise ValueError("state must be absent, cached or linked")
    if not valid_variant_id(vid):
        raise ValueError("bad variant id")
    db.execute(
        "INSERT OR REPLACE INTO intents (machine, model_id, state, set_at) VALUES (?, ?, ?, ?)",
        (machine, vid, state, time.time()),
    )


def clear_intent(machine: str, vid: str) -> None:
    db.execute("DELETE FROM intents WHERE machine = ? AND model_id = ?", (machine, vid))


def clear_intents_for(vids: list[str]) -> None:
    for vid in vids:
        db.execute("DELETE FROM intents WHERE model_id = ?", (vid,))


def intents() -> dict[str, dict[str, dict]]:
    out: dict[str, dict[str, dict]] = {}
    for machine, vid, state, set_at in db.query("SELECT machine, model_id, state, set_at FROM intents"):
        out.setdefault(machine, {})[vid] = {"state": state, "set_at": set_at}
    return out


# ----- reports: what a machine actually holds -----
def _clean_path(path: str) -> str | None:
    parts = path.split("/")
    if not path or "\t" in path or "\n" in path or any(p in ("", ".", "..") or p.startswith(".") for p in parts):
        return None
    return path


def _clean_models(entries, link_values=(True, False)) -> list[dict]:
    models = []
    for entry in entries or []:
        mid = str((entry or {}).get("id") or "")
        if not valid_repo_id(mid):
            continue
        files = []
        for f in entry.get("files") or []:
            path = _clean_path(str(f.get("path") or ""))
            if path is None:
                continue
            link = f.get("link")
            if link is True or link in ("primary", "share", "other"):
                link = link if isinstance(link, str) else "share"
            else:
                link = False
            files.append({"path": path, "size": int(f.get("size") or 0), "link": link})
        models.append({"id": mid, "link": bool(entry.get("link")), "files": files})
    return models


def store_report(machine: str, payload: dict) -> dict:
    models = _clean_models(payload.get("models"))
    providers = []
    for prov in payload.get("providers") or []:
        kind = str((prov or {}).get("kind") or "")
        if kind not in PROVIDER_KINDS:
            continue
        providers.append({"kind": kind, "path": str(prov.get("path") or ""), "models": _clean_models(prov.get("models"))})
    report = {
        "v": 2,
        "at": time.time(),
        "free_bytes": int(payload.get("free_bytes") or 0),
        "total_bytes": int(payload.get("total_bytes") or 0),
        "models": models,
        "providers": providers,
    }
    db.put_json("reports", "machine", machine, "data", report, {"at": report["at"]})
    return report


def reports() -> dict[str, dict]:
    return db.all_json("reports", "machine", "data")


def _valid(report: dict | None) -> bool:
    return bool(report) and report.get("v") == 2


def report_summary(report: dict | None) -> dict | None:
    if not report:
        return None
    models = report.get("models") or []
    if isinstance(models, dict):  # reports from before the per-quant protocol
        count = len(models)
    else:
        count = sum(1 for m in models if isinstance(m, dict) and (m.get("link") or m.get("files")))
    return {
        "at": report.get("at", 0),
        "free_bytes": report.get("free_bytes", 0),
        "total_bytes": report.get("total_bytes", 0),
        "count": count,
        "stale": not _valid(report),
    }


def _foreign_variants(rid: str, fmt: str, files: list[dict], model: dict | None) -> list[dict]:
    """Local files that belong to no library variant, grouped into uploadable variants."""
    _, repo_name = rid.split("/", 1)
    variants, shared = variants_for(fmt, repo_name, files)
    lib_shared = {f["path"] for f in (model or {}).get("shared") or []}
    carry = [f for f in shared if is_mmproj(f["path"]) and f["path"] not in lib_shared] if fmt == "gguf" else []
    out = []
    for v in variants:
        if v["size"] <= 0:
            continue
        vfiles = list(v["files"]) + carry
        out.append({"id": f"{rid}@{v['key']}", "bytes": sum(f["size"] for f in vfiles), "files": [{"path": f["path"], "size": f["size"]} for f in vfiles]})
    return out


def _variant_state(v: dict, local: dict, folder_link: bool) -> tuple[str, int]:
    """State of one variant against the files a store reported: cached (all real), linked, partial or absent."""
    n = len(v["files"])
    real = link = 0
    real_bytes = 0
    for f in v["files"]:
        lf = local.get(f["path"])
        if folder_link or (lf and lf.get("link")):
            link += 1
        elif lf and lf["size"] == f["size"]:
            real += 1
            real_bytes += f["size"]
        elif lf:
            real_bytes += lf["size"]
    if n and real == n:
        return "cached", real_bytes
    if n and link == n:
        return "linked", real_bytes
    if real == 0 and link == 0 and real_bytes == 0:
        return "absent", 0
    return "partial", real_bytes


def compatible(kind: str, fmt: str) -> bool:
    return fmt in PROVIDER_KINDS.get(kind, {}).get("formats", [])


def classify(models: dict[str, dict], report: dict | None) -> tuple[dict[str, dict], list[dict]]:
    """Per library variant: reported state in the primary store, per-provider state, real bytes; plus local
    variants the library lacks, from the primary store or any provider."""
    states: dict[str, dict] = {}
    foreign: list[dict] = []
    seen_foreign: set[str] = set()
    valid = _valid(report)
    rep_models = {m["id"]: m for m in report.get("models", [])} if valid else {}
    providers = (report or {}).get("providers", []) if valid else []
    for mid, model in models.items():
        rm = rep_models.get(mid)
        local = {f["path"]: f for f in rm.get("files", [])} if rm else {}
        folder_link = bool(rm and rm.get("link"))
        for v in model["variants"]:
            if not valid:
                states[v["id"]] = {"reported": None, "bytes": 0, "providers": {}}
                continue
            state, real_bytes = _variant_state(v, local, folder_link)
            prov_states = {}
            for prov in providers:
                if not compatible(prov["kind"], model["format"]):
                    continue
                pm = next((m for m in prov["models"] if m["id"] == mid), None)
                plocal = {f["path"]: f for f in pm.get("files", [])} if pm else {}
                pstate, pbytes = _variant_state(v, plocal, bool(pm and pm.get("link")))
                prov_states[prov["kind"]] = {"state": "real" if pstate == "cached" else pstate, "bytes": pbytes}
            states[v["id"]] = {"reported": state, "bytes": real_bytes, "providers": prov_states}
        if rm and not folder_link and model["format"] == "gguf":
            known = {f["path"] for v in model["variants"] for f in v["files"]} | {f["path"] for f in model.get("shared") or []}
            extra = [f for f in rm["files"] if f["path"] not in known and not f.get("link")]
            for x in _foreign_variants(mid, "gguf", extra, model):
                x["source"] = "primary"
                foreign.append(x)
                seen_foreign.add(x["id"])
    stores = [("primary", rep_models)] + [(prov["kind"], {m["id"]: m for m in prov["models"]}) for prov in providers]
    for source, store in stores:
        for rid, rm in store.items():
            if rid in models or rm.get("link"):
                continue
            files = [f for f in rm["files"] if not f.get("link")]
            if not files:
                continue
            pub, repo = rid.split("/", 1)
            fmt = detect_format(pub, repo, [f["path"] for f in files], [])
            for x in _foreign_variants(rid, fmt, files, None):
                if x["id"] in seen_foreign:
                    continue
                x["source"] = source
                foreign.append(x)
                seen_foreign.add(x["id"])
    foreign.sort(key=lambda x: x["id"].lower())
    return states, foreign


def cells(models: dict[str, dict], machine_names: list[str], intents_map: dict, reports_map: dict) -> dict:
    out: dict[str, dict[str, dict]] = {}
    for name in machine_names:
        states, _ = classify(models, reports_map.get(name))
        my = intents_map.get(name, {})
        row: dict[str, dict] = {}
        for vid, st in states.items():
            intent = (my.get(vid) or {}).get("state")
            reported = st["reported"]
            row[vid] = {"reported": reported, "bytes": st["bytes"], "intent": intent, "pending": bool(intent) and (reported is None or intent != reported), "providers": st.get("providers", {})}
        out[name] = row
    return out


def foreign(models: dict[str, dict], reports_map: dict) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for name, rep in reports_map.items():
        _, extras = classify(models, rep)
        if extras:
            out[name] = extras
    return out


def plan_text(models: dict[str, dict], machine_name: str, report: dict | None, machine: dict | None = None) -> str:
    """What the client script works from. Tab separated lines:
    P kind path layout | V variant bytes local wanted real_bytes | F variant path size | PV variant kind state real_bytes
    | S repo path size | R repo revision | X foreign_variant bytes source | XF foreign_variant path size"""
    states, extras = classify(models, report)
    wanted = intents().get(machine_name, {})
    provs = (machine or {}).get("providers") or []
    lines: list[str] = []
    for prov in provs:
        lines.append("\t".join(["P", prov["kind"], prov["path"], PROVIDER_KINDS[prov["kind"]]["layout"]]))
    for m in sorted(models.values(), key=lambda m: m["id"].lower()):
        for v in m["variants"]:
            st = states.get(v["id"]) or {"reported": None, "bytes": 0, "providers": {}}
            want = (wanted.get(v["id"]) or {}).get("state") or "-"
            lines.append("\t".join(["V", v["id"], str(v["bytes"]), st["reported"] or "-", want, str(st["bytes"])]))
            for f in v["files"]:
                if _clean_path(f["path"]):
                    lines.append("\t".join(["F", v["id"], f["path"], str(f["size"])]))
            for prov in provs:
                if compatible(prov["kind"], m["format"]):
                    ps = (st.get("providers") or {}).get(prov["kind"]) or {"state": "-", "bytes": 0}
                    lines.append("\t".join(["PV", v["id"], prov["kind"], ps["state"] or "-", str(ps["bytes"])]))
        for f in m.get("shared") or []:
            if _clean_path(f["path"]):
                lines.append("\t".join(["S", m["id"], f["path"], str(f["size"])]))
        if m.get("revision"):
            lines.append("\t".join(["R", m["id"], str(m["revision"])]))
    for x in extras:
        lines.append("\t".join(["X", x["id"], str(x["bytes"]), x.get("source") or "primary"]))
        for f in x["files"]:
            lines.append("\t".join(["XF", x["id"], f["path"], str(f["size"])]))
    return "\n".join(lines) + ("\n" if lines else "")


# ----- client script -----
def base_url(request, settings: dict) -> str:
    if settings.get("public_url"):
        return settings["public_url"].rstrip("/")
    host = request.headers.get("host") or request.url.netloc
    return f"{request.url.scheme}://{host}"


def smb_host(request, settings: dict) -> str:
    if settings.get("smb_host"):
        return settings["smb_host"]
    host = request.headers.get("host") or request.url.netloc
    return urlsplit(f"//{host}").hostname or host


def _sh_path(path: str) -> str:
    if path.startswith("~"):
        path = "$HOME" + path[1:]
    return path.replace("\\", "\\\\").replace('"', '\\"').replace("`", "\\`")


def one_liner(machine: dict, base: str) -> str:
    return f'bash -c "$(curl -fsSL {base}/lmsc/{machine["name"]}.sh)"'


def client_script(machine: dict, base: str, host: str, settings: dict) -> str:
    share = settings.get("smb_share") or "models"
    user = machine.get("smb_user") or settings.get("smb_user") or "guest"
    user_source = "machine" if machine.get("smb_user") else "global"
    password = "" if user == "guest" else (settings.get("smb_password") or "")
    values = {
        "NAS": base,
        "MACHINE": machine["name"],
        "OS": machine["os"],
        "MODELS_DIR": _sh_path(machine["models_dir"]),
        "MOUNT": _sh_path(machine["mount"]),
        "SMB_HOST": host,
        "SMB_SHARE": share,
        "SMB_SHARE_URL": quote(share, safe=""),
        "SMB_SHARE_FSTAB": share.replace(" ", "\\040"),
        "SMB_USER": user,
        "SMB_USER_SOURCE": user_source,
        "SMB_USER_URL": quote(user, safe=""),
        "SMB_PASS": password.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$").replace("`", "\\`"),
        "SMB_PASS_URL": quote(password, safe=""),
        "PROVIDERS": "\n".join(
            "\t".join([prov["kind"], _sh_path(prov["path"]), PROVIDER_KINDS[prov["kind"]]["layout"]]) for prov in machine.get("providers") or []
        ),
    }
    script = _CLIENT_TEMPLATE
    for key, val in values.items():
        script = script.replace("@@" + key + "@@", val)
    return script


_CLIENT_TEMPLATE = r'''#!/usr/bin/env bash
# LMS Cache client for "@@MACHINE@@". Fetched fresh from @@NAS@@ on every run; nothing is installed.
# Run:  bash -c "$(curl -fsSL @@NAS@@/lmsc/@@MACHINE@@.sh)"
# In the menu every action is staged; c commits them all and quits, d discards them and quits.
# Flags (append after the closing quote):  lmsc --apply               apply wanted changes without the menu
#                                          lmsc --apply --yes         the same, without asking before deleting local copies
#                                          lmsc --report              only report local state
#                                          lmsc --upload repo@QUANT   upload a local quant that the library lacks
NAS="@@NAS@@"; MACHINE="@@MACHINE@@"; OS="@@OS@@"
MODELS_DIR="@@MODELS_DIR@@"; MOUNT="@@MOUNT@@"; LIB="$MOUNT/lmstudio"
SMB_HOST="@@SMB_HOST@@"; SMB_SHARE="@@SMB_SHARE@@"; SMB_SHARE_URL="@@SMB_SHARE_URL@@"; SMB_SHARE_FSTAB="@@SMB_SHARE_FSTAB@@"
SMB_USER="@@SMB_USER@@"; SMB_USER_URL="@@SMB_USER_URL@@"; SMB_PASS="@@SMB_PASS@@"; SMB_PASS_URL="@@SMB_PASS_URL@@"; SMB_USER_SOURCE="@@SMB_USER_SOURCE@@"
PROVIDERS_BOOT="@@PROVIDERS@@"

AUTO=0; REPORT_ONLY=0; YES=0; UPLOAD=""
while [ $# -gt 0 ]; do
  case "$1" in
    --apply) AUTO=1;; --report) REPORT_ONLY=1;; --yes|-y) YES=1;;
    --upload) if [ $# -gt 1 ]; then shift; UPLOAD="$1"; fi;;
  esac
  shift
done
if [ -t 1 ]; then B=$'\033[1m'; D=$'\033[2m'; Y=$'\033[33m'; G=$'\033[32m'; R=$'\033[0m'; else B=""; D=""; Y=""; G=""; R=""; fi
TAB=$'\t'

# library variants (one loadable quant each) as parallel arrays, filled by sync_state
N=0; NX=0; PEND=0
VID=(); VBYTES=(); VLOCAL=(); VWANT=(); VREAL=(); VFILES=()
XID=(); XBYTES=(); XFILES=(); XSRC=(); SFILES=""
NP=0; PK=(); PP=(); PL=()          # providers on this machine: kind, path, layout
VPROV=()                          # per variant: provider states as lines kind<TAB>state<TAB>real_bytes
RREV=""                           # known revisions: repo<TAB>rev lines (used for Hugging Face cache folder names)
# the interactive menu stages everything; nothing runs until "commit and quit"
SN=0; SIDS=(); SSTATES=()      # staged state changes, by variant id
UN=0; UIDS=()                  # staged uploads, by foreign variant id
STAGE_MOUNT=0

hb() { awk -v b="${1:-0}" 'BEGIN{split("B KB MB GB TB",u," ");i=1;while(b>=1024&&i<5){b/=1024;i++}; if(i==1)printf "%d %s",b,u[i]; else printf "%.1f %s",b,u[i]}'; }
esc() { printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'; }
urlencode() { local LC_ALL=C s="$1" i c out=""; for ((i=0;i<${#s};i++)); do c="${s:i:1}"; case "$c" in [a-zA-Z0-9./_@-]) out="$out$c";; *) out="$out$(printf '%%%02X' "'$c")";; esac; done; printf '%s' "$out"; }
upload_have() { curl -fsS -m 10 "$NAS/api/upload/$1" 2>/dev/null | awk -F'\t' '{s+=$2} END{printf "%.0f", s+0}'; }
fmt_time() {
  local s=${1:-0}
  if [ "$s" -ge 3600 ]; then printf '%dh %02dm' $((s/3600)) $(((s%3600)/60))
  elif [ "$s" -ge 60 ]; then printf '%dm %02ds' $((s/60)) $((s%60))
  else printf '%ds' "$s"; fi
}
# watch_progress <pid> <total bytes> <command that prints bytes done...>: one updating line while the process runs (terminal only)
watch_progress() {
  local pid="$1" total="${2:-0}"; shift 2
  local done prev=-1 prev_t now dt inst rate=0 pct eta
  [ -t 1 ] || return 0
  prev_t=$(date +%s)
  while kill -0 "$pid" 2>/dev/null; do
    sleep 2
    done=$("$@" 2>/dev/null); done=${done:-0}
    now=$(date +%s); dt=$((now-prev_t)); [ "$dt" -gt 0 ] || dt=1
    if [ "$prev" -ge 0 ]; then
      inst=$(( (done-prev)/dt )); [ "$inst" -lt 0 ] && inst=0
      if [ "$rate" -eq 0 ]; then rate=$inst; else rate=$(( (rate*7 + inst*3)/10 )); fi
    fi
    prev=$done; prev_t=$now
    pct=0; [ "$total" -gt 0 ] && pct=$(( done*100/total )); [ "$pct" -gt 100 ] && pct=100
    if [ "$total" -gt 0 ] && [ "$done" -ge "$total" ]; then eta="finishing"
    elif [ "$rate" -gt 0 ] && [ "$total" -gt "$done" ]; then eta="$(fmt_time $(( (total-done)/rate ))) left"
    else eta="estimating..."; fi
    printf '\r  %3d%%  %s of %s   %s/s   %s          ' "$pct" "$(hb "$done")" "$(hb "$total")" "$(hb "$rate")" "$eta"
  done
  printf '\r%90s\r' ''
}
# ----- safety: every destructive step is confined to the models folder -----
valid_repo() { [[ "$1" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$ ]]; }
valid_rel() {  # relative file path from the plan: no empty, absolute, hidden or .. segments
  case "$1" in ""|/*|..|../*|*/..|*/../*|*//*|.*|*/.*) return 1;; esac; return 0
}
safe_rm() {  # remove a file, symlink or folder, only strictly inside the models folder
  local t="$1" root="${MODELS_DIR%/}"
  [ -n "$root" ] && [ -n "$t" ] || { echo "refusing to remove: empty path" >&2; return 1; }
  case "$root" in /?*) ;; *) echo "refusing to remove anything: models folder is '$root'" >&2; return 1;; esac
  case "$t" in "$root"/?*) ;; *) echo "refusing to remove $t: outside $root" >&2; return 1;; esac
  case "$t" in *"/../"*|*"/.."|*"//"*) echo "refusing to remove $t: suspicious path" >&2; return 1;; esac
  if [ -L "$t" ] || [ -f "$t" ]; then rm -f "$t"; elif [ -d "$t" ]; then rm -rf "$t"; fi
}
list_bytes() {  # $1 = dir, $2 = file with relative paths -> bytes of those files present as real files
  local d="$1" p s=0 n
  while IFS= read -r p; do if [ -f "$d/$p" ] && [ ! -L "$d/$p" ]; then n=$(wc -c < "$d/$p" | tr -d ' '); s=$((s+n)); fi; done < "$2"
  echo "$s"
}

# ----- share mount -----
smb_url() {
  if [ "$SMB_USER" = guest ]; then printf '//guest@%s/%s' "$SMB_HOST" "$SMB_SHARE_URL"
  elif [ -n "$SMB_PASS_URL" ]; then printf '//%s:%s@%s/%s' "$SMB_USER_URL" "$SMB_PASS_URL" "$SMB_HOST" "$SMB_SHARE_URL"
  else printf '//%s@%s/%s' "$SMB_USER_URL" "$SMB_HOST" "$SMB_SHARE_URL"; fi
}
cifs_cred() {
  if [ "$SMB_USER" = guest ]; then printf 'guest'
  elif [ -n "$SMB_PASS" ]; then printf 'username=%s,password=%s' "$SMB_USER" "$SMB_PASS"
  else printf 'username=%s' "$SMB_USER"; fi
}
mac_session_user() {  # account of an existing SMB connection from this Mac to the NAS, if any (macOS keeps one session per server)
  mount 2>/dev/null | awk -v h="$(printf '%s' "$SMB_HOST" | tr 'A-Z' 'a-z')" '
    $1 ~ /^\/\// { split(substr($1, 3), a, "@"); if (length(a) == 2) { split(a[2], b, "/"); if (tolower(b[1]) == h) { print a[1]; exit } } }'
}
adopt_mac_session() {  # with the global default account, reuse whatever account this Mac is already connected with
  local u
  [ "$OS" = mac ] && [ "$SMB_USER_SOURCE" = global ] || return 0
  u=$(mac_session_user)
  [ -n "$u" ] && [ "$u" != "$SMB_USER" ] || return 0
  printf '%sThis Mac is already connected to %s as %s; using that account for the library mount so the existing connection is kept (password from your Keychain).%s\n' "$D" "$SMB_HOST" "$u" "$R"
  SMB_USER="$u"; SMB_USER_URL=$(urlencode "$u"); SMB_PASS=""; SMB_PASS_URL=""
}
ensure_mount() {
  [ -d "$LIB" ] && return 0
  adopt_mac_session
  echo "Mounting //$SMB_HOST/$SMB_SHARE at $MOUNT as $SMB_USER ..."
  if [ "$OS" = mac ]; then
    mkdir -p "$MOUNT" 2>/dev/null || true
    if [ "$SMB_USER" = guest ] || [ -n "$SMB_PASS" ]; then mount_smbfs -N -o soft,nobrowse "$(smb_url)" "$MOUNT"
    else mount_smbfs -o soft,nobrowse "$(smb_url)" "$MOUNT"; fi
  else
    sudo mkdir -p "$MOUNT"
    sudo mount -t cifs -o "$(cifs_cred),ro,soft,vers=3.1.1,uid=$(id -u),gid=$(id -g),iocharset=utf8" "//$SMB_HOST/$SMB_SHARE" "$MOUNT"
  fi
  [ -d "$LIB" ]
}

# ----- symlink inspection and provider layouts -----
resolve_link() {  # follow a symlink chain (up to 8 hops); prints the final regular file, or nothing
  local p="$1" t i
  for ((i=0;i<8;i++)); do
    if [ ! -L "$p" ]; then [ -f "$p" ] && printf '%s' "$p"; return; fi
    t=$(readlink "$p"); case "$t" in /*) ;; *) t="$(dirname "$p")/$t";; esac; p="$t"
  done
}
link_kind() {  # $1 = symlink, $2 = its store root -> primary | share | real (data inside that store, e.g. a Hugging Face blob) | other
  local t r; t=$(readlink "$1")
  case "$t" in /*) ;; *) t="$(dirname "$1")/$t";; esac
  case "$t" in "$MODELS_DIR"/*) echo primary; return;; "$LIB"/*|"$MOUNT"/*) echo share; return;; esac
  r=$(resolve_link "$1")
  if [ -n "$r" ]; then case "$r" in "$2"/*) echo real; return;; "$MODELS_DIR"/*) echo primary; return;; "$LIB"/*|"$MOUNT"/*) echo share; return;; esac; fi
  echo other
}
expand_home() { case "$1" in "~"*) printf '%s%s' "$HOME" "${1#\~}";; *) printf '%s' "$1";; esac; }
prov_index() { local i; for ((i=0;i<NP;i++)); do [ "${PK[i]}" = "$1" ] && { echo "$i"; return; }; done; echo -1; }
rev_for() { printf '%s' "$RREV" | awk -F"$TAB" -v r="$1" '$1==r{print $2; exit}'; }
prov_base() { printf '%s/models--%s' "${PP[$1]}" "${2//\//--}"; }   # hfcache: $1 = provider index, $2 = repo
prov_rev() {  # revision folder to use in a Hugging Face cache for a repo: the cache's own, else the library's, else a fixed name
  local base rev; base=$(prov_base "$1" "$2")
  if [ -f "$base/refs/main" ]; then rev=$(head -c 200 "$base/refs/main" | tr -d '[:space:]'); [ -n "$rev" ] && { printf '%s' "$rev"; return; }; fi
  rev=$(rev_for "$2"); printf '%s' "${rev:-lmscache}"
}
prov_repo_dir() {  # $1 = provider index, $2 = repo -> the folder that holds this repo's files in that store
  if [ "${PL[$1]}" = hfcache ]; then printf '%s/snapshots/%s' "$(prov_base "$1" "$2")" "$(prov_rev "$1" "$2")"; else printf '%s/%s' "${PP[$1]}" "$2"; fi
}
safe_rm_under() {  # like safe_rm, confined to an arbitrary store root ($1) instead of the models folder
  local root="${1%/}" t="$2"
  [ -n "$root" ] && [ -n "$t" ] || { echo "refusing to remove: empty path" >&2; return 1; }
  case "$root" in /?*) ;; *) echo "refusing to remove under '$root'" >&2; return 1;; esac
  case "$t" in "$root"/?*) ;; *) echo "refusing to remove $t: outside $root" >&2; return 1;; esac
  case "$t" in *"/../"*|*"/.."|*"//"*) echo "refusing to remove $t: suspicious path" >&2; return 1;; esac
  if [ -L "$t" ] || [ -f "$t" ]; then rm -f "$t"; elif [ -d "$t" ]; then rm -rf "$t"; fi
}

# ----- scan local folders and exchange with the NAS -----
emit_store_model() {  # $1 = id, $2 = folder, $3 = store root ; prints one JSON model object for a provider store
  local id="$1" dir="$2" root="$3" p rel size link ffirst=1 kind r
  printf '{"id":"%s","link":false,"files":[' "$(esc "$id")"
  while IFS= read -r p; do
    [ -n "$p" ] || continue
    rel=${p#"$dir"/}
    if [ -L "$p" ]; then
      kind=$(link_kind "$p" "$root")
      if [ "$kind" = real ]; then r=$(resolve_link "$p"); size=$(wc -c < "$r" | tr -d ' '); link=false; else size=0; link="\"$kind\""; fi
    else link=false; size=$(wc -c < "$p" | tr -d ' '); fi
    [ $ffirst -eq 1 ] || printf ','; ffirst=0
    printf '{"path":"%s","size":%s,"link":%s}' "$(esc "$rel")" "${size:-0}" "$link"
  done <<< "$(find "$dir" \( -type f -o -type l \) ! -name '.*' ! -path '*/.*' 2>/dev/null | sort)"
  printf ']}'
}

scan_provider_json() {  # $1 = provider index ; prints {"kind":..,"path":..,"models":[...]}
  local i="$1" root="${PP[$1]}" pub repo base id rev first=1
  printf '{"kind":"%s","path":"%s","models":[' "${PK[i]}" "$(esc "$root")"
  if [ -d "$root" ]; then
    if [ "${PL[i]}" = flat ]; then
      for pub in "$root"/*/; do
        pub=${pub%/}; [ -d "$pub" ] || continue
        case "${pub##*/}" in .*|models--*) continue;; esac
        for repo in "$pub"/*; do
          [ -d "$repo" ] || continue; case "${repo##*/}" in .*) continue;; esac
          [ $first -eq 1 ] || printf ','; first=0
          emit_store_model "${pub##*/}/${repo##*/}" "$repo" "$root"
        done
      done
    else
      for base in "$root"/models--*; do
        [ -d "$base" ] || continue
        id=${base##*/models--}; id=${id/--//}
        rev=""; [ -f "$base/refs/main" ] && rev=$(head -c 200 "$base/refs/main" | tr -d '[:space:]')
        [ -n "$rev" ] && [ -d "$base/snapshots/$rev" ] || rev=$(ls -t "$base/snapshots" 2>/dev/null | head -1)
        [ -n "$rev" ] && [ -d "$base/snapshots/$rev" ] || continue
        [ $first -eq 1 ] || printf ','; first=0
        emit_store_model "$id" "$base/snapshots/$rev" "$root"
      done
    fi
  fi
  printf ']}'
}

scan_json() {  # every publisher/repo folder with its files (path, size, whether it is a symlink), plus each provider store
  local pub repo p rel size link first=1 ffirst free total here i
  here=$(pwd)
  cd "$MODELS_DIR" 2>/dev/null || { printf '{"models":[]}'; return; }
  free=$(df -Pk . | awk 'NR==2{printf "%.0f", $4*1024}'); total=$(df -Pk . | awk 'NR==2{printf "%.0f", $2*1024}')
  printf '{"free_bytes":%s,"total_bytes":%s,"models":[' "${free:-0}" "${total:-0}"
  for pub in */; do
    pub=${pub%/}; [ -d "$pub" ] || continue
    case "$pub" in .*) continue;; esac
    for repo in "$pub"/*; do
      [ -e "$repo" ] || [ -L "$repo" ] || continue
      case "${repo#*/}" in .*) continue;; esac
      [ -d "$repo" ] || continue
      [ $first -eq 1 ] || printf ','; first=0
      if [ -L "$repo" ]; then printf '{"id":"%s","link":true,"files":[]}' "$(esc "$repo")"; continue; fi
      printf '{"id":"%s","link":false,"files":[' "$(esc "$repo")"
      ffirst=1
      while IFS= read -r p; do
        [ -n "$p" ] || continue
        rel=${p#"$repo"/}
        if [ -L "$p" ]; then link=true; size=0; else link=false; size=$(wc -c < "$p" | tr -d ' '); fi
        [ $ffirst -eq 1 ] || printf ','; ffirst=0
        printf '{"path":"%s","size":%s,"link":%s}' "$(esc "$rel")" "${size:-0}" "$link"
      done <<< "$(find "$repo" \( -type f -o -type l \) ! -name '.*' ! -path '*/.*' 2>/dev/null | sort)"
      printf ']}'
    done
  done
  printf '],"providers":['
  for ((i=0;i<NP;i++)); do [ $i -eq 0 ] || printf ','; scan_provider_json "$i"; done
  printf ']}'
  cd "$here"
}

parse_plan() {
  local tag a b c d e cur=-1 xcur=-1 i np=0
  N=0; NX=0; VID=(); VBYTES=(); VLOCAL=(); VWANT=(); VREAL=(); VFILES=(); VPROV=(); XID=(); XBYTES=(); XFILES=(); XSRC=(); SFILES=""; RREV=""
  PK=(); PP=(); PL=()
  while IFS="$TAB" read -r tag a b c d e; do
    case "$tag" in
      P) PK[np]="$a"; PP[np]="$(expand_home "$b")"; PL[np]="$c"; np=$((np+1));;
      V) VID[N]="$a"; VBYTES[N]="${b:-0}"; VLOCAL[N]="$c"; VWANT[N]="$d"; VREAL[N]="${e:-0}"; VFILES[N]=""; VPROV[N]=""; cur=$N; N=$((N+1));;
      F) [ $cur -ge 0 ] && VFILES[cur]="${VFILES[cur]}$b$TAB$c"$'\n';;
      PV) [ $cur -ge 0 ] && VPROV[cur]="${VPROV[cur]}$b$TAB$c$TAB${d:-0}"$'\n';;
      S) SFILES="$SFILES$a$TAB$b$TAB$c"$'\n';;
      R) RREV="$RREV$a$TAB$b"$'\n';;
      X) XID[NX]="$a"; XBYTES[NX]="${b:-0}"; XSRC[NX]="${c:-primary}"; XFILES[NX]=""; xcur=$NX; NX=$((NX+1));;
      XF) [ $xcur -ge 0 ] && XFILES[xcur]="${XFILES[xcur]}$b$TAB$c"$'\n';;
    esac
  done <<< "$1"
  NP=$np
  for ((i=0;i<N;i++)); do [ "${VWANT[i]}" = "-" ] && VWANT[i]=""; [ "${VLOCAL[i]}" = "-" ] && VLOCAL[i]="absent"; done
}

boot_providers() {  # the providers this machine's profile lists, so the very first scan covers them
  local k p l; NP=0; PK=(); PP=(); PL=()
  while IFS="$TAB" read -r k p l; do [ -n "$k" ] || continue; PK[NP]="$k"; PP[NP]="$(expand_home "$p")"; PL[NP]="$l"; NP=$((NP+1)); done <<< "$PROVIDERS_BOOT"
}

sync_state() {  # report local state (primary store and providers), receive the plan
  local plan
  plan=$(scan_json | curl -fsS -X POST -H 'Content-Type: application/json' --data-binary @- "$NAS/api/machines/$MACHINE/report") || { echo "Cannot reach LMS Cache at $NAS" >&2; return 1; }
  parse_plan "$plan"
  return 0
}

vindex() { local i; for ((i=0;i<N;i++)); do [ "${VID[i]}" = "$1" ] && { echo "$i"; return; }; done; echo -1; }
xindex() { local i; for ((i=0;i<NX;i++)); do [ "${XID[i]}" = "$1" ] && { echo "$i"; return; }; done; echo -1; }
staged_for() { local i; for ((i=0;i<SN;i++)); do [ "${SIDS[i]}" = "$1" ] && { printf '%s' "${SSTATES[i]}"; return; }; done; }
stage_state() {  # variant id, state
  local i; for ((i=0;i<SN;i++)); do [ "${SIDS[i]}" = "$1" ] && { SSTATES[i]="$2"; return; }; done
  SIDS[SN]="$1"; SSTATES[SN]="$2"; SN=$((SN+1))
}
unstage_state() {
  local i j=0 ids=() sts=()
  for ((i=0;i<SN;i++)); do [ "${SIDS[i]}" = "$1" ] && continue; ids[j]="${SIDS[i]}"; sts[j]="${SSTATES[i]}"; j=$((j+1)); done
  SIDS=("${ids[@]}"); SSTATES=("${sts[@]}"); SN=$j
}
upload_staged() { local i; for ((i=0;i<UN;i++)); do [ "${UIDS[i]}" = "$1" ] && return 0; done; return 1; }
effective_want() { local st; st=$(staged_for "${VID[$1]}"); if [ -n "$st" ]; then printf '%s' "$st"; else printf '%s' "${VWANT[$1]}"; fi; }
is_pending() { local w l="${VLOCAL[$1]}"; w=$(effective_want "$1"); [ -n "$w" ] && [ "$w" != "$l" ]; }
shared_for() { printf '%s' "$SFILES" | awk -F"$TAB" -v r="$1" '$1==r{print $2 "\t" $3}'; }
repo_files() {  # all library files of a repo: every variant plus shared, as path<TAB>size lines
  local i; for ((i=0;i<N;i++)); do [ "${VID[i]%@*}" = "$1" ] && printf '%s' "${VFILES[i]}"; done; shared_for "$1"
}

render() {
  local i mark w l c free repo quant
  free=$(df -Pk "$MODELS_DIR" 2>/dev/null | awk 'NR==2{printf "%.0f", $4*1024}')
  printf '\n%sLMS Cache%s · %s · library at %s · %s free locally\n\n' "$B" "$R" "$MACHINE" "$LIB" "$(hb "${free:-0}")"
  printf '%s  %3s  %-46s %-11s %10s  %-8s %-8s %s%s\n' "$D" "#" "Model" "Quant" "Size" "Local" "Wanted" "$([ $NP -gt 0 ] && printf 'Also in')" "$R"
  PEND=0
  for ((i=0;i<N;i++)); do
    repo="${VID[i]%@*}"; quant="${VID[i]##*@}"
    w=$(effective_want "$i"); l="${VLOCAL[i]}"; mark=" "; c=""
    if [ -n "$(staged_for "${VID[i]}")" ]; then mark=">"; c="$Y"
    elif is_pending "$i"; then mark="*"; c="$Y"; PEND=$((PEND+1)); fi
    [ "$l" = absent ] && l="-"
    printf '%s%s %3d  %-46.46s %-11.11s %10s  %-8s %-8s %s%s\n' "$c" "$mark" "$((i+1))" "$repo" "$quant" "$(hb "${VBYTES[i]}")" "$l" "${w:--}" "$(prov_summary "$i")" "$R"
  done
  [ "$N" -eq 0 ] && echo "  (the library is empty; upload a model you want to keep with u<number>)"
  if [ $NX -gt 0 ]; then
    printf '\n%sLocal quants not in the library%s (type a label such as u1 to stage an upload; type it again to unstage):\n' "$D" "$R"
    for ((i=0;i<NX;i++)); do printf '  u%-2d %-58.58s %10s%s\n' "$((i+1))" "${XID[i]}" "$(hb "${XBYTES[i]}")" "$(upload_staged "${XID[i]}" && printf '  %s> staged%s' "$Y" "$R")"; done
  fi
}

prov_summary() {  # short list of providers that hold this variant: kind, kind(real) for an unadopted local copy
  local k st b out=""
  while IFS="$TAB" read -r k st b; do
    [ -n "$k" ] || continue
    case "$st" in real) out="$out${out:+,}$k(copy)";; linked) out="$out${out:+,}$k";; partial) out="$out${out:+,}$k(part)";; esac
  done <<< "${VPROV[$1]}"
  printf '%s' "$out"
}

show_staged() {
  local parts=""
  [ $SN -gt 0 ] && parts="$SN state change(s)"
  [ $UN -gt 0 ] && parts="${parts:+$parts, }$UN upload(s)"
  [ $STAGE_MOUNT -eq 1 ] && parts="${parts:+$parts, }mount at login"
  [ -n "$parts" ] && printf '\n%s> Staged, runs on commit: %s%s\n' "$Y" "$parts" "$R"
  [ $PEND -gt 0 ] && printf '%s* %d change(s) wanted from the web UI, also applied on commit.%s\n' "$D" "$PEND" "$R"
  return 0
}

set_intent() { curl -fsS -X PUT -H 'Content-Type: application/json' -d "{\"state\":\"$2\"}" "$NAS/api/machines/$MACHINE/models/$1" >/dev/null 2>&1 || true; }

# ----- actions on one variant -----
ensure_real_dir() {  # $1 = repo. A folder that is itself a symlink to the share becomes a real folder of per-file links.
  local repo="$1" d="$MODELS_DIR/$1" path size
  valid_repo "$repo" || { echo "bad repo id '$repo'" >&2; return 1; }
  if [ -L "$d" ]; then
    safe_rm "$d" && mkdir -p "$d" || return 1
    while IFS="$TAB" read -r path size; do
      valid_rel "$path" || continue
      mkdir -p "$d/$(dirname "$path")" && ln -sfn "$LIB/$repo/$path" "$d/$path"
    done <<< "$(repo_files "$repo")"
  fi
  mkdir -p "$d"
}

do_cache() {  # copy this variant's files (and missing shared files) from the share
  local i="$1" vid="${VID[$1]}" repo d list path size total=0 pid rc
  repo="${vid%@*}"; d="$MODELS_DIR/$repo"
  valid_repo "$repo" || { echo "bad repo id '$repo'" >&2; return 1; }
  ensure_mount || return 1
  ensure_real_dir "$repo" || return 1
  list=$(mktemp)
  while IFS="$TAB" read -r path size; do
    valid_rel "$path" || continue
    [ -L "$d/$path" ] && safe_rm "$d/$path"
    printf '%s\n' "$path" >> "$list"; total=$((total+size))
  done <<< "${VFILES[i]}"
  while IFS="$TAB" read -r path size; do
    valid_rel "$path" || continue
    if [ -L "$d/$path" ] || [ ! -e "$d/$path" ]; then [ -L "$d/$path" ] && safe_rm "$d/$path"; printf '%s\n' "$path" >> "$list"; total=$((total+size)); fi
  done <<< "$(shared_for "$repo")"
  if command -v rsync >/dev/null; then
    rsync -a --inplace --partial --files-from="$list" "$LIB/$repo/" "$d/" & pid=$!
  else
    ( while IFS= read -r path; do mkdir -p "$d/$(dirname "$path")"; cp "$LIB/$repo/$path" "$d/$path"; done < "$list" ) & pid=$!
  fi
  watch_progress "$pid" "$total" list_bytes "$d" "$list"
  wait "$pid"; rc=$?
  rm -f "$list"; return $rc
}

do_link() {  # per-file symlinks into the share for this variant (and missing shared files)
  local i="$1" vid="${VID[$1]}" repo d path size
  repo="${vid%@*}"; d="$MODELS_DIR/$repo"
  valid_repo "$repo" || { echo "bad repo id '$repo'" >&2; return 1; }
  ensure_mount || return 1
  ensure_real_dir "$repo" || return 1
  while IFS="$TAB" read -r path size; do
    valid_rel "$path" || continue
    mkdir -p "$d/$(dirname "$path")" || return 1
    [ -e "$d/$path" ] || [ -L "$d/$path" ] && { safe_rm "$d/$path" || return 1; }
    ln -s "$LIB/$repo/$path" "$d/$path" || return 1
  done <<< "${VFILES[i]}"
  while IFS="$TAB" read -r path size; do
    valid_rel "$path" || continue
    [ -e "$d/$path" ] && continue
    mkdir -p "$d/$(dirname "$path")" && ln -sfn "$LIB/$repo/$path" "$d/$path"
  done <<< "$(shared_for "$repo")"
}

do_remove() {  # drop this variant's files; the folder goes too when only shared library files are left
  local i="$1" vid="${VID[$1]}" repo d path size remaining
  repo="${vid%@*}"; d="$MODELS_DIR/$repo"
  valid_repo "$repo" || { echo "bad repo id '$repo'" >&2; return 1; }
  if [ -L "$d" ]; then ensure_real_dir "$repo" || return 1; fi
  [ -d "$d" ] || return 0
  while IFS="$TAB" read -r path size; do valid_rel "$path" || continue; [ -e "$d/$path" ] || [ -L "$d/$path" ] && safe_rm "$d/$path"; done <<< "${VFILES[i]}"
  remaining=$(cd "$d" && find . \( -type f -o -type l \) ! -name '.*' ! -path '*/.*' | sed 's#^\./##' | grep -v -x -F -f <(shared_for "$repo" | cut -f1) | head -1)
  if [ -z "$remaining" ]; then safe_rm "$d"; else find "$d" -mindepth 1 -type d -empty -delete 2>/dev/null; fi
  return 0
}

prov_file_real() {  # $1 = provider index, $2 = file path in the store -> prints the real data file behind it, if the store owns it
  local p="$2" r
  if [ -L "$p" ]; then [ "$(link_kind "$p" "${PP[$1]}")" = real ] || return 1; r=$(resolve_link "$p"); [ -n "$r" ] && printf '%s' "$r"
  elif [ -f "$p" ]; then printf '%s' "$p"; else return 1; fi
}

project_variant() {  # $1 = variant index. Every compatible provider ends up holding this quant as symlinks to the primary files.
  local i="$1" vid="${VID[$1]}" repo pi k st b dir path size src real prim rev base
  repo="${vid%@*}"; prim="$MODELS_DIR/$repo"
  while IFS="$TAB" read -r k st b; do
    [ -n "$k" ] || continue
    pi=$(prov_index "$k"); [ "$pi" -ge 0 ] || continue
    [ -d "${PP[pi]}" ] || mkdir -p "${PP[pi]}" 2>/dev/null || { echo "  cannot create ${PP[pi]} for $k" >&2; continue; }
    dir=$(prov_repo_dir "$pi" "$repo")
    if [ "${PL[pi]}" = hfcache ]; then
      base=$(prov_base "$pi" "$repo"); rev=${dir##*/}
      mkdir -p "$dir" "$base/refs" && { [ -f "$base/refs/main" ] || printf '%s' "$rev" > "$base/refs/main"; }
    else
      mkdir -p "$dir"
    fi
    while IFS="$TAB" read -r path size; do
      valid_rel "$path" || continue
      [ -e "$prim/$path" ] || [ -L "$prim/$path" ] || continue   # nothing to point at yet
      src="$dir/$path"
      if real=$(prov_file_real "$pi" "$src") && [ -n "$real" ]; then
        # the provider owns a real copy: drop it, the primary is the single copy now
        safe_rm_under "${PP[pi]}" "$real" 2>/dev/null || true
      fi
      mkdir -p "$(dirname "$src")"
      rm -f "$src" 2>/dev/null || true
      ln -s "$prim/$path" "$src"
    done <<< "$(printf '%s' "${VFILES[i]}"; shared_for "$repo")"
    printf '  %s: linked into %s\n' "$k" "$dir"
  done <<< "${VPROV[i]}"
}

adopt_variant() {  # $1 = variant index. Real files a provider holds move into the primary store (no copy from the NAS needed).
  local i="$1" vid="${VID[$1]}" repo pi k st b dir path size src real prim moved=0
  repo="${vid%@*}"; prim="$MODELS_DIR/$repo"
  while IFS="$TAB" read -r k st b; do
    [ -n "$k" ] || continue
    case "$st" in real|partial) ;; *) continue;; esac
    pi=$(prov_index "$k"); [ "$pi" -ge 0 ] || continue
    dir=$(prov_repo_dir "$pi" "$repo")
    while IFS="$TAB" read -r path size; do
      valid_rel "$path" || continue
      [ -f "$prim/$path" ] && [ ! -L "$prim/$path" ] && continue          # primary already has a real file
      src="$dir/$path"
      real=$(prov_file_real "$pi" "$src") || continue
      [ -n "$real" ] || continue
      [ "$(wc -c < "$real" | tr -d ' ')" = "$size" ] || continue          # only exact matches
      mkdir -p "$(dirname "$prim/$path")"
      [ -L "$prim/$path" ] && safe_rm "$prim/$path"
      mv "$real" "$prim/$path" || continue
      moved=$((moved+1))
    done <<< "$(printf '%s' "${VFILES[i]}"; shared_for "$repo")"
  done <<< "${VPROV[i]}"
  [ $moved -gt 0 ] && printf '  adopted %d file(s) already on this machine into %s\n' "$moved" "$prim"
  return 0
}

unproject_variant() {  # $1 = variant index. Remove this quant from every provider; drop empty containers.
  local i="$1" vid="${VID[$1]}" repo pi k st b dir path size src real base left
  repo="${vid%@*}"
  while IFS="$TAB" read -r k st b; do
    [ -n "$k" ] || continue
    pi=$(prov_index "$k"); [ "$pi" -ge 0 ] || continue
    dir=$(prov_repo_dir "$pi" "$repo"); [ -d "$dir" ] || continue
    while IFS="$TAB" read -r path size; do
      valid_rel "$path" || continue
      src="$dir/$path"; [ -e "$src" ] || [ -L "$src" ] || continue
      if real=$(prov_file_real "$pi" "$src") && [ -n "$real" ] && [ "$real" != "$src" ]; then safe_rm_under "${PP[pi]}" "$real" 2>/dev/null || true; fi
      safe_rm_under "${PP[pi]}" "$src"
    done <<< "${VFILES[i]}"
    left=$(cd "$dir" && find . \( -type f -o -type l \) ! -name '.*' ! -path '*/.*' 2>/dev/null | head -1)
    if [ -z "$left" ]; then
      if [ "${PL[pi]}" = hfcache ]; then base=$(prov_base "$pi" "$repo"); safe_rm_under "${PP[pi]}" "$base"; else safe_rm_under "${PP[pi]}" "$dir"; fi
    else find "$dir" -mindepth 1 -type d -empty -delete 2>/dev/null; fi
    printf '  %s: removed\n' "$k"
  done <<< "${VPROV[i]}"
}

consolidate() {  # after the primary store is settled: adopt stray provider copies, then make every provider a set of links
  local i
  for ((i=0;i<N;i++)); do
    [ -n "${VPROV[i]}" ] || continue
    case "${VLOCAL[i]}" in
      cached|linked|partial) project_variant "$i" >/dev/null;;
      absent) if printf '%s' "${VPROV[i]}" | grep -q "${TAB}real${TAB}"; then printf '\n%sAdopting %s from a provider copy%s\n' "$B" "${VID[i]}" "$R"; adopt_variant "$i"; project_variant "$i"; fi;;
    esac
  done
}

apply_one() {  # index, state
  local i="$1" s="$2" vid="${VID[$1]}" rc=0
  case "$s" in
    cached) printf '\n%sCaching %s (%s)%s\n' "$B" "$vid" "$(hb "${VBYTES[i]}")" "$R"; adopt_variant "$i"; do_cache "$i" && project_variant "$i" || rc=1;;
    linked) printf '\n%sLinking %s%s\n' "$B" "$vid" "$R"; do_link "$i" && project_variant "$i" || rc=1;;
    absent) printf '\n%sRemoving %s%s\n' "$B" "$vid" "$R"; unproject_variant "$i"; do_remove "$i" || rc=1;;
  esac
  if [ $rc -eq 0 ]; then printf '%sdone%s\n' "$G" "$R"; else printf '%sfailed: %s%s\n' "$Y" "$vid" "$R" >&2; fi
  return $rc
}

confirm_deletions() {  # args: indices; returns 1 if the user declines
  local i del=0 delbytes=0 ans
  local k st b
  for i in "$@"; do
    case "${VWANT[i]}" in linked|absent)
      if [ "${VREAL[i]:-0}" -gt 0 ]; then del=$((del+1)); delbytes=$((delbytes+VREAL[i])); fi
      while IFS="$TAB" read -r k st b; do [ -n "$k" ] && [ "${b:-0}" -gt 0 ] && { del=$((del+1)); delbytes=$((delbytes+b)); }; done <<< "${VPROV[i]}";;
    esac
  done
  [ $del -eq 0 ] && return 0
  [ $YES -eq 1 ] && return 0
  printf '\nThis deletes %d local cop%s (%s). They can be re-created from the library. Continue? [y/N] ' "$del" "$([ $del -eq 1 ] && echo y || echo ies)" "$(hb $delbytes)"
  read -r ans; case "$ans" in y|Y|yes) return 0;; *) echo "Skipped."; return 1;; esac
}

apply_pending() {
  local i todo
  todo=()
  for ((i=0;i<N;i++)); do is_pending "$i" && todo[${#todo[@]}]="$i"; done
  if [ ${#todo[@]} -eq 0 ]; then echo "Nothing to do."; return 0; fi
  confirm_deletions "${todo[@]}" || return 1
  for i in "${todo[@]}"; do apply_one "$i" "${VWANT[i]}"; done
  sync_state
  [ $NP -gt 0 ] && { consolidate; sync_state; }
  return 0
}

change_one() {
  local n="$1" i s
  case "$n" in ''|*[!0-9]*) echo "Not a row number."; return;; esac
  if [ "$n" -lt 1 ] || [ "$n" -gt "$N" ]; then echo "No such row."; return; fi
  i=$((n-1))
  printf 'Set %s to  [1] not available  [2] cached locally  [3] linked  [Enter] cancel: ' "${VID[i]}"
  read -r s
  case "$s" in 1) s=absent;; 2) s=cached;; 3) s=linked;; *) return;; esac
  if [ "$s" = "${VLOCAL[i]}" ] && [ -z "${VWANT[i]}" -o "${VWANT[i]}" = "$s" ]; then unstage_state "${VID[i]}"; echo "Already $s."; return; fi
  stage_state "${VID[i]}" "$s"
  printf 'Staged: %s -> %s (runs on commit).\n' "${VID[i]}" "$s"
}

# ----- uploads of local quants the library lacks -----
upload_variant() {  # $1 = index into the foreign list
  local i="$1" xid="${XID[$1]}" repo d f size have rc=0 json pid total count status pi rev="" src="${XSRC[$1]:-primary}"
  repo="${xid%@*}"; d="$MODELS_DIR/$repo"
  if [ "$src" != primary ]; then
    pi=$(prov_index "$src"); [ "$pi" -ge 0 ] || { echo "Unknown provider $src for $xid" >&2; return 1; }
    d=$(prov_repo_dir "$pi" "$repo")
    [ "${PL[pi]}" = hfcache ] && rev=$(prov_rev "$pi" "$repo") && [ "$rev" = lmscache ] && rev=""
  fi
  count=$(printf '%s' "${XFILES[i]}" | grep -c .); total="${XBYTES[i]}"
  printf '\n%sUploading %s%s: %s in %s file(s) to the library\n' "$B" "$xid" "$R" "$(hb "$total")" "$count"
  status=$(curl -fsS "$NAS/api/upload/$repo") || { echo "LMS Cache refused the upload (is a download of this model running on the NAS?)" >&2; return 1; }
  while IFS="$TAB" read -r f size; do
    [ -n "$f" ] || continue
    have=$(printf '%s\n' "$status" | awk -F"$TAB" -v p="$f" '$1==p{print $2}'); have=${have:-0}
    if [ "$size" -gt 0 ] && [ "$have" -ge "$size" ]; then printf '  %s: already on the NAS\n' "$f"; continue; fi
    printf '  %s (%s)%s\n' "$f" "$(hb "$size")" "$([ "$have" -gt 0 ] && echo " resuming at $(hb "$have")")"
    if [ "$have" -gt 0 ]; then
      curl -fsS -C "$have" -T "$d/$f" -H 'Content-Type: application/octet-stream' -o /dev/null "$NAS/api/upload/$repo/$(urlencode "$f")" & pid=$!
    else
      curl -fsS -T "$d/$f" -H 'Content-Type: application/octet-stream' -o /dev/null "$NAS/api/upload/$repo/$(urlencode "$f")" & pid=$!
    fi
    watch_progress "$pid" "$total" upload_have "$repo"
    wait "$pid" || { rc=1; break; }
  done <<< "${XFILES[i]}"
  if [ $rc -ne 0 ]; then printf '%sUpload interrupted; run it again to resume.%s\n' "$Y" "$R" >&2; return 1; fi
  json=""
  while IFS="$TAB" read -r f size; do [ -n "$f" ] || continue; json="$json{\"path\":\"$(esc "$f")\",\"size\":$size},"; done <<< "${XFILES[i]}"
  json="{\"machine\":\"$MACHINE\",\"revision\":\"$(esc "$rev")\",\"files\":[${json%,}]}"
  if printf '%s' "$json" | curl -fsS -X POST -H 'Content-Type: application/json' --data-binary @- "$NAS/api/upload/$repo/commit" >/dev/null; then
    printf '%sAdded %s to the library.%s\n' "$G" "$xid" "$R"
    sync_state
  else
    printf '%sThe NAS did not accept the upload; see the LMS Cache log.%s\n' "$Y" "$R" >&2; return 1
  fi
}

upload_by_id() {  # --upload repo@QUANT, or repo when it has exactly one local quant to offer
  local want="$1" i hits=() 
  for ((i=0;i<NX;i++)); do
    if [ "${XID[i]}" = "$want" ] || [ "${XID[i]%@*}" = "$want" ]; then hits[${#hits[@]}]="$i"; fi
  done
  if [ ${#hits[@]} -eq 1 ]; then upload_variant "${hits[0]}"; return $?; fi
  if [ ${#hits[@]} -eq 0 ]; then echo "No local quant named $want is missing from the library. Candidates:"; else echo "$want is ambiguous. Candidates:"; fi
  for ((i=0;i<NX;i++)); do printf '  %s\n' "${XID[i]}"; done
  return 1
}

toggle_upload() {  # $1 = 1-based foreign index: stage the upload, or unstage it if already staged
  local n="$1" id i j=0 ids=()
  if [ "$n" -lt 1 ] 2>/dev/null || [ "$n" -gt "$NX" ]; then echo "No such entry."; return; fi
  id="${XID[$((n-1))]}"
  if upload_staged "$id"; then
    for ((i=0;i<UN;i++)); do [ "${UIDS[i]}" = "$id" ] && continue; ids[j]="${UIDS[i]}"; j=$((j+1)); done
    UIDS=("${ids[@]}"); UN=$j; echo "Unstaged the upload of $id."
  else
    UIDS[UN]="$id"; UN=$((UN+1)); printf 'Staged: upload %s (runs on commit).\n' "$id"
  fi
}

choose_upload() {
  local n
  if [ $NX -eq 0 ]; then echo "Every local quant is already in the library."; return; fi
  printf 'Stage the upload of which local quant? [1-%d, Enter to cancel] ' "$NX"
  read -r n
  case "$n" in ''|*[!0-9]*) return;; esac
  toggle_upload "$n"
}

commit_and_quit() {
  local i idx
  for ((i=0;i<UN;i++)); do
    idx=$(xindex "${UIDS[i]}")
    if [ "$idx" -ge 0 ]; then upload_variant "$idx" || printf '%sUpload of %s did not finish; run the script again to resume it.%s\n' "$Y" "${UIDS[i]}" "$R" >&2; fi
  done
  UN=0; UIDS=()
  for ((i=0;i<SN;i++)); do
    idx=$(vindex "${SIDS[i]}")
    [ "$idx" -ge 0 ] || continue
    set_intent "${SIDS[i]}" "${SSTATES[i]}"; VWANT[idx]="${SSTATES[i]}"
  done
  SN=0; SIDS=(); SSTATES=()
  local todo=()
  for ((i=0;i<N;i++)); do is_pending "$i" && todo[${#todo[@]}]="$i"; done
  if [ ${#todo[@]} -gt 0 ]; then apply_pending; elif [ $NP -gt 0 ]; then consolidate; sync_state; else sync_state; fi
  if [ $STAGE_MOUNT -eq 1 ]; then STAGE_MOUNT=0; setup_mount; fi
  reported
}

discard_and_quit() {
  SN=0; SIDS=(); SSTATES=(); UN=0; UIDS=(); STAGE_MOUNT=0
  sync_state && reported
}

# ----- login-time mount -----
setup_mount() {
  if [ "$OS" = mac ]; then
    local flag="-N " plist="$HOME/Library/LaunchAgents/lmscache.mount.plist" url
    adopt_mac_session
    url=$(smb_url)
    if [ "$SMB_USER" != guest ] && [ -z "$SMB_PASS" ]; then echo "Account $SMB_USER: the login-time mount uses the password saved in your Keychain. If Finder has never remembered it for $SMB_HOST, connect once with 'Remember this password in my keychain'."; fi
    mkdir -p "$MOUNT" "$HOME/Library/LaunchAgents"
    cat > "$plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>lmscache.mount</string>
  <key>ProgramArguments</key><array>
    <string>/bin/sh</string><string>-c</string>
    <string>[ -d "$LIB" ] || /sbin/mount_smbfs ${flag}-o soft,nobrowse "$url" "$MOUNT"</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>StartInterval</key><integer>300</integer>
</dict></plist>
PLIST
    chmod 600 "$plist"
    launchctl bootout "gui/$(id -u)/lmscache.mount" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "$plist" && echo "Installed: the share mounts at login and is re-checked every 5 minutes (~/Library/LaunchAgents/lmscache.mount.plist)."
  else
    local cred unit
    command -v mount.cifs >/dev/null || echo "cifs-utils is required (apt install cifs-utils, or the equivalent for your distro)."
    sudo mkdir -p "$MOUNT"
    if [ "$SMB_USER" = guest ]; then cred=guest
    else
      printf 'username=%s\npassword=%s\n' "$SMB_USER" "$SMB_PASS" | sudo tee /etc/lmscache-smb.cred >/dev/null && sudo chmod 600 /etc/lmscache-smb.cred
      cred="credentials=/etc/lmscache-smb.cred"
    fi
    if grep -q " $MOUNT " /etc/fstab; then echo "/etc/fstab already has an entry for $MOUNT."
    else echo "//$SMB_HOST/$SMB_SHARE_FSTAB $MOUNT cifs $cred,ro,soft,vers=3.1.1,uid=$(id -u),gid=$(id -g),iocharset=utf8,noauto,x-systemd.automount,x-systemd.idle-timeout=600,_netdev 0 0" | sudo tee -a /etc/fstab >/dev/null; fi
    unit=$(systemd-escape -p --suffix=automount "$MOUNT")
    if mountpoint -q "$MOUNT" 2>/dev/null; then
      # the temporary mount made earlier in this run blocks the automount ("already a mount point")
      sudo umount "$MOUNT" || { printf '%sCould not unmount the temporary mount at %s; close anything using it and choose m again.%s\n' "$Y" "$MOUNT" "$R"; return 1; }
    fi
    sudo systemctl daemon-reload && sudo systemctl restart "$unit" && echo "Installed: $MOUNT mounts on first access (systemd automount $unit)."
  fi
  sleep 1
  if [ -d "$LIB" ]; then echo "Library is reachable at $LIB."; else printf '%sLibrary is not reachable yet; check the share permissions for %s.%s\n' "$Y" "$SMB_USER" "$R"; fi
}

reported() { printf '%sReported %s state to LMS Cache.%s\n' "$G" "$MACHINE" "$R"; }

main() {
  local choice n
  case "$MODELS_DIR" in /?*) ;; *) echo "Refusing to run: the models folder is '$MODELS_DIR'. Fix this machine's profile in LMS Cache." >&2; exit 1;; esac
  if [ ! -d "$MODELS_DIR" ]; then
    echo "LM Studio models folder not found at $MODELS_DIR. Edit this machine in LMS Cache if the path is different." >&2
    exit 1
  fi
  boot_providers
  if [ $REPORT_ONLY -eq 1 ]; then sync_state && reported; exit $?; fi
  ensure_mount || printf '%sThe library share is not mounted; caching and linking will fail until it is.%s\n' "$Y" "$R"
  sync_state || exit 1
  if [ -n "$UPLOAD" ]; then upload_by_id "$UPLOAD"; rc=$?; reported; exit $rc; fi
  if [ $AUTO -eq 1 ]; then
    render
    if [ $PEND -gt 0 ]; then apply_pending; elif [ $NP -gt 0 ]; then consolidate; sync_state; fi
    reported
    exit 0
  fi
  while :; do
    render
    show_staged
    if [ "$N" -gt 0 ]; then
      printf '\n[1-%d] change one   [u] upload a local quant   [m] mount at login%s   [r] refresh   [c] commit and quit   [d] discard and quit\n> ' "$N" "$([ $STAGE_MOUNT -eq 1 ] && printf ' (staged)')"
    else
      printf '\n[u] upload a local quant   [m] mount at login%s   [r] refresh   [c] commit and quit   [d] discard and quit\n> ' "$([ $STAGE_MOUNT -eq 1 ] && printf ' (staged)')"
    fi
    read -r choice || { echo; discard_and_quit; break; }
    case "$choice" in
      u|U) choose_upload;;
      u[0-9]*|U[0-9]*) toggle_upload "${choice#[uU]}";;
      m|M) if [ $STAGE_MOUNT -eq 1 ]; then STAGE_MOUNT=0; echo "Unstaged: mount at login."; else STAGE_MOUNT=1; echo "Staged: mount at login (runs on commit)."; fi;;
      r|R) sync_state && reported;;
      c|C) commit_and_quit; break;;
      d|D) discard_and_quit; break;;
      q|Q) echo "Use c to commit and quit, or d to discard and quit.";;
      "") ;;
      *) change_one "$choice";;
    esac
  done
}

main "$@"
'''
