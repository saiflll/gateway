import asyncio
import os
import secrets
import sqlite3
import time
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import quote

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from classifier import is_paid_model_failure

CONTROLLER_TOKEN = os.getenv("CONTROLLER_TOKEN", "change-me")
DEFAULT_TTL = int(os.getenv("DISABLE_TTL_SECONDS", "21600"))
ROUTER_URL = os.getenv("ROUTER_URL", "http://9router:20128").rstrip("/")
HEADROOM_URL = os.getenv("HEADROOM_URL", "http://headroom:8787").rstrip("/")
ROUTER_API_KEY = os.getenv("ROUTER_API_KEY", "")
ROUTER_ADMIN_PASSWORD = os.getenv("ROUTER_ADMIN_PASSWORD", "")
REGISTRY_PATH = os.getenv("REGISTRY_PATH", "/data/disabled-models.db")


class FailureReport(BaseModel):
    provider_alias: str = Field(min_length=1)
    model_id: str = Field(min_length=1)
    status: int
    error: Any
    ttl_seconds: int | None = Field(default=None, gt=0, le=2_592_000)


class RegistryEntry(BaseModel):
    provider_alias: str = Field(min_length=1)
    model_id: str = Field(min_length=1)
    expires_at: float


class RegistryImport(BaseModel):
    entries: list[RegistryEntry]
    replace: bool = False


def connect() -> sqlite3.Connection:
    parent = os.path.dirname(REGISTRY_PATH)
    if parent:
        os.makedirs(parent, exist_ok=True)
    db = sqlite3.connect(REGISTRY_PATH)
    db.execute(
        "CREATE TABLE IF NOT EXISTS disabled_models ("
        "provider_alias TEXT NOT NULL, model_id TEXT NOT NULL, expires_at REAL NOT NULL, "
        "PRIMARY KEY(provider_alias, model_id))"
    )
    return db


def require_token(x_controller_token: str | None = Header(default=None)) -> None:
    if not x_controller_token or not secrets.compare_digest(x_controller_token, CONTROLLER_TOKEN):
        raise HTTPException(status_code=401, detail="invalid controller token")


def router_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {ROUTER_API_KEY}"} if ROUTER_API_KEY else {}


async def router_management_request(
    client: httpx.AsyncClient, method: str, path: str, **kwargs: Any
) -> httpx.Response:
    """Call a protected 9Router API, establishing a dashboard session when needed."""
    response = await client.request(
        method, f"{ROUTER_URL}{path}", headers=router_headers(), **kwargs
    )
    if response.status_code != 401 or not ROUTER_ADMIN_PASSWORD:
        return response

    login = await client.post(
        f"{ROUTER_URL}/api/auth/login", json={"password": ROUTER_ADMIN_PASSWORD}
    )
    login.raise_for_status()
    return await client.request(method, f"{ROUTER_URL}{path}", **kwargs)


async def sweep_expired(client: httpx.AsyncClient) -> dict[str, int]:
    now = time.time()
    with connect() as db:
        rows = db.execute(
            "SELECT provider_alias, model_id FROM disabled_models WHERE expires_at <= ?", (now,)
        ).fetchall()
    enabled = 0
    failed = 0
    for provider_alias, model_id in rows:
        path = (
            f"/api/models/disabled?providerAlias={quote(provider_alias)}"
            f"&id={quote(model_id)}"
        )
        try:
            response = await router_management_request(client, "DELETE", path)
            response.raise_for_status()
        except httpx.HTTPError:
            failed += 1
            continue
        with connect() as db:
            db.execute(
                "DELETE FROM disabled_models WHERE provider_alias = ? AND model_id = ?",
                (provider_alias, model_id),
            )
        enabled += 1
    return {"enabled": enabled, "failed": failed}


async def periodic_sweep() -> None:
    async with httpx.AsyncClient(timeout=15) as client:
        while True:
            await asyncio.sleep(60)
            await sweep_expired(client)


@asynccontextmanager
async def lifespan(_: FastAPI):
    with connect():
        pass
    task = asyncio.create_task(periodic_sweep())
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


app = FastAPI(title="nyxgtw model controller", version="1.1.0", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/health/dependencies", dependencies=[Depends(require_token)])
async def dependency_health() -> dict[str, Any]:
    """Prove both internal HTTP routes work; this does not mutate either service."""
    targets = {
        "9router": f"{ROUTER_URL}/v1/models",
        "headroom": f"{HEADROOM_URL}/readyz",
    }
    results: dict[str, Any] = {}
    async with httpx.AsyncClient(timeout=5) as client:
        for name, url in targets.items():
            try:
                response = await client.get(url, headers=router_headers() if name == "9router" else {})
                results[name] = {"reachable": response.is_success, "status": response.status_code}
            except httpx.HTTPError as exc:
                results[name] = {"reachable": False, "error": type(exc).__name__}
    if not all(item["reachable"] for item in results.values()):
        raise HTTPException(status_code=503, detail=results)
    return {"status": "ok", "dependencies": results}


@app.get("/registry/export", dependencies=[Depends(require_token)])
async def export_registry() -> dict[str, Any]:
    with connect() as db:
        rows = db.execute(
            "SELECT provider_alias, model_id, expires_at FROM disabled_models ORDER BY provider_alias, model_id"
        ).fetchall()
    return {
        "format": "nyxgtw-disabled-models-v1",
        "exported_at": time.time(),
        "entries": [
            {"provider_alias": provider, "model_id": model, "expires_at": expires}
            for provider, model, expires in rows
        ],
    }


@app.post("/registry/import", dependencies=[Depends(require_token)])
async def import_registry(payload: RegistryImport) -> dict[str, int]:
    """Import controller TTL state; a sweep reconciles already-expired entries with 9Router."""
    with connect() as db:
        if payload.replace:
            db.execute("DELETE FROM disabled_models")
        db.executemany(
            "INSERT INTO disabled_models(provider_alias, model_id, expires_at) VALUES (?, ?, ?) "
            "ON CONFLICT(provider_alias, model_id) DO UPDATE SET expires_at=excluded.expires_at",
            [(entry.provider_alias, entry.model_id, entry.expires_at) for entry in payload.entries],
        )
    return {"imported": len(payload.entries)}


@app.post("/report-failure", dependencies=[Depends(require_token)])
async def report_failure(report: FailureReport) -> dict[str, Any]:
    if not is_paid_model_failure(report.status, report.error):
        return {"classified": False, "scope": None, "disabled": False}

    ttl = report.ttl_seconds or DEFAULT_TTL
    async with httpx.AsyncClient(timeout=15) as client:
        response = await router_management_request(
            client,
            "POST",
            "/api/models/disabled",
            json={"providerAlias": report.provider_alias, "ids": [report.model_id]},
        )
    if response.is_error:
        raise HTTPException(status_code=502, detail="9Router rejected the disable request")

    expires_at = time.time() + ttl
    with connect() as db:
        db.execute(
            "INSERT INTO disabled_models(provider_alias, model_id, expires_at) VALUES (?, ?, ?) "
            "ON CONFLICT(provider_alias, model_id) DO UPDATE SET expires_at=excluded.expires_at",
            (report.provider_alias, report.model_id, expires_at),
        )
    return {
        "classified": True,
        "scope": "MODEL_ERROR",
        "disabled": True,
        "expires_at": expires_at,
    }


@app.post("/sweep", dependencies=[Depends(require_token)])
async def sweep() -> dict[str, int]:
    async with httpx.AsyncClient(timeout=15) as client:
        return await sweep_expired(client)
