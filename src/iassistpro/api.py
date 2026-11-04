"""HTTP API (§5.7 and the plan's "Service interface"). Errors are {"detail": {"code", "message", ...}}."""

import asyncio
import contextlib
import time
from pathlib import Path

from fastapi import FastAPI, Request, WebSocket
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response

from .assets import ModelStatus, health, prepare
from .config import Settings
from .contract import Brief
from .logs import log_event, setup_logging
from .manager import ServiceError, SessionManager
from .store import SessionStore

DEV_CLIENT = Path(__file__).parent / "dev_client" / "client.html"


def _error(http_status: int, code: str, message: str, **extra) -> JSONResponse:
    return JSONResponse(status_code=http_status, content={"detail": {"code": code, "message": message, **extra}})


def _clean_errors(errors) -> list[dict]:
    out = []
    for e in errors:
        out.append({k: (list(v) if k == "loc" else v) for k, v in e.items() if k in ("type", "loc", "msg")})
    return out


def _token(request: Request) -> str | None:
    return request.headers.get("x-session-token") or request.query_params.get("token")


def _warm(s: Settings) -> None:
    """Load the speech models once so the first session's connect excludes model load."""
    from .providers import make_stt, make_tts

    if s.stt_provider == "whisper":
        make_stt(s)
    make_tts(s)
    if s.turn_stop == "smart":
        from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3

        LocalSmartTurnAnalyzerV3()


def create_app(s: Settings | None = None, manager: SessionManager | None = None) -> FastAPI:
    s = s or Settings()
    background = manager is None  # tests pass their own manager and drive housekeeping themselves
    if manager is None:
        manager = SessionManager(s, SessionStore(Path(s.sessions_dir), s.result_ttl_h, keep_raw=s.debug_stereo_wav),
                                 ModelStatus())

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        setup_logging(s.log_level)
        tasks = []
        if background:
            if s.transport == "smallwebrtc":
                from .ice import patch_aioice

                try:
                    patch_aioice(*s.ice_range, s.ice_host)
                except ValueError as e:
                    manager.model_status.state, manager.model_status.error = "error", f"config:{e}"
            await manager.startup()
            if manager.model_status.state != "error":
                tasks.append(asyncio.create_task(prepare(s, manager.model_status, lambda: _warm(s))))
            tasks.append(asyncio.create_task(manager.housekeeping()))
        log_event("service_started", transport=s.transport, stt=s.stt_model, tts=s.tts_provider, llm=s.llm_provider)
        yield
        for t in tasks:
            t.cancel()

    app = FastAPI(title="IAssistPro interviewer", version="0.1.0", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.manager = manager

    @app.middleware("http")
    async def timing(request: Request, call_next):
        t0 = time.monotonic()
        response = await call_next(request)
        route = request.scope.get("route")
        log_event("http", method=request.method, route=getattr(route, "path", "unmatched"),
                  status=response.status_code, ms=int(1000 * (time.monotonic() - t0)))
        return response

    @app.exception_handler(ServiceError)
    async def service_error(_request: Request, e: ServiceError):
        return _error(e.http_status, e.code, e.message, **e.extra)

    @app.exception_handler(RequestValidationError)
    async def invalid(_request: Request, e: RequestValidationError):
        errors = _clean_errors(e.errors())
        first = errors[0] if errors else {"loc": [], "msg": "invalid"}
        loc = ".".join(str(x) for x in first["loc"] if x != "body")
        log_event("invalid_brief", n_errors=len(errors))
        return _error(422, "invalid_brief", f"{loc}: {first['msg']}" if loc else first["msg"], errors=errors)

    @app.get("/health")
    async def get_health():
        return health(s, manager.model_status, manager.live_session_id)

    @app.post("/sessions", status_code=201)
    async def create_session(brief: Brief):
        return await manager.create(brief)

    @app.get("/sessions/{session_id}")
    async def get_status(session_id: str):
        return manager.status(session_id)

    @app.post("/sessions/{session_id}/offer")
    async def post_offer(session_id: str, request: Request):
        return await manager.offer(session_id, _token(request), await request.json())

    @app.patch("/sessions/{session_id}/offer")
    async def patch_offer(session_id: str, request: Request):
        return await manager.patch(session_id, _token(request), await request.json())

    @app.websocket("/sessions/{session_id}/ws")
    async def ws(websocket: WebSocket, session_id: str):
        await manager.websocket(session_id, websocket.query_params.get("token"), websocket)

    @app.post("/sessions/{session_id}/end", status_code=202)
    async def end_session(session_id: str):
        return await manager.end(session_id)

    @app.get("/sessions/{session_id}/result")
    async def get_result(session_id: str):
        return JSONResponse(manager.result(session_id).model_dump(mode="json"))

    @app.get("/sessions/{session_id}/audio")
    async def get_audio(session_id: str):
        return FileResponse(manager.audio_path(session_id), media_type="audio/ogg")

    @app.delete("/sessions/{session_id}", status_code=204)
    async def delete_session(session_id: str):
        await manager.delete(session_id)
        return Response(status_code=204)

    @app.get("/dev/client")
    async def dev_client():
        if not s.dev_client or not DEV_CLIENT.exists():
            return _error(404, "not_found", "the dev client is off (DEV_CLIENT=1 turns it on)")
        return HTMLResponse(DEV_CLIENT.read_text(encoding="utf-8"))

    return app
