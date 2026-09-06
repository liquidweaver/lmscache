"""Machine profiles, per-machine intents and reports, and the shell that the UI hands out to copy."""

from __future__ import annotations

import re
import time
from urllib.parse import quote, urlsplit

from . import config, db
from .util import valid_repo_id

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$")
STATES = ("absent", "cached", "linked")
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
    link_mode = data.get("link_mode") or existing.get("link_mode") or "dir"
    if link_mode not in ("dir", "files"):
        raise ValueError("link_mode must be dir or files")
    profile = {
        "name": name,
        "os": os_,
        "models_dir": (data.get("models_dir") or existing.get("models_dir") or OS_DEFAULTS[os_]["models_dir"]).strip(),
        "mount": (data.get("mount") or existing.get("mount") or OS_DEFAULTS[os_]["mount"]).strip().rstrip("/"),
        "smb_user": (data.get("smb_user") or "").strip() or None,
        "link_mode": link_mode,
        "created_at": existing.get("created_at") or time.time(),
    }
    for key in ("models_dir", "mount"):
        if not profile[key].startswith(("/", "~")):
            raise ValueError(f"{key} must be an absolute path or start with ~")
    db.put_json("machines", "name", name, "data", profile)
    return profile


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


# ----- intents and reports -----
def set_intent(machine: str, model_id: str, state: str) -> None:
    if state not in STATES:
        raise ValueError("state must be absent, cached or linked")
    db.execute(
        "INSERT OR REPLACE INTO intents (machine, model_id, state, set_at) VALUES (?, ?, ?, ?)",
        (machine, model_id, state, time.time()),
    )


def clear_intent(machine: str, model_id: str) -> None:
    db.execute("DELETE FROM intents WHERE machine = ? AND model_id = ?", (machine, model_id))


def intents() -> dict[str, dict[str, dict]]:
    out: dict[str, dict[str, dict]] = {}
    for machine, model_id, state, set_at in db.query("SELECT machine, model_id, state, set_at FROM intents"):
        out.setdefault(machine, {})[model_id] = {"state": state, "set_at": set_at}
    return out


def store_report(machine: str, payload: dict) -> dict:
    models: dict[str, dict] = {}
    for entry in payload.get("models") or []:
        mid = str(entry.get("id") or "")
        if not valid_repo_id(mid):
            continue
        state = entry.get("state")
        if state not in ("cached", "linked"):
            continue
        models[mid] = {"state": state, "bytes": int(entry.get("bytes") or 0), "target": entry.get("target") or ""}
    report = {
        "at": time.time(),
        "free_bytes": int(payload.get("free_bytes") or 0),
        "total_bytes": int(payload.get("total_bytes") or 0),
        "models": models,
    }
    db.put_json("reports", "machine", machine, "data", report, {"at": report["at"]})
    return report


def reports() -> dict[str, dict]:
    return db.all_json("reports", "machine", "data")


def cells(models: dict[str, dict], machine_names: list[str], intents_map: dict, reports_map: dict) -> dict:
    """Per machine, per model: what the last report says, what the user intends, and whether they disagree."""
    out: dict[str, dict[str, dict]] = {}
    for name in machine_names:
        rep = reports_map.get(name)
        my_intents = intents_map.get(name, {})
        row: dict[str, dict] = {}
        for mid, model in models.items():
            reported = None
            nbytes = 0
            if rep is not None:
                entry = rep["models"].get(mid)
                if entry is None:
                    reported = "absent"
                elif entry["state"] == "linked":
                    reported = "linked"
                else:
                    nbytes = entry["bytes"]
                    total = model.get("total_bytes") or 0
                    reported = "partial" if total and nbytes < total * 0.98 else "cached"
            intent = (my_intents.get(mid) or {}).get("state")
            pending = bool(intent) and (reported is None or intent != reported)
            if intent and reported == "partial" and intent == "cached":
                pending = True
            row[mid] = {"reported": reported, "bytes": nbytes, "intent": intent, "pending": pending}
        out[name] = row
    return out


