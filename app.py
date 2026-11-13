"""
Web app for the receipt tools.

  /          upload a PO folder, rename its receipts, download the result
  /single    read one receipt (amount, date, type)

Run:
    python receipt_extractor/app.py        # then open http://127.0.0.1:8001

Files are sent as raw request bodies (no multipart dependency). Uploaded folders
live in a temporary job folder and are deleted on "Clear" or after 24 hours.
"""

import logging
import os
import shutil
import sys
import tempfile
import threading
import time
import uuid
from datetime import date
from pathlib import Path, PurePosixPath
from urllib.parse import unquote

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

try:
    from dotenv import load_dotenv
    load_dotenv(HERE / ".env")
except ImportError:
    pass

from fastapi import FastAPI, HTTPException, Request  # noqa: E402
from fastapi.responses import FileResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

from extractor import explain_error, extract_receipt  # noqa: E402
from rename_receipts import RECEIPT_SUFFIXES, REVIEW_DIR, run, safe_name, unique_path, write_report  # noqa: E402

logging.basicConfig(level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("receipts")

MAX_RECEIPT_MB = 100
MAX_UPLOAD = MAX_RECEIPT_MB * 1024 * 1024
MAX_WORKBOOK = 200 * 1024 * 1024
MAX_FILES = 3000
JOB_TTL = 24 * 3600
JOBS_ROOT = Path(tempfile.gettempdir()) / "receipt_jobs"
WORKERS = int(os.getenv("RECEIPT_WORKERS", "4"))

app = FastAPI(title="Receipt tools")
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")


@app.middleware("http")
async def revalidate_pages(request: Request, call_next):
    """Make the browser re-check the pages and their CSS/JS on every load, so an update to
    the app is never hidden behind a stale cached copy."""
    response = await call_next(request)
    if request.url.path.startswith("/static/") or request.url.path in ("/", "/single"):
        response.headers["Cache-Control"] = "no-cache"
    return response

_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()


def _default_workbooks() -> dict[str, Path | None]:
    """Optional fallback workbooks from MASTER_FILE / ANNEXURE_FILE in .env. A workbook
    uploaded with the job always takes priority; a missing or unset path is just None."""
    out: dict[str, Path | None] = {}
    for kind, var in (("master", "MASTER_FILE"), ("annexure", "ANNEXURE_FILE")):
        raw = os.getenv(var, "").strip().strip('"')
        out[kind] = Path(raw) if raw and Path(raw).is_file() else None
    return out


def _job(job_id: str) -> dict:
    job = _jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "Unknown or expired job")
    return job


def _purge_old_jobs() -> None:
    cutoff = time.time() - JOB_TTL
    if JOBS_ROOT.exists():
        for d in JOBS_ROOT.iterdir():
            if d.is_dir() and d.stat().st_mtime < cutoff:
                shutil.rmtree(d, ignore_errors=True)
                _jobs.pop(d.name, None)


def _safe_relative(raw: str) -> PurePosixPath:
    """A relative path from the browser, refusing anything that could escape the job folder."""
    rel = PurePosixPath(unquote(raw or "").replace("\\", "/"))
    if rel.is_absolute() or len(rel.parts) < 2 or any(p in ("..", "") or ":" in p for p in rel.parts):
        raise HTTPException(400, "Invalid file path")
    return rel


def _jsonable(value):
    return value.isoformat() if isinstance(value, date) else value


@app.get("/")
def index():
    return FileResponse(HERE / "static" / "index.html")


@app.get("/single")
def single():
    return FileResponse(HERE / "static" / "single.html")


@app.get("/api/config")
def config():
    defaults = _default_workbooks()
    out = {kind: path.name if path else None for kind, path in defaults.items()}
    out["max_receipt_mb"] = MAX_RECEIPT_MB
    return out


