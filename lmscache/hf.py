"""Hugging Face Hub lookups: search, repo file trees, quant grouping."""

from __future__ import annotations

import asyncio
import re

from huggingface_hub import HfApi

from .catalog import detect_format
from .util import detect_quant, is_mmproj

SORT_MAP = {"downloads": "downloads", "likes": "likes", "updated": "lastModified", "trending": "trendingScore"}
FORMAT_TAG = {"gguf": "gguf", "mlx": "mlx", "safetensors": "safetensors"}
_EXPAND = ["downloads", "likes", "lastModified", "gated", "pipeline_tag", "tags", "createdAt"]


def _api(token: str | None) -> HfApi:
    return HfApi(token=token or None)


def _gated(v) -> bool:
    return bool(v) and v is not False


def _iso(dt) -> str | None:
    return dt.isoformat() if dt else None


def _format_from_tags(tags: list[str], repo_id: str) -> str:
    t = [x.lower() for x in tags]
    if "gguf" in t or repo_id.lower().endswith("-gguf") or "gguf" in repo_id.lower():
        return "gguf"
    if "mlx" in t or repo_id.lower().startswith("mlx-community/"):
        return "mlx"
    if "safetensors" in t:
        return "safetensors"
    return "other"


def _search_sync(q: str, fmt: str, sort: str, limit: int, token: str | None) -> list[dict]:
    api = _api(token)
    kwargs: dict = {"search": q or None, "sort": SORT_MAP.get(sort, "downloads"), "limit": limit}
    if fmt in FORMAT_TAG:
        kwargs["filter"] = FORMAT_TAG[fmt]
    try:
        infos = list(api.list_models(expand=_EXPAND, **kwargs))
    except Exception:
        infos = list(api.list_models(**kwargs))
    out = []
    for m in infos:
        tags = list(m.tags or [])
        out.append(
            {
                "id": m.id,
                "downloads": m.downloads,
                "likes": m.likes,
                "updated": _iso(m.last_modified),
                "gated": _gated(getattr(m, "gated", None)),
                "pipeline_tag": m.pipeline_tag,
                "format": _format_from_tags(tags, m.id),
            }
        )
    return out


async def search(q: str, fmt: str, sort: str, limit: int, token: str | None) -> list[dict]:
    return await asyncio.to_thread(_search_sync, q, fmt, sort, limit, token)


def _quant_rank(key: str) -> tuple:
    m = re.search(r"(\d+)", key)
    bits = int(m.group(1)) if m else 99
    if key.upper() in ("F16", "BF16", "FP16"):
        bits = 16
    if key.upper() in ("F32",):
        bits = 32
    if key.upper() in ("FP8",):
        bits = 8
    return (bits, key)


def group_files(files: list[dict], fmt: str) -> list[dict]:
    total = sum(f["size"] or 0 for f in files)
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
    quant_keys = sorted((k for k in buckets if k not in ("mmproj", "other")), key=_quant_rank)
    groups = []
    for k in quant_keys:
        groups.append({"key": k, "label": k, "kind": "quant", "files": buckets[k], "size": sum(x["size"] or 0 for x in buckets[k])})
    if "mmproj" in buckets:
        groups.append({"key": "mmproj", "label": "Vision projector (mmproj)", "kind": "mmproj", "files": buckets["mmproj"], "size": sum(x["size"] or 0 for x in buckets["mmproj"])})
    if "other" in buckets:
        groups.append({"key": "other", "label": "Other files (README, imatrix, configs)", "kind": "other", "files": buckets["other"], "size": sum(x["size"] or 0 for x in buckets["other"])})
    return groups


def _repo_info_sync(repo_id: str, token: str | None) -> dict:
    api = _api(token)
    info = api.model_info(repo_id, files_metadata=True)
    files = []
    for s in info.siblings or []:
        lfs = getattr(s, "lfs", None)
        sha = None
        if lfs is not None:
            sha = lfs.get("sha256") if isinstance(lfs, dict) else getattr(lfs, "sha256", None)
        files.append({"path": s.rfilename, "size": s.size or 0, "sha256": sha})
    files.sort(key=lambda f: f["path"])
    tags = list(info.tags or [])
    pub, repo = repo_id.split("/", 1)
    fmt = detect_format(pub, repo, [f["path"] for f in files], tags)
    params = None
    st = getattr(info, "safetensors", None)
    if st is not None:
        params = getattr(st, "total", None) or (st.get("total") if isinstance(st, dict) else None)
    gg = getattr(info, "gguf", None)
    if params is None and isinstance(gg, dict):
        params = gg.get("total")
    return {
        "id": info.id,
        "revision": info.sha,
        "gated": _gated(getattr(info, "gated", None)),
        "downloads": info.downloads,
        "likes": info.likes,
        "updated": _iso(info.last_modified),
        "pipeline_tag": info.pipeline_tag,
        "tags": tags,
        "format": fmt,
        "params": params,
        "files": files,
        "groups": group_files(files, fmt),
        "total_bytes": sum(f["size"] for f in files),
    }


async def repo_info(repo_id: str, token: str | None) -> dict:
    return await asyncio.to_thread(_repo_info_sync, repo_id, token)
