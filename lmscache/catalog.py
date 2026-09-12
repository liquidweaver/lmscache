"""The library catalog. The folder tree is the source of truth; SQLite adds metadata we learned at download time."""

from __future__ import annotations

import os
import shutil
import threading
import time
from pathlib import Path

from . import config, db
from .util import quant_rank, valid_repo_id, variants_for

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
                fmt = detect_format(pub.name, repo.name, [f["path"] for f in files])
                try:
                    mtime = repo.stat().st_mtime
                except OSError:
                    mtime = time.time()
                variants, shared = variants_for(fmt, repo.name, files)
                found[mid] = {
                    "id": mid,
                    "publisher": pub.name,
                    "repo": repo.name,
                    "format": fmt,
                    "quants": [v["key"] for v in variants],
                    "variants": [
                        {"id": f"{mid}@{v['key']}", "key": v["key"], "label": v["label"], "kind": v["kind"], "files": v["files"], "bytes": v["size"], "file_count": len(v["files"])}
                        for v in sorted(variants, key=lambda v: quant_rank(v["key"]))
                    ],
                    "shared": shared,
                    "files": files,
                    "file_count": len(files),
                    "total_bytes": sum(f["size"] for f in files),
                    "added_at": meta.get("added_at") or mtime,
                    "source": meta.get("source"),
                    "revision": meta.get("revision"),
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
        slim = {k: v for k, v in m.items() if k not in ("files", "shared", "variants")}
        slim["variants"] = [{k: v for k, v in var.items() if k != "files"} for var in m["variants"]]
        slim["shared_count"] = len(m.get("shared") or [])
        out.append(slim)
    out.sort(key=lambda m: m["id"].lower())
    return out


def variant(vid: str) -> tuple[dict, dict] | None:
    """(model, variant) for a variant id like publisher/repo@Q4_K_M."""
    repo_id, _, key = vid.partition("@")
    m = get(repo_id)
    if not m:
        return None
    for v in m["variants"]:
        if v["key"] == key:
            return m, v
    return None


def delete_variant(vid: str) -> None:
    found = variant(vid)
    if not found:
        raise ValueError("no such variant")
    model, var = found
    if len(model["variants"]) <= 1:
        delete_model(model["id"])
        return
    root = (config.LIBRARY_DIR / model["id"]).resolve()
    for f in var["files"]:
        target = (root / f["path"]).resolve()
        if root not in target.parents:
            continue
        target.unlink(missing_ok=True)
    for dirpath, dirnames, filenames in os.walk(root, topdown=False):
        if dirpath != str(root) and not dirnames and not filenames:
            try:
                os.rmdir(dirpath)
            except OSError:
                pass
    scan()


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