@app.post("/api/jobs")
async def create_job(request: Request):
    folder = str((await request.json()).get("folder") or "").strip()
    if not folder or "/" in folder or "\\" in folder or folder in (".", ".."):
        raise HTTPException(400, "A folder name is required")
    _purge_old_jobs()
    job_id = uuid.uuid4().hex
    job_dir = JOBS_ROOT / job_id
    (job_dir / "input" / folder).mkdir(parents=True)
    with _jobs_lock:
        _jobs[job_id] = {"dir": job_dir, "folder": folder, "files": 0, "state": "uploading",
                         "done": 0, "total": 0, "error": None, "result": None, "rejected": []}
    return {"id": job_id}


@app.put("/api/jobs/{job_id}/files")
async def upload_file(job_id: str, request: Request):
    job = _job(job_id)
    if job["state"] != "uploading":
        raise HTTPException(409, "This job already started")
    rel = _safe_relative(request.headers.get("x-path", ""))
    if rel.parts[0] != job["folder"]:
        raise HTTPException(400, "File is outside the selected folder")
    if PurePosixPath(rel.name).suffix.lower() not in RECEIPT_SUFFIXES:
        return {"skipped": True}
    if job["files"] >= MAX_FILES:
        raise HTTPException(413, f"Too many files (limit {MAX_FILES})")
    body = await request.body()
    # The browser announces the real size and sends no body for a file over the limit, so one
    # oversized receipt never blocks the rest: it is listed as needing review instead.
    announced = request.headers.get("x-size", "")
    size = int(announced) if announced.isdigit() else len(body)
    if size > MAX_UPLOAD or len(body) > MAX_UPLOAD:
        job["rejected"].append({"ER": rel.parts[1].upper() if len(rel.parts) > 2 else "", "File": rel.name,
                                "Status": "Review", "New name": "",
                                "Note": f"Not processed: the file is larger than {MAX_RECEIPT_MB} MB ({size / 1048576:,.0f} MB)"})
        return {"skipped": "too large"}
    target = job["dir"] / "input" / Path(*rel.parts)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(body)
    job["files"] += 1
    return {"saved": True}


@app.put("/api/jobs/{job_id}/workbooks/{kind}")
async def upload_workbook(job_id: str, kind: str, request: Request):
    """Receive the master dump ("master") or the travel annexure ("annexure") for this job."""
    job = _job(job_id)
    if kind not in ("master", "annexure"):
        raise HTTPException(404, "Unknown workbook")
    if job["state"] != "uploading":
        raise HTTPException(409, "This job already started")
    name = unquote(request.headers.get("x-filename", ""))
    suffix = PurePosixPath(name.replace("\\", "/")).suffix.lower()
    if suffix not in (".xlsx", ".xlsm"):
        raise HTTPException(400, f"{name or 'The file'} is not an Excel workbook (.xlsx or .xlsm)")
    body = await request.body()
    if len(body) > MAX_WORKBOOK:
        raise HTTPException(413, f"{name} is larger than {MAX_WORKBOOK // (1024 * 1024)} MB")
    target = job["dir"] / f"{kind}{suffix}"
    target.write_bytes(body)
    job[kind] = target
    return {"saved": True}


def _run_job(job: dict) -> None:
    master, annexure = job["master"], job["annexure"]

    def progress(done: int, total: int) -> None:
        job["done"], job["total"] = done, total

    try:
        result = run(job["dir"] / "input" / job["folder"], master, annexure, job["dir"] / "output",
                     workers=WORKERS, cache_dir=job["dir"] / "cache", progress=progress, log=log.info,
                     extra_rows=job["rejected"])
        rows = [{k: _jsonable(v) for k, v in r.items()} for r in result["rows"]]
        missing = [{k: _jsonable(v) for k, v in a.items()} for a in result["missing"]]
        job["result"] = {"po": result["po"], "sheet": result["sheet"], "rows": rows, "missing": missing,
                         "out_dir": result["out_dir"], "report": result["report"]}
        job["state"] = "done"
    except ValueError as exc:  # expected problems: no ER folders, PO not in the Overview sheet, bad headers
        job["error"], job["state"] = str(exc), "error"
    except Exception as exc:
        log.exception("Job failed")
        job["error"], job["state"] = f"Something went wrong: {exc}", "error"


