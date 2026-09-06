"""The library catalog. The folder tree is the source of truth; SQLite adds metadata we learned at download time."""

from __future__ import annotations

import os
import shutil
import threading
import time
from pathlib import Path

from . import config, db
from .util import detect_quant, is_mmproj, valid_repo_id

_lock = threading.Lock()
_models: dict[str, dict] = {}
_scanned_at: float = 0.0


def _walk_files(root: Path) -> list[dict]:
    files: list[dict] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for fn in filenames:
            if fn.startswith("."):
                continue
            p = Path(dirpath) / fn
            try:
                if p.is_symlink():
                    continue
                size = p.stat().st_size
            except OSError:
                continue
            files.append({"path": p.relative_to(root).as_posix(), "size": size})
    files.sort(key=lambda f: f["path"])
    return files


def detect_format(publisher: str, repo: str, paths: list[str], tags: list[str] | None = None) -> str:
    low = [p.lower() for p in paths]
    if any(p.endswith(".gguf") for p in low):
        return "gguf"
    if any(p.endswith(".safetensors") for p in low):
        tags = [t.lower() for t in (tags or [])]
        if "mlx" in tags or "mlx" in publisher.lower() or "mlx" in repo.lower():
            return "mlx"
        return "safetensors"
    return "other"


def quants_for(fmt: str, repo: str, paths: list[str]) -> list[str]:
    qs: set[str] = set()
    if fmt == "gguf":
        for p in paths:
            if p.lower().endswith(".gguf") and not is_mmproj(p):
                q = detect_quant(p)
                if q:
                    qs.add(q)
    else:
        q = detect_quant(repo)
        if q:
            qs.add(q)
    return sorted(qs)


def scan() -> dict[str, dict]:
    global _models, _scanned_at
    lib = config.LIBRARY_DIR
    metas = db.all_json("models", "id", "meta")
    found: dict[str, dict] = {}
    if lib.exists():
        for pub in sorted(lib.iterdir()):
            if not pub.is_dir() or pub.name.startswith("."):
                continue
            for repo in sorted(pub.iterdir()):
                if not repo.is_dir() or repo.name.startswith("."):
                    continue
                mid = f"{pub.name}/{repo.name}"
                files = _walk_files(repo)
                meta = metas.get(mid) or {}
                paths = [f["path"] for f in files]
                tags = (meta.get("hf") or {}).get("tags") or []
                fmt = detect_format(pub.name, repo.name, paths, tags)
                try:
                    mtime = repo.stat().st_mtime
                except OSError:
                    mtime = time.time()
                found[mid] = {
                    "id": mid,
                    "publisher": pub.name,
                    "repo": repo.name,
                    "format": fmt,
                    "quants": quants_for(fmt, repo.name, paths),
                    "files": files,
                    "file_count": len(files),
                    "total_bytes": sum(f["size"] for f in files),
                    "added_at": meta.get("added_at") or mtime,
                    "revision": meta.get("revision"),
                    "hf": meta.get("hf"),
                    "source": meta.get("source"),
                }
    with _lock:
        _models = found
        _scanned_at = time.time()
    return found


def models() -> dict[str, dict]:
    with _lock:
        return dict(_models)


def get(mid: str) -> dict | None:
    with _lock:
        return _models.get(mid)


def scanned_at() -> float:
    return _scanned_at


def summary() -> list[dict]:
    """Catalog without per-file lists, for the state payload."""
    out = []
    for m in models().values():
        out.append({k: v for k, v in m.items() if k != "files"})
    out.sort(key=lambda m: m["id"].lower())
    return out


def remember(mid: str, meta: dict) -> None:
    db.put_json("models", "id", mid, "meta", meta)


def forget(mid: str) -> None:
    db.execute("DELETE FROM models WHERE id = ?", (mid,))


def delete_model(mid: str) -> None:
    if not valid_repo_id(mid):
        raise ValueError("invalid model id")
    target = (config.LIBRARY_DIR / mid).resolve()
    lib = config.LIBRARY_DIR.resolve()
    if lib not in target.parents:
        raise ValueError("refusing to delete outside the library")
    if target.exists():
        shutil.rmtree(target)
        try:
            target.parent.rmdir()  # drop the publisher folder if now empty
        except OSError:
            pass
    forget(mid)
    scan()


def place(src_dir: Path, repo_id: str, files: list[dict]) -> None:
    """Move verified files from a scratch folder into the library. A brand-new model is a single atomic rename."""
    lib_dest = config.LIBRARY_DIR / repo_id
    if not lib_dest.exists():
        lib_dest.parent.mkdir(parents=True, exist_ok=True)
        os.rename(src_dir, lib_dest)
    else:
        for f in files:
            src, dst = src_dir / f["path"], lib_dest / f["path"]
            dst.parent.mkdir(parents=True, exist_ok=True)
            os.replace(src, dst)
        shutil.rmtree(src_dir, ignore_errors=True)
    try:
        (config.INCOMING_DIR / repo_id.split("/")[0]).rmdir()
    except OSError:
        pass


def disk() -> dict:
    try:
        u = shutil.disk_usage(config.MODELS_ROOT)
        return {"total": u.total, "used": u.used, "free": u.free}
    except OSError:
        return {"total": 0, "used": 0, "free": 0}
