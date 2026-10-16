"""Loopback-only dashboard/API."""
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

from .providers import ROOT, Settings
from .service import Lab
from .games import game_catalog
from .exporting import read_bundle


def create_app(data_dir: Path | None = None, settings: Settings | None = None):
    lab = Lab(settings or Settings.load(), data_dir or ROOT / "data")
    app = FastAPI(title="Game Harness Lab", docs_url=None, redoc_url=None)
    app.state.lab = lab

    @app.middleware("http")
    async def local_origin(request: Request, call_next):
        hostname = request.url.hostname
        if hostname not in {"127.0.0.1", "localhost", "testserver"}:
            return JSONResponse({"detail": "仅允许本地访问"}, status_code=403)
        origin = request.headers.get("origin")
        if request.method != "GET" and origin:
            parsed = urlparse(origin)
            if parsed.netloc != request.headers.get("host") or parsed.scheme not in {"http", "https"}:
                return JSONResponse({"detail": "拒绝跨站控制请求"}, status_code=403)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.get("/")
    def index():
        return FileResponse(Path(__file__).parent / "static" / "index.html")

    @app.get("/api/state")
    def state():
        return lab.state()

    @app.get("/api/games")
    def games():
        return {"games": game_catalog()}

    @app.get("/api/harnesses/{game_id}/{version_id}")
    def harness(game_id: str, version_id: str):
        try:
            return read_bundle(lab.export_dir, game_id, version_id)
        except (ValueError, FileNotFoundError):
            raise HTTPException(404, "导出包不存在或校验失败") from None

    @app.post("/api/exports/reconcile")
    def reconcile():
        if lab.busy:
            raise HTTPException(409, "请先等待实验结束")
        lab.reconcile_exports()
        return {"exports": lab.store.list("export", 100)}

    @app.get("/api/health")
    def health():
        return lab.check_health()

    @app.post("/api/run")
    def run(options: dict):
        try:
            return lab.start(options)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None

    @app.post("/api/pause")
    def pause():
        try:
            lab.pause()
            return {"ok": True}
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None

    @app.post("/api/resume")
    def resume():
        lab.resume()
        return {"ok": True}

    @app.post("/api/stop")
    def stop():
        lab.stop()
        return {"ok": True}

    @app.post("/api/rollback")
    def rollback(body: dict):
        try:
            return lab.rollback(body.get("version_id"))
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None

    @app.get("/api/episodes/{episode_id}")
    def episode(episode_id: str):
        result = lab.store.load_episode(episode_id)
        if result is None:
            raise HTTPException(404, "回合不存在")
        return result

    return app
