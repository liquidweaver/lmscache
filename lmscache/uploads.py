"""Uploads from client machines. Files stream into .incoming/<publisher>/<repo>; a commit moves them into the library."""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import time
from pathlib import Path

from . import catalog, config, db
from .events import bus

_active: dict[str, float] = {}  # repo_id -> last activity
_RANGE = re.compile(r"^bytes\s+(\d+)-(\d+)?/(\d+|\*)$")
BUFFER = 8 * 1024 * 1024


class OffsetMismatch(Exception):
    def __init__(self, current: int) -> None:
        super().__init__(f"server has {current} bytes")
        self.current = current


class NoSpace(Exception):
    pass


def touch(repo_id: str) -> None:
    _active[repo_id] = time.time()


def is_active(repo_id: str, within: float = 600) -> bool:
    return time.time() - _active.get(repo_id, 0) < within


def finish(repo_id: str) -> None:
    _active.pop(repo_id, None)


def safe_rel_path(path: str) -> str:
    parts = [p for p in path.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." or p.startswith(".") for p in parts):
        raise ValueError("invalid file path")
    return "/".join(parts)


def scratch_dir(repo_id: str) -> Path:
    return config.INCOMING_DIR / repo_id


def status(repo_id: str) -> dict[str, int]:
    """Bytes already received per file, so a client can resume."""
    root = scratch_dir(repo_id)
    out: dict[str, int] = {}
    if root.exists():
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for fn in filenames:
                if fn.startswith("."):
                    continue
                p = Path(dirpath) / fn
                try:
                    out[p.relative_to(root).as_posix()] = p.stat().st_size
                except OSError:
                    pass
    return out


def parse_range(header: str | None) -> int:
    if not header:
        return 0
    m = _RANGE.match(header.strip())
    if not m:
        raise ValueError("bad Content-Range header")
    return int(m.group(1))


async def receive(repo_id: str, rel: str, request, start: int) -> int:
    target = scratch_dir(repo_id) / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    current = target.stat().st_size if target.exists() else 0
    if start != current:
        raise OffsetMismatch(current)
    length = int(request.headers.get("content-length") or 0)
    free = shutil.disk_usage(config.MODELS_ROOT).free
    if length and length > free - 2 * 1024**3:
        raise NoSpace()
    touch(repo_id)
    buf = bytearray()
    with open(target, "ab" if start else "wb") as fh:
        async for chunk in request.stream():
            if not chunk:
                continue
            buf += chunk
            if len(buf) >= BUFFER:
                data, buf = bytes(buf), bytearray()
                await asyncio.to_thread(fh.write, data)
                touch(repo_id)
        if buf:
            await asyncio.to_thread(fh.write, bytes(buf))
    touch(repo_id)
    return target.stat().st_size


def commit(repo_id: str, files: list[dict], machine: str | None) -> dict | None:
    root = scratch_dir(repo_id)
    if not root.exists():
        raise ValueError("nothing has been uploaded for this model")
    clean: list[dict] = []
    for f in files:
        rel = safe_rel_path(str(f.get("path") or ""))
        size = int(f.get("size") or 0)
        p = root / rel
        if not p.is_file():
            raise ValueError(f"missing file {rel}")
        if p.stat().st_size != size:
            raise ValueError(f"size mismatch for {rel}: server has {p.stat().st_size}, client says {size}")
        clean.append({"path": rel, "size": size})
    if not clean:
        raise ValueError("empty file list")
    keep = {c["path"] for c in clean}
    for rel in status(repo_id):
        if rel not in keep:
            (root / rel).unlink(missing_ok=True)
    shutil.rmtree(root / ".cache", ignore_errors=True)
    catalog.place(root, repo_id, clean)
    meta = db.get_json("models", "id", repo_id) or {}
    meta.update({"added_at": meta.get("added_at") or time.time(), "source": {"kind": "upload", "machine": machine, "at": time.time()}})
    catalog.remember(repo_id, meta)
    finish(repo_id)
    catalog.scan()
    bus.notify()
    return catalog.get(repo_id)


def abort(repo_id: str) -> None:
    shutil.rmtree(scratch_dir(repo_id), ignore_errors=True)
    finish(repo_id)
    try:
        (config.INCOMING_DIR / repo_id.split("/")[0]).rmdir()
    except OSError:
        pass
