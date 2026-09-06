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
