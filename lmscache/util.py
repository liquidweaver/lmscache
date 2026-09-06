"""Shared helpers: repo id validation, quant detection, formatting."""

from __future__ import annotations

import re

REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$")

# Order matters: longer / more specific first.
_QUANT_RE = re.compile(
    r"(?<![A-Za-z0-9])("
    r"UD-(?:IQ|Q|TQ)[0-9][A-Z0-9_]*(?:_XL|_L|_M|_S|_XS|_XXS)?"
    r"|(?:IQ|TQ)[1-4]_[A-Z0-9_]+"
    r"|Q[1-8]_K_[SML](?:_XL|_L)?|Q[1-8]_K(?:_XL|_L)?|Q[1-8]_[0-9](?:_XL|_L)?|Q[1-8]_[A-Z]"
    r"|MXFP4(?:_MOE)?|NVFP4|F16|BF16|F32|FP16|FP8"
    r"|[1-8]bit"
    r")(?![A-Za-z0-9])",
    re.IGNORECASE,
)


def valid_repo_id(repo_id: str) -> bool:
    return bool(REPO_RE.match(repo_id or "")) and ".." not in repo_id


def detect_quant(path: str) -> str | None:
    """Best-effort quantization label from a file or folder path."""
    m = None
    for m in _QUANT_RE.finditer(path):
        pass  # keep the last match; the quant usually trails the model name
    if not m:
        return None
    q = m.group(1)
    return q.upper() if not q.lower().endswith("bit") else q.lower()


def is_mmproj(path: str) -> bool:
    name = path.rsplit("/", 1)[-1].lower()
    return name.startswith("mmproj") or "mmproj" in name


def human_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} TB"


VARIANT_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*)@([A-Za-z0-9][A-Za-z0-9._-]*)$")


def valid_variant_id(vid: str) -> bool:
    m = VARIANT_RE.match(vid or "")
    return bool(m) and ".." not in vid


def split_variant_id(vid: str) -> tuple[str, str]:
    repo, _, key = vid.partition("@")
    return repo, key


def quant_rank(key: str) -> tuple:
    m = re.search(r"(\d+)", key)
    bits = int(m.group(1)) if m else 99
    up = key.upper()
    if up in ("F16", "BF16", "FP16"):
        bits = 16
    if up == "F32":
        bits = 32
    if up == "FP8":
        bits = 8
    return (bits, key)


def group_files(files: list[dict], fmt: str) -> list[dict]:
    """Group a repo's files. GGUF: one group per quant plus mmproj and other; anything else: one group."""
    total = sum(f.get("size") or 0 for f in files)
    if fmt != "gguf":
        return [{"key": "all", "label": "Whole repository", "kind": "all", "files": files, "size": total}]
    buckets: dict[str, list[dict]] = {}
    for f in files:
        p = f["path"]
        if p.lower().endswith(".gguf"):
            key = "mmproj" if is_mmproj(p) else (detect_quant(p) or "gguf")
        else:
            key = "other"
        buckets.setdefault(key, []).append(f)
    quant_keys = sorted((k for k in buckets if k not in ("mmproj", "other")), key=quant_rank)
    groups = [{"key": k, "label": k, "kind": "quant", "files": buckets[k], "size": sum(x.get("size") or 0 for x in buckets[k])} for k in quant_keys]
    if "mmproj" in buckets:
        groups.append({"key": "mmproj", "label": "Vision projector (mmproj)", "kind": "mmproj", "files": buckets["mmproj"], "size": sum(x.get("size") or 0 for x in buckets["mmproj"])})
    if "other" in buckets:
        groups.append({"key": "other", "label": "Other files (README, imatrix, configs)", "kind": "other", "files": buckets["other"], "size": sum(x.get("size") or 0 for x in buckets["other"])})
    return groups


def variants_for(fmt: str, repo: str, files: list[dict]) -> tuple[list[dict], list[dict]]:
    """Loadable variants of a repo folder plus the files shared by all of them (mmproj, README, configs)."""
    if fmt == "gguf":
        groups = group_files(files, "gguf")
        variants = [g for g in groups if g["kind"] == "quant"]
        shared = [f for g in groups if g["kind"] in ("mmproj", "other") for f in g["files"]]
        return variants, shared
    key = detect_quant(repo) or "all"
    label = key if key != "all" else fmt
    return [{"key": key, "label": label, "kind": "all", "files": files, "size": sum(f.get("size") or 0 for f in files)}], []
