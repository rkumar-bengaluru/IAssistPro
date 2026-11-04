"""Session lifecycle: one session at a time (A2/§5.7), one-time connect tokens, temp results and their expiry."""

import asyncio
import hashlib
import hmac
import secrets
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .assets import ModelStatus, health
from .config import Settings
from .contract import AudioInfo, Brief, EndReason, Latency, Result
from .logs import log_event
from .store import SessionMeta, SessionStore, StateRecord

SWEEP_EVERY_S = 600
HOUSEKEEPING_EVERY_S = 10


class ServiceError(Exception):
    http_status = 500
    code = "error"

    def __init__(self, message: str = "", **extra):
        super().__init__(message or self.code)
        self.message = message or self.code.replace("_", " ")
        self.extra = extra


class Busy(ServiceError):
    http_status, code = 409, "busy"


class NotReady(ServiceError):
    http_status, code = 503, "not_ready"


class UnknownSession(ServiceError):
    http_status, code = 404, "unknown_session"


class TokenInvalid(ServiceError):
    http_status, code = 401, "token_invalid"


class TokenExpired(ServiceError):
    http_status, code = 410, "token_expired"


class WrongState(ServiceError):
    http_status, code = 409, "wrong_state"


class NotFinished(ServiceError):
    http_status, code = 409, "not_finished"


class AudioPending(ServiceError):
    http_status, code = 409, "audio_pending"


class NoAudio(ServiceError):
    http_status, code = 404, "no_audio"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class LiveSession:
    """A waiting or live session in memory; implements pipeline.SessionHooks."""

    def __init__(self, manager: "SessionManager", brief: Brief, session_id: str, token: str, ttl_s: int):
        self.manager = manager
        self.brief = brief
        self.session_id = session_id
        self.store = manager.store
        self.end_requested = asyncio.Event()
        self.latency = Latency()
        self.token_hash = _hash(token)
        self.created_mono = manager.clock()
        self.ttl_s = ttl_s
        self.expires_at = _now() + timedelta(seconds=ttl_s)
        self.connected_mono: float | None = None
        self.ended_mono: float | None = None
        self.started_at: datetime | None = None
        self.pc_id: str | None = None
        self.offered = False
        self.state = "waiting"
        self.question_id: str | None = None
        self.turn_kind: str | None = None
        self.question_number = 0
        self.questions_total = len(brief.planned_questions)
        self.end_reason: EndReason | None = None
        self.error: str | None = None
        self.audio_state = "none"
        self.task: asyncio.Task | None = None

    # ---- token
    def token_ok(self, token: str | None) -> bool:
        return bool(token) and hmac.compare_digest(_hash(token), self.token_hash)

    def expired(self) -> bool:
        return self.manager.clock() - self.created_mono > self.ttl_s

    # ---- SessionHooks
    def _write(self, **upd) -> None:
        rec = self.store.read_state(self.session_id)
        self.store.write_state(self.session_id, rec.model_copy(update=upd))

    def on_live(self) -> None:
        self.state = "live"
        self.connected_mono = self.manager.clock()
        self.started_at = _now()
        self.turn_kind = "greeting"
        self.audio_state = "pending"
        self._write(state="live", started_at=self.started_at, audio=AudioInfo(state="pending"))
        log_event("session_live", session=self.session_id)

    def on_progress(self, question_id: str | None, kind: str, number: int) -> None:
        self.question_id, self.turn_kind, self.question_number = question_id, kind, number

    def on_end(self, reason: EndReason, error: str | None) -> None:
        if self.state in ("ended", "failed"):
            return
        self.state = "failed" if reason == "error" else "ended"
        self.end_reason, self.error = reason, error
        self.ended_mono = self.manager.clock()
        if self.audio_state != "pending":
            self.audio_state = "none"
        self._write(state=self.state, end_reason=reason, error=error, ended_at=_now(), latency=self.latency,
                    audio=AudioInfo(state=self.audio_state))
        log_event("session_ended", session=self.session_id, reason=reason, error=error)
        self.manager._release(self)

    def on_audio(self, info: AudioInfo) -> None:
        self.audio_state = info.state
        self._write(audio=info)

    def status(self) -> dict:
        if self.connected_mono is None:
            elapsed = 0.0
        else:
            elapsed = (self.ended_mono or self.manager.clock()) - self.connected_mono
        live = self.state == "live"
        return {
            "session_id": self.session_id, "state": self.state, "elapsed_s": round(elapsed, 1),
            "current_question_id": self.question_id if live else None, "question_number": self.question_number,
            "questions_total": self.questions_total, "turn_kind": self.turn_kind if live else None,
            "end_reason": self.end_reason, "error": self.error, "audio_state": self.audio_state,
        }


Runner = Callable[[LiveSession, object, Settings], Awaitable[None]]


def _default_runner() -> Runner:
    from .pipeline import run_interview

    return run_interview