def foreign(models: dict[str, dict], reports_map: dict) -> dict[str, list[dict]]:
    """Models present on a machine that the library does not have."""
    out: dict[str, list[dict]] = {}
    for name, rep in reports_map.items():
        extras = [
            {"id": mid, **entry}
            for mid, entry in rep["models"].items()
            if mid not in models and not (entry["state"] == "cached" and entry["bytes"] == 0)
        ]
        if extras:
            out[name] = sorted(extras, key=lambda e: e["id"].lower())
    return out


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
    """Render a user path for use inside double quotes; ~ becomes $HOME."""
    if path.startswith("~"):
        path = "$HOME" + path[1:]
    return path.replace("\\", "\\\\").replace('"', '\\"').replace("`", "\\`")


def one_liner(machine: dict, base: str) -> str:
    return f'bash -c "$(curl -fsSL {base}/lmsc/{machine["name"]}.sh)"'


def plan_text(models: dict[str, dict], machine_name: str) -> str:
    """Tab-separated: id, total bytes, format, wanted state ('-' when none). Parsed by the client script."""
    wanted = intents().get(machine_name, {})
    lines = []
    for mid in sorted(models, key=str.lower):
        m = models[mid]
        want = (wanted.get(mid) or {}).get("state") or "-"
        lines.append("\t".join([mid, str(m.get("total_bytes") or 0), m.get("format") or "other", want]))
    return "\n".join(lines) + ("\n" if lines else "")


def client_script(machine: dict, base: str, host: str, settings: dict) -> str:
    share = settings.get("smb_share") or "models"
    user = machine.get("smb_user") or settings.get("smb_user") or "guest"
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
        "SMB_USER_URL": quote(user, safe=""),
        "SMB_PASS": password.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$").replace("`", "\\`"),
        "SMB_PASS_URL": quote(password, safe=""),
        "LINK_MODE": machine.get("link_mode") or "dir",
    }
    script = _CLIENT_TEMPLATE
    for key, val in values.items():
        script = script.replace("@@" + key + "@@", val)
    return script


