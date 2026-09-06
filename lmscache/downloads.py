"""Download queue. Each job runs `hf download` into .incoming/, then the files are moved atomically into the library."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import sys
import time
import uuid
from collections import deque
from pathlib import Path

from . import catalog, config, db, uploads
from .events import bus

ACTIVE = ("queued", "running", "finalizing")


def _hf_cmd() -> list[str]:
    exe = shutil.which("hf")
    if exe:
        return [exe]
    return [sys.executable, "-c", "import sys; from huggingface_hub.cli.hf import main; main()"]


def _tree_size(root: Path) -> int:
    total = 0
    for dirpath, _dirs, files in os.walk(root):
        for fn in files:
            try:
                total += os.lstat(os.path.join(dirpath, fn)).st_size
            except OSError:
                pass
    return total


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(8 * 1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


class Manager:
    def __init__(self) -> None:
        self.jobs: dict[str, dict] = {}
        self.procs: dict[str, asyncio.subprocess.Process] = {}
        self._wake: asyncio.Event | None = None
        self._task: asyncio.Task | None = None

    # ----- persistence -----
    def _load(self) -> None:
        for jid, data in db.query("SELECT id, data FROM jobs ORDER BY created_at"):
            job = json.loads(data)
            if job["status"] in ("running", "finalizing"):
                job["status"] = "queued"
                job["bytes_done"] = 0
            self.jobs[jid] = job

    def _persist(self, job: dict) -> None:
        db.put_json("jobs", "id", job["id"], "data", job, {"created_at": job["created_at"]})

    # ----- public API -----
    def list(self) -> list[dict]:
        return sorted(self.jobs.values(), key=lambda j: j["created_at"], reverse=True)

    def add(self, repo_id: str, revision: str | None, files: list[dict], whole_repo: bool, fmt: str, hf_meta: dict) -> dict:
        for j in self.jobs.values():
            if j["repo_id"] == repo_id and j["status"] in ACTIVE:
                raise ValueError(f"{repo_id} is already queued or downloading")
        if uploads.is_active(repo_id):
            raise ValueError(f"a machine is uploading {repo_id} right now")
        job = {
            "id": uuid.uuid4().hex[:12],
            "repo_id": repo_id,
            "revision": revision,
            "files": files,
            "whole_repo": whole_repo,
            "format": fmt,
            "hf": hf_meta,
            "total_bytes": sum(f.get("size") or 0 for f in files),
            "bytes_done": 0,
            "rate": 0.0,
            "status": "queued",
            "error": None,
            "log": "",
            "created_at": time.time(),
            "started_at": None,
            "finished_at": None,
        }
        self.jobs[job["id"]] = job
        self._persist(job)
        self._kick()
        bus.notify()
        return job

    def cancel(self, jid: str) -> dict:
        job = self._get(jid)
        if job["status"] in ("running", "finalizing"):
            job["status"] = "cancelled"
            proc = self.procs.get(jid)
            if proc and proc.returncode is None:
                proc.terminate()
        elif job["status"] == "queued":
            job["status"] = "cancelled"
        job["finished_at"] = time.time()
        self._persist(job)
        bus.notify()
        return job

    def retry(self, jid: str) -> dict:
        job = self._get(jid)
        if job["status"] in ACTIVE:
            return job
        job.update({"status": "queued", "error": None, "bytes_done": 0, "rate": 0.0, "finished_at": None})
        self._persist(job)
        self._kick()
        bus.notify()
        return job

    def remove(self, jid: str) -> None:
        job = self._get(jid)
        if job["status"] in ("running", "finalizing"):
            raise ValueError("cancel the job before removing it")
        self.jobs.pop(jid, None)
        db.execute("DELETE FROM jobs WHERE id = ?", (jid,))
        if not any(j["repo_id"] == job["repo_id"] for j in self.jobs.values()):
            shutil.rmtree(config.INCOMING_DIR / job["repo_id"], ignore_errors=True)
        bus.notify()

    def clear_finished(self) -> None:
        for jid in [j["id"] for j in self.jobs.values() if j["status"] in ("done", "cancelled")]:
            self.remove(jid)

    # ----- worker -----
    async def start(self) -> None:
        self._wake = asyncio.Event()
        self._load()
        self._task = asyncio.create_task(self._loop())

    def _kick(self) -> None:
        if self._wake is not None:
            self._wake.set()

    def _get(self, jid: str) -> dict:
        if jid not in self.jobs:
            raise KeyError(jid)
        return self.jobs[jid]

    async def _loop(self) -> None:
        assert self._wake is not None
        while True:
            try:
                settings = config.load()
                running = sum(1 for j in self.jobs.values() if j["status"] in ("running", "finalizing"))
                queued = [j for j in sorted(self.jobs.values(), key=lambda j: j["created_at"]) if j["status"] == "queued"]
                for job in queued[: max(0, settings["max_parallel"] - running)]:
                    job["status"] = "running"
                    asyncio.create_task(self._run(job))
            except Exception as exc:  # never let the scheduler die
                print("scheduler error:", exc, file=sys.stderr)
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=3.0)
            except asyncio.TimeoutError:
                pass

    async def _run(self, job: dict) -> None:
        job["started_at"] = time.time()
        job["error"] = None
        self._persist(job)
        bus.notify()
        settings = config.load()
        dest = config.INCOMING_DIR / job["repo_id"]
        dest.mkdir(parents=True, exist_ok=True)

        env = {
            **os.environ,
            "HF_HOME": str(config.HF_HOME),
            "HF_HUB_DISABLE_TELEMETRY": "1",
            "HF_HUB_DISABLE_PROGRESS_BARS": "1",
            "HF_XET_HIGH_PERFORMANCE": "1" if settings.get("xet_high_performance") else "0",
            "HF_XET_RECONSTRUCT_WRITE_SEQUENTIALLY": "1",
        }
        env.pop("HF_TOKEN", None)
        if settings.get("hf_token"):
            env["HF_TOKEN"] = settings["hf_token"]

        cmd = [*_hf_cmd(), "download", job["repo_id"]]
        if not job["whole_repo"]:
            cmd += [f["path"] for f in job["files"]]
        cmd += ["--local-dir", str(dest)]
        if job.get("revision"):
            cmd += ["--revision", job["revision"]]

        lines: deque[str] = deque(maxlen=30)
        poller = asyncio.create_task(self._poll_progress(job, dest))
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
            )
            self.procs[job["id"]] = proc
            assert proc.stdout is not None
            async for raw in proc.stdout:
                text = raw.decode(errors="replace").rstrip()
                if text:
                    lines.append(text[-400:])
                    job["log"] = "\n".join(lines)
            rc = await proc.wait()
        except Exception as exc:
            rc = -1
            lines.append(f"failed to start hf: {exc}")
            job["log"] = "\n".join(lines)
        finally:
            poller.cancel()
            self.procs.pop(job["id"], None)

        if job["status"] == "cancelled":
            self._persist(job)
            bus.notify()
            self._kick()
            return
        if rc != 0:
            job["status"] = "failed"
            job["error"] = f"hf download exited with code {rc}. " + (lines[-1] if lines else "")
        else:
            job["status"] = "finalizing"
            bus.notify()
            try:
                await asyncio.to_thread(self._finalize, job, dest, settings)
            except Exception as exc:
                job["status"] = "failed"
                job["error"] = str(exc)
            else:
                job["status"] = "done"
                job["bytes_done"] = job["total_bytes"]
        job["finished_at"] = time.time()
        job["rate"] = 0.0
        self._persist(job)
        bus.notify()
        self._kick()

    async def _poll_progress(self, job: dict, dest: Path) -> None:
        last_bytes = 0
        last_t = time.time()
        rate = 0.0
        while True:
            await asyncio.sleep(1.0)
            try:
                done = await asyncio.to_thread(_tree_size, dest)
            except Exception:
                continue
            now = time.time()
            inst = (done - last_bytes) / max(now - last_t, 1e-3)
            rate = inst if rate == 0 else 0.7 * rate + 0.3 * inst
            last_bytes, last_t = done, now
            job["bytes_done"] = min(done, job["total_bytes"]) if job["total_bytes"] else done
            job["rate"] = max(0.0, rate)
            bus.notify("progress")

    def _finalize(self, job: dict, dest: Path, settings: dict) -> None:
        shutil.rmtree(dest / ".cache", ignore_errors=True)
        expected = job["files"]
        for f in expected:
            p = dest / f["path"]
            if not p.is_file():
                raise RuntimeError(f"missing file after download: {f['path']}")
            if f.get("size") and p.stat().st_size != f["size"]:
                raise RuntimeError(f"size mismatch for {f['path']}: got {p.stat().st_size}, expected {f['size']}")
            if settings.get("verify_checksums") and f.get("sha256"):
                job["log"] = (job.get("log") or "") + f"\nverifying {f['path']}"
                if _sha256(p) != f["sha256"]:
                    raise RuntimeError(f"checksum mismatch for {f['path']}")

        catalog.place(dest, job["repo_id"], expected)

        meta = db.get_json("models", "id", job["repo_id"]) or {}
        shas = dict(meta.get("sha256") or {})
        for f in expected:
            if f.get("sha256"):
                shas[f["path"]] = f["sha256"]
        meta.update(
            {
                "added_at": meta.get("added_at") or time.time(),
                "revision": job.get("revision"),
                "hf": job.get("hf"),
                "sha256": shas,
            }
        )
        catalog.remember(job["repo_id"], meta)
        catalog.scan()


manager = Manager()
