import json
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .assistant import AssistantBusy, ReuseAssistant
from .config import ROOT, settings
from .embeddings import LocalEmbeddings
from .github import clean_component, parse_repository
from .ollama import OllamaClient
from .search import search
from .store import Store
from .worker import Worker


class RepositoryRequest(BaseModel):
    repository: str = Field(min_length=3, max_length=250)
    component: str = Field(default="", max_length=240)


class AssistantRequest(BaseModel):
    query: str = Field(min_length=2, max_length=1200)
    technology: str = Field(default="", max_length=80)


def create_app(configuration=settings, start_worker=True):
    store = Store(configuration.db_path)
    engine = LocalEmbeddings(configuration, store)
    worker = Worker(configuration, store, engine)
    assistant = ReuseAssistant(store, engine, OllamaClient(configuration))

    @asynccontextmanager
    async def lifespan(app):
        if start_worker:
            worker.start()
        yield
        worker.close()

    app = FastAPI(
        title="Innovation Farming",
        version="0.2.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
    )
    app.state.store, app.state.engine, app.state.worker = store, engine, worker
    app.state.assistant = assistant

    @app.middleware("http")
    async def local_boundary(request: Request, call_next):
        host = request.url.hostname
        if host not in {"127.0.0.1", "localhost", "::1", "testserver"}:
            return JSONResponse(
                {"detail": "This prototype is for local access only."}, status_code=403
            )
        origin = request.headers.get("origin")
        if (
            request.method not in {"GET", "HEAD", "OPTIONS"}
            and origin
            and origin != f"{request.url.scheme}://{request.headers.get('host')}"
        ):
            return JSONResponse(
                {"detail": "Cross-origin writes are not allowed."}, status_code=403
            )
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        )
        return response

    @app.get("/api/status")
    def status():
        repositories = store.repositories()
        with store.connect() as db:
            counts = db.execute(
                "SELECT COUNT(*) AS total, COALESCE(SUM(CASE WHEN vector IS NOT NULL AND model=? THEN 1 ELSE 0 END),0) AS embedded FROM documents",
                (configuration.model_name,),
            ).fetchone()
        technologies = sorted(
            {t["name"] for r in repositories for t in r["manifest"]["technologies"]}
        )
        return {
            "repositories": len(repositories),
            "sections": counts["total"],
            "embedded_sections": counts["embedded"],
            "model": engine.status(),
            "technologies": technologies,
            "mode": "local",
            "jobs": store.jobs()[:8],
        }

    @app.get("/api/repositories")
    def list_repositories():
        return store.repositories()

    @app.post("/api/repositories", status_code=202)
    def add_repository(payload: RepositoryRequest):
        try:
            name, component = (
                parse_repository(payload.repository),
                clean_component(payload.component),
            )
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        return {"job_id": store.enqueue("index", name, component)}

    def require_repository(identifier):
        repository = store.repository(identifier)
        if not repository:
            raise HTTPException(404, "Repository was not found.")
        return repository

    @app.get("/api/repositories/{identifier}")
    def details(identifier: str):
        repository = require_repository(identifier)
        return {
            **repository,
            "sections": [
                {
                    k: v
                    for k, v in d.items()
                    if k not in {"vector", "fingerprint", "model"}
                }
                for d in store.documents(identifier)
            ],
        }

    @app.get("/api/repositories/{identifier}/manifest")
    def download_manifest(identifier: str):
        repository = require_repository(identifier)
        return Response(
            json.dumps(repository["manifest"], ensure_ascii=False, indent=2),
            media_type="application/json",
            headers={
                "Content-Disposition": f'attachment; filename="manifest-{identifier}.json"'
            },
        )

    @app.post("/api/repositories/{identifier}/sync", status_code=202)
    def sync(identifier: str):
        repository = require_repository(identifier)
        return {
            "job_id": store.enqueue(
                "index", repository["full_name"], repository["component"]
            )
        }

    @app.post("/api/repositories/{identifier}/clone", status_code=202)
    def clone(identifier: str):
        require_repository(identifier)
        return {"job_id": store.enqueue("clone", identifier)}

    @app.post("/api/model/prepare", status_code=202)
    def prepare_model():
        return {"job_id": store.enqueue("model", configuration.model_name)}

    @app.get("/api/search")
    def find(
        q: Annotated[str, Query(min_length=2, max_length=1200)],
        technology: str = "",
        limit: Annotated[int, Query(ge=1, le=20)] = 12,
    ):
        return search(store, engine, q, technology[:80], limit)

    @app.get("/api/jobs")
    def jobs():
        return store.jobs()

    @app.get("/api/assistant/status")
    def assistant_status(refresh: bool = False):
        return assistant.status(refresh=refresh)

    @app.post("/api/assistant")
    def analyze_reuse(payload: AssistantRequest):
        query = payload.query.strip()
        if len(query) < 2:
            raise HTTPException(
                422, "Describe a software requirement in at least two characters."
            )
        try:
            return assistant.analyze(query, payload.technology.strip())
        except AssistantBusy as error:
            raise HTTPException(
                429, str(error), headers={"Retry-After": "5"}
            ) from error

    @app.get("/health")
    def health():
        with store.connect() as db:
            db.execute("SELECT 1")
        return {
            "ok": True,
            "database": "sqlite",
            "semantic_ready": engine.model is not None,
        }

    app.mount("/assets", StaticFiles(directory=ROOT / "web"), name="assets")

    @app.get("/")
    def index():
        return FileResponse(ROOT / "web" / "index.html")

    return app


app = create_app()
