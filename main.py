import os
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel
import engine
import malware

PREFIX = os.environ.get("URL_PREFIX", "/upgrader")
app = FastAPI()


@app.middleware("http")
async def strip_prefix(request, call_next):
    p = request.scope["path"]
    if PREFIX and (p == PREFIX or p.startswith(PREFIX + "/")):
        request.scope["path"] = p[len(PREFIX):] or "/"
    return await call_next(request)


class NewJob(BaseModel):
    project: str
    base_url: str = ""
    extra_paths: str = ""
    dry_run: bool = False
    repair: bool = False
    ai: bool = True
    skip_missing: bool = True
    patch: bool = False


class NewScan(BaseModel):
    project: str
    deep: bool = False
    uploads: bool = True
    exclude: str = ""


class QuarantineBody(BaseModel):
    files: list[str]


@app.get("/")
def index():
    return FileResponse(engine.BASE / "static" / "index.html")


@app.get("/api/state")
def state():
    return {"root": str(engine.ROOT), "php": engine.PHP, "composer": " ".join(engine.COMPOSER), "ai": bool(engine.api_key())}


@app.get("/api/projects")
def projects():
    return engine.projects()


@app.get("/api/jobs")
def jobs():
    return [j.brief() for j in sorted(engine.all_jobs(), key=lambda j: j.id, reverse=True)]


@app.post("/api/jobs")
def create(body: NewJob):
    try:
        return engine.start(body.project, body.model_dump())
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/jobs/{jid}")
def job(jid: str):
    j = engine.load(jid)
    if not j:
        raise HTTPException(404)
    return j.view()


@app.get("/api/jobs/{jid}/log")
def log(jid: str, offset: int = 0):
    f = engine.JOBDIR / (jid + ".log")
    data = f.read_bytes() if f.exists() and "/" not in jid else b""
    return PlainTextResponse(data[offset:].decode("utf-8", "replace"), headers={"X-Next": str(len(data))})


@app.post("/api/jobs/{jid}/rollback")
def rollback(jid: str):
    try:
        return engine.rollback(jid)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/errors")
def errors():
    f = engine.DATA / "errors.jsonl"
    return PlainTextResponse(f.read_text() if f.exists() else "")


@app.get("/api/malware/scans")
def scans():
    return [j.brief() for j in sorted(malware.all_scans(), key=lambda j: j.id, reverse=True)]


@app.post("/api/malware/scans")
def create_scan(body: NewScan):
    try:
        return malware.start(body.project, body.model_dump())
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/malware/scans/{sid}")
def scan(sid: str):
    j = malware.load(sid)
    if not j:
        raise HTTPException(404)
    return j.view()


@app.get("/api/malware/scans/{sid}/log")
def scan_log(sid: str, offset: int = 0):
    data = malware.log_bytes(sid)
    return PlainTextResponse(data[offset:].decode("utf-8", "replace"), headers={"X-Next": str(len(data))})


@app.post("/api/malware/scans/{sid}/stop")
def scan_stop(sid: str):
    try:
        return malware.stop(sid)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/malware/scans/{sid}/quarantine")
def scan_quarantine(sid: str, body: QuarantineBody):
    try:
        return malware.quarantine(sid, body.files)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/malware/scans/{sid}/restore")
def scan_restore(sid: str):
    try:
        return malware.restore(sid)
    except ValueError as e:
        raise HTTPException(400, str(e))