def _default_transport_factory(connection, s: Settings):
    from pipecat.transports.base_transport import TransportParams
    from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport

    return SmallWebRTCTransport(webrtc_connection=connection,
                                params=TransportParams(audio_in_enabled=True, audio_out_enabled=True))


class SessionManager:
    def __init__(self, s: Settings, store: SessionStore, status: ModelStatus, runner: Runner | None = None,
                 clock: Callable[[], float] = time.monotonic, webrtc_handler=None, transport_factory=None):
        self.s = s
        self.store = store
        self.model_status = status
        self.runner = runner or _default_runner()
        self.clock = clock
        self._webrtc = webrtc_handler
        self._transport_factory = transport_factory or _default_transport_factory
        self._lock = asyncio.Lock()
        self._active: LiveSession | None = None
        self._sessions: dict[str, LiveSession] = {}
        self._last_sweep = clock()
        self.audio_recovery: asyncio.Task | None = None

    @property
    def live_session_id(self) -> str | None:
        return self._active.session_id if self._active else None

    def _webrtc_handler(self):
        if self._webrtc is None:
            from pipecat.transports.smallwebrtc.request_handler import SmallWebRTCRequestHandler

            self._webrtc = SmallWebRTCRequestHandler()
        return self._webrtc

    async def startup(self) -> None:
        self.store.recover_on_startup(_now())
        self.store.sweep(_now())
        # A restart during encoding left a finished session's audio pending: settle it in the background (AF4).
        self.audio_recovery = asyncio.create_task(asyncio.to_thread(self.store.recover_pending_audio))

    def _release(self, live: LiveSession) -> None:
        if self._active is live:
            self._active = None

    def _get(self, session_id: str) -> LiveSession | None:
        return self._sessions.get(session_id)

    def _require(self, session_id: str) -> LiveSession | None:
        live = self._get(session_id)
        if live is None and not self.store.exists(session_id):
            raise UnknownSession("unknown session")
        return live

    # ---- create
    async def create(self, brief: Brief) -> dict:
        async with self._lock:
            await self._expire_waiting()
            h = health(self.s, self.model_status, self.live_session_id)
            if h["status"] != "ready":
                raise NotReady("the interviewer is not ready", status=h["status"])
            if self._active is not None:
                raise Busy("a session is already waiting or live", session_id=self._active.session_id)
            sid = uuid.uuid4().hex
            token = secrets.token_urlsafe(32)
            live = LiveSession(self, brief, sid, token, self.s.connect_token_ttl_s)
            self.store.create(sid, SessionMeta.from_brief(brief, _now()))
            self._sessions[sid] = live
            self._active = live
        log_event("session_created", session=sid, questions=live.questions_total)
        connect = {"transport": self.s.transport, "token": token, "expires_at": _iso(live.expires_at)}
        if self.s.transport == "websocket":
            connect.update(ws_path=f"/sessions/{sid}/ws", audio={"in_sample_rate": 16000, "out_sample_rate": 24000})
        else:
            connect["offer_path"] = f"/sessions/{sid}/offer"
        return {"session_id": sid, "connect": connect}

    # ---- connect
    def _expire(self, live: LiveSession) -> None:
        live.state = "failed"
        live.end_reason, live.error = "error", "not_connected"
        live.ended_mono = self.clock()
        live._write(state="failed", end_reason="error", error="not_connected", ended_at=_now(),
                    audio=AudioInfo(state="none"))
        log_event("session_expired", session=live.session_id)
        self._release(live)

    async def _expire_waiting(self) -> None:
        live = self._active
        if live is not None and live.state == "waiting" and not live.offered and live.expired():
            self._expire(live)

    def _check_connect(self, session_id: str, token: str | None) -> LiveSession:
        live = self._require(session_id)
        if live is None:
            raise WrongState("session is not waiting")
        if not live.token_ok(token):
            raise TokenInvalid("invalid session token")
        if live.state == "waiting" and not live.offered and live.expired():
            self._expire(live)
        if live.error == "not_connected":
            raise TokenExpired("the connect token has expired")
        if live.state != "waiting" or live.offered:
            raise WrongState("session already connected or finished")
        return live

    def _start_runner(self, live: LiveSession, transport) -> None:
        async def run():
            try:
                await self.runner(live, transport, self.s)
            except Exception as e:
                log_event("runner_failed", session=live.session_id, error=type(e).__name__)
                live.on_end("error", f"runner_error:{type(e).__name__}")
            finally:
                if live.state in ("waiting", "live"):
                    live.on_end("error", "runner_exited")

        live.task = asyncio.create_task(run())

    async def offer(self, session_id: str, token: str | None, body: dict) -> dict:
        from pipecat.transports.smallwebrtc.request_handler import SmallWebRTCRequest

        if self.s.transport != "smallwebrtc":
            raise WrongState("this service uses the websocket transport")
        async with self._lock:
            live = self._check_connect(session_id, token)
            live.offered = True

        async def on_connection(connection):
            self._start_runner(live, self._transport_factory(connection, self.s))

        try:
            answer = await self._webrtc_handler().handle_web_request(
                request=SmallWebRTCRequest.from_dict(dict(body)), webrtc_connection_callback=on_connection)
        except Exception as e:
            log_event("offer_failed", session=session_id, error=type(e).__name__)
            live.on_end("error", "offer_failed")
            raise WrongState("the offer could not be answered") from e
        live.pc_id = (answer or {}).get("pc_id")
        log_event("offer_answered", session=session_id)
        return answer

    async def patch(self, session_id: str, token: str | None, body: dict) -> dict:
        from pipecat.transports.smallwebrtc.request_handler import IceCandidate, SmallWebRTCPatchRequest

        live = self._require(session_id)
        if live is None:
            raise WrongState("session is not connecting")
        if not live.token_ok(token):
            raise TokenInvalid("invalid session token")
        if not live.offered or live.state in ("ended", "failed") or body.get("pc_id") != live.pc_id:
            raise WrongState("no connection to patch")
        req = SmallWebRTCPatchRequest(pc_id=body["pc_id"],
                                      candidates=[IceCandidate(**c) for c in body.get("candidates", [])])
        await self._webrtc_handler().handle_patch_request(req)
        return {"status": "success"}

    async def websocket(self, session_id: str, token: str | None, ws) -> None:
        if self.s.transport != "websocket":
            await ws.close(code=4409)
            return
        try:
            async with self._lock:
                live = self._check_connect(session_id, token)
                live.offered = True
        except TokenInvalid:
            await ws.close(code=4401)
            return
        except TokenExpired:
            await ws.close(code=4410)
            return
        except (WrongState, UnknownSession):
            await ws.close(code=4409)
            return
        from pipecat.serializers.protobuf import ProtobufFrameSerializer
        from pipecat.transports.websocket.fastapi import FastAPIWebsocketParams, FastAPIWebsocketTransport

        await ws.accept()
        transport = FastAPIWebsocketTransport(websocket=ws, params=FastAPIWebsocketParams(
            audio_in_enabled=True, audio_out_enabled=True, serializer=ProtobufFrameSerializer()))
        self._start_runner(live, transport)
        await live.task

    # ---- status, end, results
    def status(self, session_id: str) -> dict:
        live = self._require(session_id)
        if live is not None:
            return live.status()
        st, meta = self.store.read_state(session_id), self.store.read_meta(session_id)
        elapsed = (st.ended_at - st.started_at).total_seconds() if st.ended_at and st.started_at else 0.0
        return {"session_id": session_id, "state": st.state, "elapsed_s": round(elapsed, 1),
                "current_question_id": None, "question_number": 0, "questions_total": meta.questions_total,
                "turn_kind": None, "end_reason": st.end_reason, "error": st.error, "audio_state": st.audio.state}

    async def end(self, session_id: str) -> dict:
        live = self._require(session_id)
        if live is not None:
            if live.state == "waiting" and not live.offered:
                live.on_end("ended_by_candidate", None)
            elif live.state in ("waiting", "live"):
                live.end_requested.set()
                log_event("end_requested", session=session_id)
        return self.status(session_id)

    def _finished_state(self, session_id: str) -> StateRecord:
        self._require(session_id)
        st = self.store.read_state(session_id)
        if st.state not in ("ended", "failed"):
            raise NotFinished("the session has not finished")
        return st

    def result(self, session_id: str) -> Result:
        self._finished_state(session_id)
        return self.store.result(session_id)

    def audio_path(self, session_id: str) -> Path:
        st = self._finished_state(session_id)
        if st.audio.state == "pending":
            raise AudioPending("the recording is still being encoded")
        path = self.store.dir(session_id) / "audio.ogg"
        if st.audio.state != "ready" or not path.exists():
            raise NoAudio("this session has no recording")
        return path

    async def delete(self, session_id: str) -> None:
        live = self._require(session_id)
        state = live.state if live is not None else self.store.read_state(session_id).state
        if state in ("waiting", "live"):
            raise WrongState("end the session before deleting it")
        self.store.delete(session_id)
        self._sessions.pop(session_id, None)
        log_event("session_deleted", session=session_id)

    async def housekeeping(self, once: bool = False) -> None:
        while True:
            try:
                async with self._lock:
                    await self._expire_waiting()
                if self.clock() - self._last_sweep >= SWEEP_EVERY_S:
                    self._last_sweep = self.clock()
                    for sid in self.store.sweep(_now()):
                        self._sessions.pop(sid, None)
            except Exception as e:
                log_event("housekeeping_failed", error=type(e).__name__)
            if once:
                return
            await asyncio.sleep(HOUSEKEEPING_EVERY_S)
