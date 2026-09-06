"""FastAPI application: JSON API for the web UI, the per-machine client script, and static files."""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import catalog, config, db, hf, machines, uploads
from .downloads import manager
from .events import bus
from .util import valid_repo_id, valid_variant_id

STATIC = Path(__file__).parent / "static"


async def _periodic_rescan() -> None:
    while True:
        await asyncio.sleep(120)
        try:
            before = {k: v["total_bytes"] for k, v in catalog.models().items()}
            await asyncio.to_thread(catalog.scan)
            after = {k: v["total_bytes"] for k, v in catalog.models().items()}
            if before != after:
                bus.notify()
        except Exception:
            pass


@asynccontextmanager
async def lifespan(_app: FastAPI):
    config.ensure_dirs()
    config.load()
    bus.bind(asyncio.get_running_loop())
    await asyncio.to_thread(catalog.scan)
    await manager.start()
    task = asyncio.create_task(_periodic_rescan())
    yield
    task.cancel()


app = FastAPI(title="LMS Cache", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


# ----- request bodies -----
class SettingsIn(BaseModel):
    hf_token: str | None = None
    preferred_quant: str | None = None
    max_parallel: int | None = None
    xet_high_performance: bool | None = None
    verify_checksums: bool | None = None
    public_url: str | None = None
    smb_host: str | None = None
    smb_share: str | None = None
    smb_user: str | None = None
    smb_password: str | None = None


class DownloadIn(BaseModel):
    repo_id: str
    files: list[str] = []
    whole_repo: bool = False


class MachineIn(BaseModel):
    name: str
    rename_from: str | None = None
    os: str = "mac"
    models_dir: str | None = None
    mount: str | None = None
    smb_user: str | None = None
    link_mode: str | None = None


class IntentIn(BaseModel):
    state: str


class UploadFileIn(BaseModel):
    path: str
    size: int


class CommitIn(BaseModel):
    files: list[UploadFileIn]
    machine: str | None = None


# ----- helpers -----
def _model_id(publisher: str, repo: str) -> str:
    mid = f"{publisher}/{repo}"
    if not valid_repo_id(mid):
        raise HTTPException(400, "invalid model id")
    return mid


def _variant_id(publisher: str, repo: str) -> str:
    """publisher/repo@QUANT; must exist in the library."""
    vid = f"{publisher}/{repo}"
    if not valid_variant_id(vid):
        raise HTTPException(400, "expected a variant id like publisher/repo@Q4_K_M")
    if not catalog.variant(vid):
        raise HTTPException(404, "no such variant in the library")
    return vid


def _machine(name: str) -> dict:
    m = machines.get(name)
    if not m:
        raise HTTPException(404, f"unknown machine {name}")
    return m


def _state_payload(request: Request) -> dict:
    settings = config.load()
    base = machines.base_url(request, settings)
    models = catalog.models()
    machs = machines.list_machines()
    names = [m["name"] for m in machs]
    intents = machines.intents()
    reports = machines.reports()
    for m in machs:
        m["report"] = machines.report_summary(reports.get(m["name"]))
        m["one_liner"] = machines.one_liner(m, base)
    return {
        "models": catalog.summary(),
        "machines": machs,
        "cells": machines.cells(models, names, intents, reports),
        "foreign": machines.foreign(models, reports),
        "jobs": manager.list(),
        "settings": config.public(settings),
        "disk": catalog.disk(),
        "library_dir": str(config.LIBRARY_DIR),
        "base_url": base,
        "scanned_at": catalog.scanned_at(),
        "now": time.time(),
    }


# ----- pages -----
@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/state")
async def state(request: Request):
    return _state_payload(request)


@app.get("/api/events")
async def events(request: Request):
    q = bus.subscribe()

    async def gen():
        try:
            yield "event: hello\ndata: {}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    kind = await asyncio.wait_for(q.get(), timeout=15)
                    while not q.empty():
                        q.get_nowait()
                    yield f"event: {kind}\ndata: {{}}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            bus.unsubscribe(q)

    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ----- settings -----
@app.get("/api/settings")
async def get_settings():
    return config.public(config.load())


@app.get("/api/settings/reveal/{key}")
async def reveal_setting(key: str):
    """Return a stored secret for display in the UI. The UI is unauthenticated, so this is LAN-only by design."""
    if key not in ("hf_token", "smb_password"):
        raise HTTPException(404, "unknown setting")
    return {"key": key, "value": config.load().get(key) or ""}


@app.put("/api/settings")
async def put_settings(body: SettingsIn):
    update = {k: v for k, v in body.model_dump().items() if v is not None}
    saved = config.save(update)
    bus.notify()
    return config.public(saved)


# ----- library -----
@app.get("/api/library/{publisher}/{repo}")
async def library_model(publisher: str, repo: str):
    m = catalog.get(_model_id(publisher, repo))
    if not m:
        raise HTTPException(404, "not in library")
    return m


@app.post("/api/library/rescan")
async def rescan():
    await asyncio.to_thread(catalog.scan)
    bus.notify()
    return {"count": len(catalog.models())}


@app.delete("/api/library/{publisher}/{repo}")
async def delete_model(publisher: str, repo: str, variant: str | None = None):
    mid = _model_id(publisher, repo)
    model = catalog.get(mid)
    if not model:
        raise HTTPException(404, "not in library")
    for job in manager.list():
        if job["repo_id"] == mid and job["status"] in ("queued", "running", "finalizing"):
            raise HTTPException(409, "a download for this model is still active")
    if variant:
        vid = f"{mid}@{variant}"
        if not catalog.variant(vid):
            raise HTTPException(404, "no such variant")
        gone = [vid] if len(model["variants"]) > 1 else [v["id"] for v in model["variants"]]
        try:
            await asyncio.to_thread(catalog.delete_variant, vid)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
    else:
        gone = [v["id"] for v in model["variants"]]
        try:
            await asyncio.to_thread(catalog.delete_model, mid)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
    machines.clear_intents_for(gone)
    bus.notify()
    return {"deleted": gone}


# ----- hugging face -----
@app.get("/api/search")
async def search(q: str = "", format: str = "any", sort: str = "downloads", limit: int = 30):
    token = config.load().get("hf_token") or None
    try:
        results = await hf.search(q.strip(), format, sort, max(1, min(100, limit)), token)
    except Exception as exc:
        raise HTTPException(502, f"Hugging Face search failed: {exc}")
    have = catalog.models()
    for r in results:
        r["in_library"] = r["id"] in have
    return {"results": results}


@app.get("/api/repo/{publisher}/{repo}")
async def repo(publisher: str, repo: str):
    mid = _model_id(publisher, repo)
    token = config.load().get("hf_token") or None
    try:
        info = await hf.repo_info(mid, token)
    except Exception as exc:
        raise HTTPException(502, f"Hugging Face lookup failed: {exc}")
    existing = catalog.get(mid)
    info["in_library"] = bool(existing)
    info["library_files"] = [f["path"] for f in existing["files"]] if existing else []
    info["preferred_quant"] = config.load().get("preferred_quant") or ""
    return info


# ----- downloads -----
@app.get("/api/downloads")
async def downloads():
    return {"jobs": manager.list()}


@app.post("/api/downloads")
async def add_download(body: DownloadIn):
    if not valid_repo_id(body.repo_id):
        raise HTTPException(400, "invalid repo id")
    token = config.load().get("hf_token") or None
    try:
        info = await hf.repo_info(body.repo_id, token)
    except Exception as exc:
        raise HTTPException(502, f"Hugging Face lookup failed: {exc}")
    if body.whole_repo:
        selected = info["files"]
    else:
        wanted = set(body.files)
        selected = [f for f in info["files"] if f["path"] in wanted]
    if not selected:
        raise HTTPException(400, "no files selected")
    hf_meta = {k: info.get(k) for k in ("downloads", "likes", "tags", "pipeline_tag", "gated", "params", "updated")}
    try:
        job = manager.add(body.repo_id, info.get("revision"), selected, body.whole_repo, info["format"], hf_meta)
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    return job


@app.post("/api/downloads/clear")
async def clear_downloads():
    manager.clear_finished()
    return {"ok": True}


@app.post("/api/downloads/{jid}/cancel")
async def cancel_download(jid: str):
    try:
        return manager.cancel(jid)
    except KeyError:
        raise HTTPException(404, "no such job")


@app.post("/api/downloads/{jid}/retry")
async def retry_download(jid: str):
    try:
        return manager.retry(jid)
    except KeyError:
        raise HTTPException(404, "no such job")


@app.delete("/api/downloads/{jid}")
async def remove_download(jid: str):
    try:
        manager.remove(jid)
    except KeyError:
        raise HTTPException(404, "no such job")
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    return {"ok": True}


# ----- uploads from machines -----
def _upload_allowed(mid: str) -> None:
    for job in manager.list():
        if job["repo_id"] == mid and job["status"] in ("queued", "running", "finalizing"):
            raise HTTPException(409, "a download for this model is running on the NAS")


async def _enrich_from_hub(mid: str) -> None:
    """Best effort: attach Hub metadata to an uploaded model so the UI can show downloads, tags and gating."""
    try:
        info = await hf.repo_info(mid, config.load().get("hf_token") or None)
    except Exception:
        return
    meta = db.get_json("models", "id", mid)
    if meta is None or not catalog.get(mid):
        return
    meta["hf"] = {k: info.get(k) for k in ("downloads", "likes", "tags", "pipeline_tag", "gated", "params", "updated")}
    catalog.remember(mid, meta)
    await asyncio.to_thread(catalog.scan)
    bus.notify()


@app.get("/api/upload/{publisher}/{repo}", response_class=PlainTextResponse)
async def upload_status(publisher: str, repo: str):
    mid = _model_id(publisher, repo)
    _upload_allowed(mid)
    lines = [f"{path}\t{size}" for path, size in sorted(uploads.status(mid).items())]
    return "\n".join(lines) + ("\n" if lines else "")


@app.put("/api/upload/{publisher}/{repo}/{path:path}")
async def upload_file(publisher: str, repo: str, path: str, request: Request):
    mid = _model_id(publisher, repo)
    _upload_allowed(mid)
    try:
        rel = uploads.safe_rel_path(path)
        start = uploads.parse_range(request.headers.get("content-range"))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    try:
        size = await uploads.receive(mid, rel, request, start)
    except uploads.OffsetMismatch as exc:
        raise HTTPException(409, f"server has {exc.current} bytes of {rel}; resume from that offset")
    except uploads.NoSpace:
        raise HTTPException(507, "not enough free space on the NAS")
    return {"path": rel, "size": size}


@app.post("/api/upload/{publisher}/{repo}/commit")
async def upload_commit(publisher: str, repo: str, body: CommitIn):
    mid = _model_id(publisher, repo)
    _upload_allowed(mid)
    try:
        model = await asyncio.to_thread(uploads.commit, mid, [f.model_dump() for f in body.files], body.machine)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    asyncio.create_task(_enrich_from_hub(mid))
    return model


@app.delete("/api/upload/{publisher}/{repo}")
async def upload_abort(publisher: str, repo: str):
    mid = _model_id(publisher, repo)
    _upload_allowed(mid)
    await asyncio.to_thread(uploads.abort, mid)
    return {"ok": True}


# ----- machines -----
@app.get("/api/machines")
async def list_machines(request: Request):
    base = machines.base_url(request, config.load())
    out = machines.list_machines()
    for m in out:
        m["one_liner"] = machines.one_liner(m, base)
    return {"machines": out}


@app.post("/api/machines")
async def upsert_machine(body: MachineIn):
    data = body.model_dump()
    old = data.pop("rename_from", None)
    try:
        if old and old != data["name"]:
            if not machines.get(old):
                raise HTTPException(404, f"unknown machine {old}")
            machines.rename(old, data["name"])
        profile = machines.upsert(data)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    bus.notify()
    return profile


@app.delete("/api/machines/{name}")
async def delete_machine(name: str):
    _machine(name)
    machines.delete(name)
    bus.notify()
    return {"ok": True}


@app.put("/api/machines/{name}/models/{publisher}/{repo}")
async def set_intent(name: str, publisher: str, repo: str, body: IntentIn):
    _machine(name)
    vid = _variant_id(publisher, repo)
    try:
        machines.set_intent(name, vid, body.state)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    bus.notify()
    return {"ok": True}


@app.delete("/api/machines/{name}/models/{publisher}/{repo}")
async def clear_intent(name: str, publisher: str, repo: str):
    _machine(name)
    machines.clear_intent(name, f"{publisher}/{repo}")
    bus.notify()
    return {"ok": True}


@app.post("/api/machines/{name}/report", response_class=PlainTextResponse)
async def report(name: str, request: Request):
    """The client script posts what the machine holds and gets back the plan it works from."""
    _machine(name)
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(400, "body must be JSON")
    rep = machines.store_report(name, payload if isinstance(payload, dict) else {})
    bus.notify()
    return machines.plan_text(catalog.models(), name, rep)


@app.get("/api/machines/{name}/plan", response_class=PlainTextResponse)
async def plan(name: str):
    _machine(name)
    return machines.plan_text(catalog.models(), name, machines.reports().get(name))


@app.get("/lmsc/{name}.sh", response_class=PlainTextResponse)
async def client_script(name: str, request: Request):
    m = _machine(name)
    settings = config.load()
    script = machines.client_script(m, machines.base_url(request, settings), machines.smb_host(request, settings), settings)
    return PlainTextResponse(script, media_type="text/x-shellscript")