_CLIENT_TEMPLATE = r'''#!/usr/bin/env bash
# LMS Cache client for "@@MACHINE@@". Fetched fresh from @@NAS@@ on every run; nothing is installed.
# Run:  bash -c "$(curl -fsSL @@NAS@@/lmsc/@@MACHINE@@.sh)"
# Flags (append after the closing quote):  lmsc --apply   apply wanted changes without the menu
#                                          lmsc --report  only report local state
#                                          lmsc --yes     with --apply: do not ask before deleting local copies
#                                          lmsc --upload publisher/repo   upload a local model that the library lacks
NAS="@@NAS@@"; MACHINE="@@MACHINE@@"; OS="@@OS@@"
MODELS_DIR="@@MODELS_DIR@@"; MOUNT="@@MOUNT@@"; LIB="$MOUNT/lmstudio"
SMB_HOST="@@SMB_HOST@@"; SMB_SHARE="@@SMB_SHARE@@"; SMB_SHARE_URL="@@SMB_SHARE_URL@@"; SMB_SHARE_FSTAB="@@SMB_SHARE_FSTAB@@"
SMB_USER="@@SMB_USER@@"; SMB_USER_URL="@@SMB_USER_URL@@"; SMB_PASS="@@SMB_PASS@@"; SMB_PASS_URL="@@SMB_PASS_URL@@"; LINK_MODE="@@LINK_MODE@@"

AUTO=0; REPORT_ONLY=0; YES=0; UPLOAD=""
while [ $# -gt 0 ]; do
  case "$1" in
    --apply) AUTO=1;; --report) REPORT_ONLY=1;; --yes|-y) YES=1;;
    --upload) if [ $# -gt 1 ]; then shift; UPLOAD="$1"; fi;;
  esac
  shift
done
if [ -t 1 ]; then B=$'\033[1m'; D=$'\033[2m'; Y=$'\033[33m'; G=$'\033[32m'; R=$'\033[0m'; else B=""; D=""; Y=""; G=""; R=""; fi

N=0; PEND=0; NF=0
IDS=(); SIZES=(); FMTS=(); WANT=(); LOCAL=(); LBYTES=(); FOREIGN=(); FBYTES=()

hb() { awk -v b="${1:-0}" 'BEGIN{split("B KB MB GB TB",u," ");i=1;while(b>=1024&&i<5){b/=1024;i++}; if(i==1)printf "%d %s",b,u[i]; else printf "%.1f %s",b,u[i]}'; }
esc() { printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'; }
urlencode() { local LC_ALL=C s="$1" i c out=""; for ((i=0;i<${#s};i++)); do c="${s:i:1}"; case "$c" in [a-zA-Z0-9./_-]) out="$out$c";; *) out="$out$(printf '%%%02X' "'$c")";; esac; done; printf '%s' "$out"; }
dir_bytes() { ( cd "$1" 2>/dev/null && find . -type f ! -name '.*' ! -path '*/.*' -exec wc -c {} \; 2>/dev/null ) | awk '{s+=$1} END{printf "%.0f", s+0}'; }   # relative paths, so a dot in the parent path (like ~/.lmstudio) is not mistaken for a hidden file
dir_bytes_all() { find "$1" -type f -exec wc -c {} \; 2>/dev/null | awk '{s+=$1} END{printf "%.0f", s+0}'; }   # includes rsync's temporary files, for progress
upload_have() { curl -fsS -m 10 "$NAS/api/upload/$1" 2>/dev/null | awk -F'\t' '{s+=$2} END{printf "%.0f", s+0}'; }
fmt_time() {
  local s=${1:-0}
  if [ "$s" -ge 3600 ]; then printf '%dh %02dm' $((s/3600)) $(((s%3600)/60))
  elif [ "$s" -ge 60 ]; then printf '%dm %02ds' $((s/60)) $((s%60))
  else printf '%ds' "$s"; fi
}
# watch_progress <pid> <total bytes> <command that prints bytes done...>
# One updating line (percent, bytes, speed, time left) until the process exits. Only when stdout is a terminal.
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

smb_url() {  # URL form used by mount_smbfs on macOS
  if [ "$SMB_USER" = guest ]; then printf '//guest@%s/%s' "$SMB_HOST" "$SMB_SHARE_URL"
  elif [ -n "$SMB_PASS_URL" ]; then printf '//%s:%s@%s/%s' "$SMB_USER_URL" "$SMB_PASS_URL" "$SMB_HOST" "$SMB_SHARE_URL"
  else printf '//%s@%s/%s' "$SMB_USER_URL" "$SMB_HOST" "$SMB_SHARE_URL"; fi
}
cifs_cred() {  # credential options for mount.cifs on Linux
  if [ "$SMB_USER" = guest ]; then printf 'guest'
  elif [ -n "$SMB_PASS" ]; then printf 'username=%s,password=%s' "$SMB_USER" "$SMB_PASS"
  else printf 'username=%s' "$SMB_USER"; fi
}

ensure_mount() {
  [ -d "$LIB" ] && return 0
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

fetch_plan() {
  local plan id size fmt want
  plan=$(curl -fsS "$NAS/api/machines/$MACHINE/plan") || { echo "Cannot reach LMS Cache at $NAS" >&2; exit 1; }
  N=0
  while IFS=$'\t' read -r id size fmt want; do
    [ -n "$id" ] || continue
    [ "$want" = "-" ] && want=""
    IDS[N]="$id"; SIZES[N]="${size:-0}"; FMTS[N]="$fmt"; WANT[N]="$want"; N=$((N+1))
  done <<< "$plan"
}

local_state() {  # $1 = model id, $2 = expected bytes -> prints "state bytes"
  local d="$MODELS_DIR/$1" st=absent by=0
  if [ -L "$d" ]; then st=linked
  elif [ -d "$d" ]; then
    if [ -z "$(find "$d" -type f -print -quit 2>/dev/null)" ] && [ -n "$(find "$d" -type l -print -quit 2>/dev/null)" ]; then st=linked
    else
      by=$(find "$d" -type f -exec wc -c {} \; 2>/dev/null | awk '{s+=$1} END{printf "%.0f", s+0}')
      if [ "$by" -eq 0 ]; then st=absent
      elif [ "${2:-0}" -gt 0 ] && [ "$by" -lt $(( ${2:-0} * 98 / 100 )) ]; then st=partial; else st=cached; fi
    fi
  fi
  printf '%s %s' "$st" "$by"
}

scan_local() {
  local i out
  for ((i=0;i<N;i++)); do
    out=$(local_state "${IDS[i]}" "${SIZES[i]}")
    LOCAL[i]="${out% *}"; LBYTES[i]="${out#* }"
  done
}

is_pending() { local w="${WANT[$1]}" l="${LOCAL[$1]}"; [ -n "$w" ] && [ "$w" != "$l" ]; }

list_foreign() {
  local pub repo id found i size
  NF=0; FOREIGN=(); FBYTES=()
  [ -d "$MODELS_DIR" ] || return 0
  for pub in "$MODELS_DIR"/*/; do
    pub=${pub%/}; [ -d "$pub" ] || continue
    case "${pub##*/}" in .*) continue;; esac
    for repo in "$pub"/*; do
      [ -e "$repo" ] || [ -L "$repo" ] || continue
      case "${repo##*/}" in .*) continue;; esac
      [ -d "$repo" ] || continue
      id="${pub##*/}/${repo##*/}"
      found=0; for ((i=0;i<N;i++)); do [ "${IDS[i]}" = "$id" ] && { found=1; break; }; done
      if [ $found -eq 0 ]; then
        size=$(dir_bytes "$repo")
        [ "${size:-0}" -gt 0 ] || continue   # an empty folder is nothing to upload
        FOREIGN[NF]="$id"; FBYTES[NF]="$size"; NF=$((NF+1))
      fi
    done
  done
  if [ $NF -gt 0 ]; then
    printf '\n%sLocal models not in the library%s (type a label such as u1 to upload that model; the Search page can fetch them from the Hub instead):\n' "$D" "$R"
    for ((i=0;i<NF;i++)); do printf '  u%-2d %-52.52s %10s\n' "$((i+1))" "${FOREIGN[i]}" "$(hb "${FBYTES[i]}")"; done
  fi
}

render() {
  local i mark w l free c
  free=$(df -Pk "$MODELS_DIR" 2>/dev/null | awk 'NR==2{printf "%.0f", $4*1024}')
  printf '\n%sLMS Cache%s · %s · library at %s · %s free locally\n\n' "$B" "$R" "$MACHINE" "$LIB" "$(hb "${free:-0}")"
  printf '%s  %3s  %-52s %10s  %-8s %-8s%s\n' "$D" "#" "Model" "Size" "Local" "Wanted" "$R"
  PEND=0
  for ((i=0;i<N;i++)); do
    w="${WANT[i]:--}"; l="${LOCAL[i]}"; mark=" "; c=""
    if is_pending "$i"; then mark="*"; c="$Y"; PEND=$((PEND+1)); fi
    [ "$l" = absent ] && l="-"
    printf '%s%s %3d  %-52.52s %10s  %-8s %-8s%s\n' "$c" "$mark" "$((i+1))" "${IDS[i]}" "$(hb "${SIZES[i]}")" "$l" "$w" "$R"
  done
  [ "$N" -eq 0 ] && echo "  (the library is empty; download something from the Search page first)"
  list_foreign
}

set_intent() { curl -fsS -X PUT -H 'Content-Type: application/json' -d "{\"state\":\"$2\"}" "$NAS/api/machines/$MACHINE/models/$1" >/dev/null 2>&1 || true; }

do_cache() {  # $1 = model id, $2 = expected bytes from the library plan
  local id="$1" d="$MODELS_DIR/$1" total="${2:-0}" pid
  ensure_mount || return 1
  [ -L "$d" ] && rm "$d"
  mkdir -p "$d" || return 1
  [ "$total" -gt 0 ] || total=$(dir_bytes_all "$LIB/$id")
  if command -v rsync >/dev/null; then rsync -a --partial "$LIB/$id/" "$d/" & pid=$!
  else cp -R "$LIB/$id/." "$d/" & pid=$!; fi
  watch_progress "$pid" "$total" dir_bytes_all "$d"
  wait "$pid"
}

do_link() {
  local id="$1" d="$MODELS_DIR/$1" f
  ensure_mount || return 1
  if [ -d "$d" ] && [ ! -L "$d" ]; then rm -rf "$d"; fi
  if [ "$LINK_MODE" = files ]; then
    mkdir -p "$d" || return 1
    (cd "$LIB/$id" && find . -type f) | while IFS= read -r f; do
      mkdir -p "$d/$(dirname "$f")" && ln -sfn "$LIB/$id/$f" "$d/$f"
    done
  else
    mkdir -p "$(dirname "$d")" && ln -sfn "$LIB/$id" "$d"
  fi
}

do_remove() { local d="$MODELS_DIR/$1"; if [ -L "$d" ]; then rm "$d"; elif [ -d "$d" ]; then rm -rf "$d"; fi; }

apply_one() {  # index, state
  local i="$1" s="$2" id="${IDS[$1]}" rc=0
  case "$s" in
    cached) printf '\n%sCaching %s (%s)%s\n' "$B" "$id" "$(hb "${SIZES[i]}")" "$R"; do_cache "$id" "${SIZES[i]}" || rc=1;;
    linked) printf '\n%sLinking %s%s\n' "$B" "$id" "$R"; do_link "$id" || rc=1;;
    absent) printf '\n%sRemoving %s%s\n' "$B" "$id" "$R"; do_remove "$id" || rc=1;;
  esac
  if [ $rc -eq 0 ]; then printf '%sdone%s\n' "$G" "$R"; else printf '%sfailed: %s%s\n' "$Y" "$id" "$R" >&2; fi
  return $rc
}

confirm_deletions() {  # args: indices; returns 1 if the user declines
  local i del=0 delbytes=0 ans
  for i in "$@"; do
    case "${LOCAL[i]}" in cached|partial) case "${WANT[i]}" in linked|absent) del=$((del+1)); delbytes=$((delbytes+${LBYTES[i]}));; esac;; esac
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
  for i in "${todo[@]}"; do apply_one "$i" "${WANT[i]}"; done
  scan_local
}

change_one() {
  local n="$1" i s
  case "$n" in ''|*[!0-9]*) echo "Not a row number."; return;; esac
  if [ "$n" -lt 1 ] || [ "$n" -gt "$N" ]; then echo "No such row."; return; fi
  i=$((n-1))
  printf 'Set %s to  [1] not available  [2] cached locally  [3] linked  [Enter] cancel: ' "${IDS[i]}"
  read -r s
  case "$s" in 1) s=absent;; 2) s=cached;; 3) s=linked;; *) return;; esac
  WANT[i]="$s"; set_intent "${IDS[i]}" "$s"
  if is_pending "$i"; then
    confirm_deletions "$i" || return
    apply_one "$i" "$s"; scan_local
  else
    echo "Already $s."
  fi
}

upload_model() {  # $1 = publisher/repo present locally but missing from the library
  local id="$1" d="$MODELS_DIR/$1" files count total status f size have rc=0 json pid
  case "$id" in */*) ;; *) echo "Give the model as publisher/repo."; return 1;; esac
  [ -d "$d" ] || { echo "No local folder at $d"; return 1; }
  files=$(cd "$d" && find . -type f ! -name '.*' ! -path '*/.*' | sed 's#^\./##' | sort)
  [ -n "$files" ] || { echo "Nothing to upload in $d"; return 1; }
  count=$(printf '%s\n' "$files" | wc -l | tr -d ' '); total=$(dir_bytes "$d")
  printf '\n%sUploading %s%s: %s in %s file(s) to the library\n' "$B" "$id" "$R" "$(hb "$total")" "$count"
  status=$(curl -fsS "$NAS/api/upload/$id") || { echo "LMS Cache refused the upload (is a download of this model running on the NAS?)" >&2; return 1; }
  while IFS= read -r f; do
    size=$(wc -c < "$d/$f" | tr -d ' ')
    have=$(printf '%s\n' "$status" | awk -F'\t' -v p="$f" '$1==p{print $2}'); have=${have:-0}
    if [ "$size" -gt 0 ] && [ "$have" -ge "$size" ]; then printf '  %s: already on the NAS\n' "$f"; continue; fi
    printf '  %s (%s)%s\n' "$f" "$(hb "$size")" "$([ "$have" -gt 0 ] && echo " resuming at $(hb "$have")")"
    if [ "$have" -gt 0 ]; then
      curl -fsS -C "$have" -T "$d/$f" -H 'Content-Type: application/octet-stream' -o /dev/null "$NAS/api/upload/$id/$(urlencode "$f")" & pid=$!
    else
      curl -fsS -T "$d/$f" -H 'Content-Type: application/octet-stream' -o /dev/null "$NAS/api/upload/$id/$(urlencode "$f")" & pid=$!
    fi
    watch_progress "$pid" "$total" upload_have "$id"
    wait "$pid" || { rc=1; break; }
  done <<< "$files"
  if [ $rc -ne 0 ]; then printf '%sUpload interrupted; run it again to resume.%s\n' "$Y" "$R" >&2; return 1; fi
  json=""
  while IFS= read -r f; do json="$json{\"path\":\"$(esc "$f")\",\"size\":$(wc -c < "$d/$f" | tr -d ' ')},"; done <<< "$files"
  json="{\"machine\":\"$MACHINE\",\"files\":[${json%,}]}"
  if printf '%s' "$json" | curl -fsS -X POST -H 'Content-Type: application/json' --data-binary @- "$NAS/api/upload/$id/commit" >/dev/null; then
    printf '%sAdded %s to the library.%s\n' "$G" "$id" "$R"
    fetch_plan; scan_local
  else
    printf '%sThe NAS did not accept the upload; see the LMS Cache log.%s\n' "$Y" "$R" >&2; return 1
  fi
}

choose_upload() {
  local n
  if [ $NF -eq 0 ]; then echo "Every local model is already in the library."; return; fi
  printf 'Upload which local model? [1-%d, Enter to cancel] ' "$NF"
  read -r n
  case "$n" in ''|*[!0-9]*) return;; esac
  if [ "$n" -lt 1 ] || [ "$n" -gt "$NF" ]; then echo "No such entry."; return; fi
  upload_model "${FOREIGN[$((n-1))]}"
}

setup_mount() {
  if [ "$OS" = mac ]; then
    local flag="" plist="$HOME/Library/LaunchAgents/lmscache.mount.plist" url
    url=$(smb_url)
    if [ "$SMB_USER" = guest ] || [ -n "$SMB_PASS" ]; then flag="-N "
    else echo "Account $SMB_USER has no password stored in LMS Cache: connect once in Finder with 'Remember this password in my keychain', or the login-time mount cannot run unattended."; fi
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
    sudo systemctl daemon-reload && sudo systemctl start "$unit" && echo "Installed: $MOUNT mounts on first access (systemd automount $unit)."
  fi
  sleep 1
  if [ -d "$LIB" ]; then echo "Library is reachable at $LIB."; else printf '%sLibrary is not reachable yet; check the share permissions for %s.%s\n' "$Y" "$SMB_USER" "$R"; fi
}

report() {
  local pub repo state bytes target free total first json here
  here=$(pwd)
  cd "$MODELS_DIR" 2>/dev/null || { echo "models folder not found: $MODELS_DIR" >&2; return 1; }
  free=$(df -Pk . | awk 'NR==2{printf "%.0f", $4*1024}'); total=$(df -Pk . | awk 'NR==2{printf "%.0f", $2*1024}')
  json="{\"free_bytes\":${free:-0},\"total_bytes\":${total:-0},\"models\":["
  first=1
  for pub in */; do
    pub=${pub%/}; [ -d "$pub" ] || continue
    case "$pub" in .*) continue;; esac
    for repo in "$pub"/*; do
      [ -e "$repo" ] || [ -L "$repo" ] || continue
      case "${repo#*/}" in .*) continue;; esac
      if [ -L "$repo" ]; then state=linked; bytes=0; target=$(readlink "$repo")
      elif [ -d "$repo" ]; then
        target=""
        if [ -z "$(find "$repo" -type f -print -quit 2>/dev/null)" ] && [ -n "$(find "$repo" -type l -print -quit 2>/dev/null)" ]; then state=linked; bytes=0
        else state=cached; bytes=$(find "$repo" -type f -exec wc -c {} \; 2>/dev/null | awk '{s+=$1} END{printf "%.0f", s+0}'); fi
        [ "$state" = cached ] && [ "${bytes:-0}" -eq 0 ] && continue   # empty folder: nothing to report
      else continue; fi
      [ $first -eq 1 ] || json="$json,"; first=0
      json="$json{\"id\":\"$(esc "$repo")\",\"state\":\"$state\",\"bytes\":${bytes:-0},\"target\":\"$(esc "$target")\"}"
    done
  done
  json="$json]}"
  cd "$here"
  if printf '%s' "$json" | curl -fsS -X POST -H 'Content-Type: application/json' --data-binary @- "$NAS/api/machines/$MACHINE/report" >/dev/null; then
    printf '%sReported %s state to LMS Cache.%s\n' "$G" "$MACHINE" "$R"
  else
    echo "Could not send the report to $NAS" >&2
  fi
}

main() {
  local choice n
  if [ ! -d "$MODELS_DIR" ]; then
    echo "LM Studio models folder not found at $MODELS_DIR. Edit this machine in LMS Cache if the path is different." >&2
    exit 1
  fi
  fetch_plan
  if [ $REPORT_ONLY -eq 1 ]; then report; exit 0; fi
  if [ -n "$UPLOAD" ]; then scan_local; list_foreign >/dev/null; upload_model "$UPLOAD"; report; exit $?; fi
  ensure_mount || printf '%sThe library share is not mounted; caching and linking will fail until it is.%s\n' "$Y" "$R"
  scan_local
  if [ $AUTO -eq 1 ]; then
    render
    [ $PEND -gt 0 ] && apply_pending
    report
    exit 0
  fi
  while :; do
    render
    [ $PEND -gt 0 ] && printf '\n%s* %d wanted change(s) not applied yet.%s\n' "$Y" "$PEND" "$R"
    if [ "$N" -gt 0 ]; then
      printf '\n[a] apply wanted changes   [1-%d] change one model   [u] upload a local model   [m] mount at login   [r] report only   [q] quit\n> ' "$N"
    else
      printf '\n[u] upload a local model   [m] mount at login   [r] report only   [q] quit\n> '
    fi
    read -r choice || break
    case "$choice" in
      a|A) apply_pending;;
      u|U) choose_upload;;
      u[0-9]*|U[0-9]*) n=${choice#[uU]}; if [ "$n" -ge 1 ] 2>/dev/null && [ "$n" -le "$NF" ]; then upload_model "${FOREIGN[$((n-1))]}"; else echo "No such entry."; fi;;
      m|M) setup_mount;;
      r|R) report;;
      q|Q) break;;
      "") ;;
      *) change_one "$choice";;
    esac
  done
  report
}

main "$@"
'''
