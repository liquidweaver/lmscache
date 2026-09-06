"""FastAPI application: JSON API for the web UI, uploads from machines, the per-machine client script, static files."""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import catalog, config, machines, uploads
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
    task = asyncio.create_task(_periodic_rescan())
    yield
    task.cancel()


app = FastAPI(title="LMS Cache", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


# ----- request bodies -----
class SettingsIn(BaseModel):
    public_url: str | None = None
    smb_host: str | None = None
    smb_share: str | None = None
    smb_user: str | None = None
    smb_password: str | None = None


class MachineIn(BaseModel):
    name: str
    rename_from: str | None = None
    os: str = "mac"
    models_dir: str | None = None
    mount: str | None = None
    smb_user: str | None = None


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
        "settings": config.public(settings),
        "disk": catalog.disk(),
        "library_dir": str(config.LIBRARY_DIR),
        "base_url": base,
        "scanned_at": catalog.scanned_at(),
        "now": time.time(),
    }


# ----- pages and live state -----
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
    """Return the stored SMB password for display in the UI. The UI is unauthenticated: LAN only by design."""
    if key != "smb_password":
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


# ----- uploads from machines -----
@app.get("/api/upload/{publisher}/{repo}", response_class=PlainTextResponse)
async def upload_status(publisher: str, repo: str):
    mid = _model_id(publisher, repo)
    lines = [f"{path}\t{size}" for path, size in sorted(uploads.status(mid).items())]
    return "\n".join(lines) + ("\n" if lines else "")


@app.put("/api/upload/{publisher}/{repo}/{path:path}")
async def upload_file(publisher: str, repo: str, path: str, request: Request):
    mid = _model_id(publisher, repo)
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
    try:
        model = await asyncio.to_thread(uploads.commit, mid, [f.model_dump() for f in body.files], body.machine)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return model


@app.delete("/api/upload/{publisher}/{repo}")
async def upload_abort(publisher: str, repo: str):
    mid = _model_id(publisher, repo)
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
