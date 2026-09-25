from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.routers import approvals, overseer, overview, services, team, tickets, users
from shared.db import ping_database

app = FastAPI(title="AI Service Desk Agent")

STATIC_DIR = Path(__file__).parent / "static"

# The overseer's script and styles live in their own files rather than inline,
# because that page is large enough that one file would be hard to read. Still
# no build step: these are served exactly as written.
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

app.include_router(tickets.router)
app.include_router(approvals.router)
app.include_router(users.router)
app.include_router(services.router)
app.include_router(overview.router)
app.include_router(overseer.router)
app.include_router(team.router)


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    """The demo UI. Plain HTML and fetch calls against the same endpoints
    below - no build step, no separate frontend service to keep running."""
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/health")
def health():
    try:
        ping_database()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"database unreachable: {exc}") from exc
    return {"status": "ok", "database": "connected"}