@app.post("/api/jobs/{job_id}/start")
def start_job(job_id: str):
    job = _job(job_id)
    if job["state"] != "uploading":
        raise HTTPException(409, "This job already started")
    if job["files"] == 0:
        raise HTTPException(400, "The folder has no PDF or image receipts")
    defaults = _default_workbooks()
    for kind, label in (("master", "master dump"), ("annexure", "travel annexure")):
        job[kind] = job.get(kind) or defaults[kind]
        if not job[kind]:
            raise HTTPException(400, f"Choose the {label} Excel file")
    job["state"] = "running"
    threading.Thread(target=_run_job, args=(job,), daemon=True).start()
    return {"started": True}


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str):
    job = _job(job_id)
    out = {"state": job["state"], "done": job["done"], "total": job["total"], "error": job["error"],
           "files": job["files"]}
    if job["state"] == "done":
        r = job["result"]
        out.update({"po": r["po"], "sheet": r["sheet"], "rows": r["rows"], "missing": r["missing"]})
    return out


@app.post("/api/jobs/{job_id}/rename")
async def rename_output(job_id: str, request: Request):
    """The user approved a suggestion or typed a name for a receipt that needed review: move it out
    of NeedsReview in the downloadable copy under that name. (The page renames the file in the
    user's own folder separately, when the browser allows it.)"""
    job = _job(job_id)
    if job["state"] != "done":
        raise HTTPException(409, "The job is not finished")
    body = await request.json()
    er, file, kind = str(body.get("er") or ""), str(body.get("file") or ""), body.get("kind")
    row = next((r for r in job["result"]["rows"] if r["ER"] == er and r["File"] == file), None)
    if row is None:
        raise HTTPException(404, "Unknown receipt")
    if row["Status"].startswith("Renamed"):
        raise HTTPException(409, "This receipt is already renamed")
    suffix = PurePosixPath(file).suffix.lower()
    name = safe_name(str(body.get("name") or ""))
    if name.lower().endswith(suffix):
        name = name[: -len(suffix)].rstrip(" .")
    if not name or len(name) > 150:
        raise HTTPException(400, "Enter a file name (up to 150 characters)")
    out_dir: Path = job["result"]["out_dir"]
    with _jobs_lock:
        source = out_dir / er / REVIEW_DIR / file
        if source.is_file():   # a file that was too large to upload has no copy to move
            target = unique_path(out_dir / er, name, suffix)
            shutil.move(str(source), str(target))
            final = target.name
        else:
            final = name + suffix
        row["Status"] = "Renamed (approved)" if kind == "approve" else "Renamed (by hand)"
        row["New name"] = final
        row["Note"] = "Suggestion approved" if kind == "approve" else "Named by hand"
        try:
            write_report(job["result"]["report"], job["result"]["rows"], job["result"]["missing"])
        except Exception:
            log.exception("Could not refresh report.xlsx")
    return {"name": final}


@app.get("/api/jobs/{job_id}/download")
def download(job_id: str):
    job = _job(job_id)
    if job["state"] != "done":
        raise HTTPException(409, "The job is not finished")
    out_dir: Path = job["result"]["out_dir"]
    archive = shutil.make_archive(str(job["dir"] / "result"), "zip", root_dir=out_dir.parent, base_dir=out_dir.name)
    return FileResponse(archive, media_type="application/zip", filename=f"{out_dir.name}_renamed.zip")


@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: str):
    job = _jobs.pop(job_id, None)
    if job:
        shutil.rmtree(job["dir"], ignore_errors=True)
    return {"deleted": True}


@app.post("/extract")
async def extract(request: Request):
    body = await request.body()
    if not body:
        raise HTTPException(400, "No file received")
    if len(body) > MAX_UPLOAD:
        raise HTTPException(413, f"File is larger than {MAX_RECEIPT_MB} MB")
    try:
        result = extract_receipt(body)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except RuntimeError as exc:
        raise HTTPException(500, str(exc))
    except Exception as exc:  # auth / network / API failures
        logging.exception("Extraction failed")
        raise HTTPException(502, explain_error(exc))
    result["filename"] = request.headers.get("x-filename", "")
    return result


if __name__ == "__main__":
    import uvicorn

    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8001
    uvicorn.run(app, host="127.0.0.1", port=port)
