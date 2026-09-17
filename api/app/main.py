from fastapi import FastAPI, HTTPException

from app.db import ping_database
from app.routers import services, tickets, users

app = FastAPI(title="AI Service Desk Agent")

app.include_router(tickets.router)
app.include_router(users.router)
app.include_router(services.router)


@app.get("/health")
def health():
    try:
        ping_database()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"database unreachable: {exc}") from exc
    return {"status": "ok", "database": "connected"}
