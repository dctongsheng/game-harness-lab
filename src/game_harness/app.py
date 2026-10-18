"""Loopback-only dashboard/API."""
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

from .providers import ROOT, Settings
from .service import Lab
from .games import game_catalog
from .exporting import read_bundle


def create_app(data_dir: Path | None = None, settings: Settings | None = None, *,
               lab: Lab | None = None, index_file: Path | None = None):
    lab = lab or Lab(settings or Settings.load(), data_dir or ROOT / "data")
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
        return FileResponse(index_file or Path(__file__).parent / "static" / "index.html")

    @app.get("/api/state")
    def state():
        return lab.state()

    @app.get("/api/games")
    def games():
        return {"games": game_catalog()}

    @app.get('/spectate')
    def spectate():
        return FileResponse(Path(__file__).parent / 'static' / 'spectate.html')

    @app.get('/showdown/{filename}')
    def showdown_page(filename: str):
        if filename == 'manifest.json':
            from .showdown_assets import manifest
            try:
                return JSONResponse(manifest())
            except (OSError, ValueError, KeyError):
                raise HTTPException(404, '原版观战组件尚未安装') from None
        filename = {'frame': 'showdown-frame.html', 'frame.js': 'showdown-frame.js'}.get(filename, filename)
        allowed = {'showdown-frame.html': 'text/html', 'showdown-frame.js': 'text/javascript',
                   'spectate.js': 'text/javascript'}
        if filename not in allowed:
            raise HTTPException(404, '观战文件不存在')
        response = FileResponse(Path(__file__).parent / 'static' / filename, media_type=allowed[filename])
        if filename == 'showdown-frame.html':
            response.headers['Content-Security-Policy'] = (
                "default-src 'none'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
                "img-src 'self' https://play.pokemonshowdown.com data:; connect-src 'none'; "
                "font-src 'self' https://play.pokemonshowdown.com; media-src 'none'; "
                "frame-ancestors 'self'; base-uri 'none'; form-action 'none'")
        return response

    @app.get('/api/showdown-viewer')
    def showdown_status():
        from .showdown_assets import manifest
        try:
            data = manifest()
            return {'available': True, 'client': data['client']}
        except (OSError, ValueError, KeyError):
            return {'available': False, 'message': '原版观战组件尚未安装。运行 .venv/bin/python scripts/setup_showdown_viewer.py'}

    @app.get('/showdown/assets/{name:path}')
    def showdown_asset(name: str):
        from .showdown_assets import asset
        try:
            path, mime = asset(name)
            return FileResponse(path, media_type=mime)
        except (OSError, ValueError, KeyError):
            raise HTTPException(404, '原版观战资源不可用或校验失败') from None

    @app.get('/api/spectator/episodes')
    def spectator_episodes():
        return {'episodes': lab.spectator_episodes()}

    @app.get('/api/spectator/{episode_id}')
    def spectator_episode(episode_id: str, after: int = 0):
        try:
            return lab.spectator_feed(episode_id, after)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        except FileNotFoundError:
            raise HTTPException(404, '回合不存在') from None

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
        return lab.public(result)

    return app
