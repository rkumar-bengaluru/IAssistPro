import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
import jsonschema
import pytest

from iassistpro.assets import ModelStatus
from iassistpro.config import Settings
from iassistpro.contract import AudioInfo, Turn, Usage
from iassistpro.store import SessionStore
from tests.conftest import CANARY

ROOT = Path(__file__).resolve().parents[1]
BRIEF = json.loads((ROOT / "contracts" / "examples" / "brief.example.json").read_text())
RESULT_SCHEMA = json.loads((ROOT / "contracts" / "result.v1.schema.json").read_text())
OFFER = {"sdp": "v=0 fake", "type": "offer"}


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class FakeRunner:
    """Stands in for run_interview: live at once, two turns and usage, then ends on End or a scripted event."""

    def __init__(self):
        self.disconnect = asyncio.Event()

    async def __call__(self, live, transport, s):
        live.on_live()
        live.on_progress("q1", "main", 1)
        live.store.append_turn(live.session_id, Turn(speaker="interviewer", text="Hello Sam. Tell me about yourself.",
                                                     start_ms=100, end_ms=2000, question_id="q1", kind="main"))
        live.store.append_turn(live.session_id, Turn(speaker="candidate", text="I build backend services.",
                                                     start_ms=4000, end_ms=6000, question_id="q1", kind="main"))
        live.store.write_usage(live.session_id, [Usage(provider="anthropic", model="claude-haiku-5-5",
                                                       input_tokens=1000, output_tokens=100)])
        ended = asyncio.create_task(live.end_requested.wait())
        dropped = asyncio.create_task(self.disconnect.wait())
        done, pending = await asyncio.wait({ended, dropped}, return_when=asyncio.FIRST_COMPLETED)
        for p in pending:
            p.cancel()
        live.on_end("ended_by_candidate" if ended in done else "disconnected", None)
        self.audio_gate = asyncio.Event()
        await self.audio_gate.wait()
        (live.store.dir(live.session_id) / "audio.ogg").write_bytes(b"OggS fake")
        live.on_audio(AudioInfo(state="ready", format="ogg_opus", duration_ms=6000, bytes=9))


class FakeHandler:
    """Stands in for SmallWebRTCRequestHandler: a canned answer, and the connection callback is called."""

    def __init__(self):
        self.patches = []

    async def handle_web_request(self, request, webrtc_connection_callback):
        await webrtc_connection_callback(object())
        return {"sdp": "v=0 answer", "type": "answer", "pc_id": "pc-1"}

    async def handle_patch_request(self, request):
        self.patches.append(request.pc_id)


class FakeCheckRunner:
    """Stands in for run_check: live at once, then ends on End or a scripted disconnect. `stuck` ignores End."""

    def __init__(self, stuck: bool = False):
        self.stuck = stuck
        self.disconnect = asyncio.Event()

    async def __call__(self, check, transport, s):
        check.on_live()
        if self.stuck:
            await asyncio.Event().wait()
        ended = asyncio.create_task(check.end_requested.wait())
        dropped = asyncio.create_task(self.disconnect.wait())
        done, pending = await asyncio.wait({ended, dropped}, return_when=asyncio.FIRST_COMPLETED)
        for p in pending:
            p.cancel()
        await asyncio.sleep(0.05)  # the real check says goodbye before it stops
        check.on_end("ended_by_client" if ended in done else "disconnected", None)


def make(tmp_path, *, state="ready", check_runner=None, **env):
    from iassistpro.api import create_app
    from iassistpro.manager import SessionManager

    s = Settings(llm_provider="fake", sessions_dir=str(tmp_path / "sessions"), **env)
    status = ModelStatus()
    status.state = state
    clock = Clock()
    runner = FakeRunner()
    mgr = SessionManager(s, SessionStore(Path(s.sessions_dir), s.result_ttl_h), status, runner=runner, clock=clock,
                         webrtc_handler=FakeHandler(), transport_factory=lambda conn, s: object(),
                         check_runner=check_runner or FakeCheckRunner())
    app = create_app(s, mgr)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")
    return client, mgr, clock, runner, status


@pytest.fixture
async def ctx(tmp_path):
    client, mgr, clock, runner, status = make(tmp_path)
    async with client:
        yield client, mgr, clock, runner, status


@pytest.fixture
async def client(ctx):
    return ctx[0]


async def create(client):
    r = await client.post("/sessions", json=BRIEF)
    assert r.status_code == 201, r.text
    return r.json()


async def offer(client, c, token=None):
    return await client.post(c["connect"]["offer_path"], json=OFFER,
                             headers={"X-Session-Token": token or c["connect"]["token"]})


async def until(pred, timeout=2.0):
    for _ in range(int(timeout / 0.01)):
        if await pred():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not reached")


async def test_health_loading_then_ready(tmp_path):
    client, mgr, clock, runner, status = make(tmp_path, state="loading_models")
    async with client:
        status.total_mb, status.done_mb, status.current = 208, 145, "piper:en_US-lessac-medium"
        h = (await client.get("/health")).json()
        assert (h["status"], h["progress"]["done_mb"], h["contract_version"]) == ("loading_models", 145, 1)
        status.state = "ready"
        h = (await client.get("/health")).json()
        assert (h["status"], h["progress"], h["busy"], h["providers"]["llm"]["name"]) == ("ready", None, False, "fake")


async def test_create_returns_connect_info(client):
    c = await create(client)
    conn = c["connect"]
    assert conn["transport"] == "smallwebrtc" and conn["offer_path"] == f"/sessions/{c['session_id']}/offer"
    assert len(conn["token"]) >= 40
    expires = datetime.fromisoformat(conn["expires_at"].replace("Z", "+00:00"))
    assert abs((expires - datetime.now(timezone.utc)).total_seconds() - 120) < 5
    st = (await client.get(f"/sessions/{c['session_id']}")).json()
    assert (st["state"], st["questions_total"], st["question_number"], st["audio_state"]) == ("waiting", 6, 0, "none")


async def test_websocket_connect_info(tmp_path):
    client, *_ = make(tmp_path, transport="websocket")
    async with client:
        conn = (await create(client))["connect"]
        assert conn["transport"] == "websocket" and conn["ws_path"].endswith("/ws")
        assert conn["audio"] == {"in_sample_rate": 16000, "out_sample_rate": 24000}


async def test_not_ready_is_503(tmp_path):
    client, *_ = make(tmp_path, state="loading_models")
    async with client:
        r = await client.post("/sessions", json=BRIEF)
        assert r.status_code == 503 and r.json()["detail"]["code"] == "not_ready"
        assert r.json()["detail"]["status"] == "loading_models"


async def test_second_session_while_waiting_is_busy(client):
    c = await create(client)
    r = await client.post("/sessions", json=BRIEF)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "busy"
    assert "session_id" not in r.json()["detail"] and c["session_id"] not in r.text   # AF1: ids are never handed out


async def test_offer_token_is_single_use(ctx):
    client, mgr, *_ = ctx
    c = await create(client)
    r = await offer(client, c)
    assert r.status_code == 200 and r.json()["pc_id"] == "pc-1"
    await until(lambda: _state(client, c, "live"))
    r2 = await offer(client, c)
    assert r2.status_code == 409 and r2.json()["detail"]["code"] == "wrong_state"
    p = await client.patch(c["connect"]["offer_path"], json={"pc_id": "pc-1", "candidates": []},
                           headers={"X-Session-Token": c["connect"]["token"]})
    assert p.status_code == 200 and p.json() == {"status": "success"}
    bad = await client.patch(c["connect"]["offer_path"], json={"pc_id": "other", "candidates": []},
                             headers={"X-Session-Token": c["connect"]["token"]})
    assert bad.status_code == 409
    h = await client.get("/health")
    assert h.json()["busy"] is True and "live_session" not in h.json() and c["session_id"] not in h.text


async def _state(client, c, want):
    return (await client.get(f"/sessions/{c['session_id']}")).json()["state"] == want


async def test_token_via_query_parameter(client):
    c = await create(client)
    r = await client.post(c["connect"]["offer_path"] + "?token=" + c["connect"]["token"], json=OFFER)
    assert r.status_code == 200


async def test_wrong_token_is_401_and_does_not_change_state(client):
    c = await create(client)
    r = await offer(client, c, token="x" * 43)
    assert r.status_code == 401 and r.json()["detail"]["code"] == "token_invalid"
    r = await client.post(c["connect"]["offer_path"], json=OFFER)
    assert r.status_code == 401
    assert await _state(client, c, "waiting")


async def test_unused_session_expires_and_frees_lock(ctx):
    client, mgr, clock, *_ = ctx
    c = await create(client)
    clock.t += 121
    await mgr.housekeeping(once=True)
    st = (await client.get(f"/sessions/{c['session_id']}")).json()
    assert (st["state"], st["error"], st["end_reason"]) == ("failed", "not_connected", "error")
    r = await offer(client, c)
    assert r.status_code == 410 and r.json()["detail"]["code"] == "token_expired"
    await create(client)


async def test_expired_token_is_410_even_before_housekeeping(ctx):
    client, mgr, clock, *_ = ctx
    c = await create(client)
    clock.t += 121
    r = await offer(client, c)
    assert r.status_code == 410
    await create(client)   # the lock was freed by the expiry


async def test_end_on_live_then_result_and_audio_flow(ctx):
    client, mgr, clock, runner, _ = ctx
    c = await create(client)
    sid = c["session_id"]
    await offer(client, c)
    await until(lambda: _state(client, c, "live"))
    r = await client.post(f"/sessions/{sid}/end")
    assert r.status_code == 202
    await until(lambda: _state(client, c, "ended"))
    st = (await client.get(f"/sessions/{sid}")).json()
    assert (st["end_reason"], st["audio_state"]) == ("ended_by_candidate", "pending")
    res = await client.get(f"/sessions/{sid}/result")
    assert res.status_code == 200
    jsonschema.validate(res.json(), RESULT_SCHEMA)
    assert len(res.json()["turns"]) == 2 and res.json()["llm_cost_usd"] > 0
    a = await client.get(f"/sessions/{sid}/audio")
    assert a.status_code == 409 and a.json()["detail"]["code"] == "audio_pending"
    runner.audio_gate.set()
    await until(lambda: _audio_ready(client, sid))
    a = await client.get(f"/sessions/{sid}/audio")
    assert a.status_code == 200 and a.headers["content-type"] == "audio/ogg" and a.headers["content-length"] == "9"
    assert (await client.post(f"/sessions/{sid}/end")).status_code == 202   # idempotent
    assert (await client.get(f"/sessions/{sid}/result")).json()["audio"]["state"] == "ready"


async def _audio_ready(client, sid):
    return (await client.get(f"/sessions/{sid}")).json()["audio_state"] == "ready"


async def test_end_while_waiting_ends_without_turns(client):
    c = await create(client)
    r = await client.post(f"/sessions/{c['session_id']}/end")
    assert r.status_code == 202 and r.json()["state"] == "ended"
    res = (await client.get(f"/sessions/{c['session_id']}/result")).json()
    assert (res["end_reason"], res["turns"], res["audio"]["state"]) == ("ended_by_candidate", [], "none")
    a = await client.get(f"/sessions/{c['session_id']}/audio")
    assert a.status_code == 404 and a.json()["detail"]["code"] == "no_audio"
    await create(client)


async def test_result_before_end_is_409(client):
    c = await create(client)
    r = await client.get(f"/sessions/{c['session_id']}/result")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "not_finished"


async def test_disconnect_mid_session_keeps_turns_and_frees_lock(ctx):
    client, mgr, clock, runner, _ = ctx
    c = await create(client)
    await offer(client, c)
    await until(lambda: _state(client, c, "live"))
    runner.disconnect.set()
    await until(lambda: _state(client, c, "ended"))
    res = (await client.get(f"/sessions/{c['session_id']}/result")).json()
    assert res["end_reason"] == "disconnected" and len(res["turns"]) == 2 and res["usage"]
    assert (await client.get("/health")).json()["busy"] is False
    await create(client)


async def test_delete_removes_folder_and_live_delete_is_409(ctx, tmp_path):
    client, mgr, clock, runner, _ = ctx
    c = await create(client)
    sid = c["session_id"]
    r = await client.delete(f"/sessions/{sid}")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "wrong_state"
    await client.post(f"/sessions/{sid}/end")
    assert (await client.delete(f"/sessions/{sid}")).status_code == 204
    assert not (tmp_path / "sessions" / sid).exists()
    assert (await client.get(f"/sessions/{sid}")).status_code == 404


async def test_invalid_brief_422_logs_no_content(client, captured_logs):
    bad = json.loads(json.dumps(BRIEF))
    bad["candidate"]["summary"] = f"{CANARY} write to sam@example.org"
    r = await client.post("/sessions", json=bad)
    d = r.json()["detail"]
    assert r.status_code == 422 and d["code"] == "invalid_brief" and "candidate.summary" in d["message"]
    assert all("input" not in e for e in d["errors"])
    assert CANARY not in r.text
    assert CANARY not in captured_logs() and "invalid_brief" in captured_logs()


async def test_dev_client_only_when_enabled(client):
    assert (await client.get("/dev/client")).status_code == 404


async def test_unknown_session_404(client):
    for method, path in [("get", "/sessions/nope"), ("get", "/sessions/nope/result"), ("get", "/sessions/nope/audio"),
                         ("post", "/sessions/nope/end"), ("delete", "/sessions/nope")]:
        r = await getattr(client, method)(path)
        assert r.status_code == 404 and r.json()["detail"]["code"] == "unknown_session", path


async def test_restart_recovery_and_lock_free(tmp_path):
    client, mgr, *_ = make(tmp_path)
    async with client:
        c = await create(client)
    from iassistpro.manager import SessionManager

    s = mgr.s
    mgr2 = SessionManager(s, SessionStore(Path(s.sessions_dir), 24), mgr.model_status, runner=FakeRunner())
    await mgr2.startup()
    st = mgr2.status(c["session_id"])
    assert (st["state"], st["error"]) == ("failed", "service_restarted") and not mgr2.busy


async def test_startup_reencodes_pending_audio_in_the_background(tmp_path):
    client, mgr, *_ = make(tmp_path)
    async with client:
        c = await create(client)
    from iassistpro.manager import SessionManager

    sid, store = c["session_id"], mgr.store
    rec = store.read_state(sid)
    store.write_state(sid, rec.model_copy(update={"state": "ended", "end_reason": "completed",
                                                   "audio": AudioInfo(state="pending")}))
    s = mgr.s
    mgr2 = SessionManager(s, SessionStore(Path(s.sessions_dir), 24), mgr.model_status, runner=FakeRunner())
    await mgr2.startup()
    await mgr2.audio_recovery   # no raw on disk: the recording is reported failed, never left pending
    assert mgr2.status(sid)["audio_state"] == "failed"


def test_session_ids_are_128_bit_random():
    from iassistpro.manager import new_session_id

    ids = [new_session_id() for _ in range(64)]
    assert len(set(ids)) == 64 and all(len(i) == 32 and int(i, 16) >= 0 for i in ids)   # 32 hex = 128 bits
    # every bit varies: unlike uuid4, whose version and variant bits are fixed (122 random bits)
    bits = [int(i, 16) for i in ids]
    assert all(any(b >> k & 1 for b in bits) and not all(b >> k & 1 for b in bits) for k in range(128))


# ---- audio checks (UX7; plan "Contract additions")

async def create_check(client):
    r = await client.post("/checks")
    assert r.status_code == 201, r.text
    return r.json()


async def check_state(client, c):
    return (await client.get(f"/checks/{c['check_id']}")).json()


async def _check_is(client, c, want):
    return (await check_state(client, c))["state"] == want


async def test_create_check_returns_connect_info(client):
    import re

    c = await create_check(client)
    conn = c["connect"]
    assert re.fullmatch(r"[0-9a-f]{32}", c["check_id"])
    assert conn["transport"] == "smallwebrtc" and conn["offer_path"] == f"/checks/{c['check_id']}/offer"
    assert len(conn["token"]) >= 40
    expires = datetime.fromisoformat(conn["expires_at"].replace("Z", "+00:00"))
    assert abs((expires - datetime.now(timezone.utc)).total_seconds() - 120) < 5
    st = await check_state(client, c)
    assert st == {"check_id": c["check_id"], "state": "waiting", "elapsed_s": 0.0, "end_reason": None, "error": None}
    assert (await client.post("/checks", json={})).status_code == 409   # {} is accepted as a body; busy now


async def test_check_and_session_share_the_lock(client):
    c = await create_check(client)
    r = await client.post("/sessions", json=BRIEF)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "busy"
    r = await client.post("/checks")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "busy" and c["check_id"] not in r.text
    assert (await client.get("/health")).json()["busy"] is True
    assert (await client.post(f"/checks/{c['check_id']}/end")).status_code == 202
    await create(client)
    r = await client.post("/checks")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "busy"


async def test_check_not_ready_is_503(tmp_path):
    client, *_ = make(tmp_path, state="loading_models")
    async with client:
        r = await client.post("/checks")
        assert r.status_code == 503 and r.json()["detail"]["code"] == "not_ready"
        assert r.json()["detail"]["status"] == "loading_models"


async def test_check_offer_token_is_single_use(client):
    c = await create_check(client)
    r = await offer(client, c)
    assert r.status_code == 200 and r.json()["pc_id"] == "pc-1"
    await until(lambda: _check_is(client, c, "live"))
    r2 = await offer(client, c)
    assert r2.status_code == 409 and r2.json()["detail"]["code"] == "wrong_state"
    bad = await offer(client, c, token="x" * 43)
    assert bad.status_code == 401 and bad.json()["detail"]["code"] == "token_invalid"
    p = await client.patch(c["connect"]["offer_path"], json={"pc_id": "pc-1", "candidates": []},
                           headers={"X-Session-Token": c["connect"]["token"]})
    assert p.status_code == 200 and p.json() == {"status": "success"}


async def test_unused_check_expires_and_frees_the_lock(ctx):
    client, mgr, clock, *_ = ctx
    c = await create_check(client)
    clock.t += 121
    await mgr.housekeeping(once=True)
    st = await check_state(client, c)
    assert (st["state"], st["error"], st["end_reason"]) == ("failed", "not_connected", "error")
    r = await offer(client, c)
    assert r.status_code == 410 and r.json()["detail"]["code"] == "token_expired"
    await create(client)


async def test_end_check_frees_the_lock_before_replying(client):
    c = await create_check(client)
    await offer(client, c)
    await until(lambda: _check_is(client, c, "live"))
    r = await client.post(f"/checks/{c['check_id']}/end")
    assert r.status_code == 202
    assert (r.json()["state"], r.json()["end_reason"]) == ("ended", "ended_by_client")
    await create(client)   # immediately: not busy


async def test_end_check_cancels_a_stuck_check_after_the_wait(tmp_path, monkeypatch):
    import iassistpro.manager

    monkeypatch.setattr(iassistpro.manager, "CHECK_END_WAIT_S", 0.1)
    client, *_ = make(tmp_path, check_runner=FakeCheckRunner(stuck=True))
    async with client:
        c = await create_check(client)
        await offer(client, c)
        await until(lambda: _check_is(client, c, "live"))
        r = await client.post(f"/checks/{c['check_id']}/end")
        assert r.status_code == 202
        assert (r.json()["state"], r.json()["end_reason"]) == ("ended", "ended_by_client")
        await create(client)


async def test_end_check_is_idempotent(client):
    c = await create_check(client)
    await offer(client, c)
    await until(lambda: _check_is(client, c, "live"))
    first = await client.post(f"/checks/{c['check_id']}/end")
    second = await client.post(f"/checks/{c['check_id']}/end")
    assert second.status_code == 202 and second.json() == first.json()


async def test_end_waiting_check_ends_it_at_once(client):
    c = await create_check(client)
    r = await client.post(f"/checks/{c['check_id']}/end")
    assert (r.status_code, r.json()["state"], r.json()["end_reason"]) == (202, "ended", "ended_by_client")
    await create_check(client)


async def test_disconnected_check_frees_the_lock(ctx):
    client, mgr, *_ = ctx
    c = await create_check(client)
    await offer(client, c)
    await until(lambda: _check_is(client, c, "live"))
    mgr.check_runner.disconnect.set()
    await until(lambda: _check_is(client, c, "ended"))
    assert (await check_state(client, c))["end_reason"] == "disconnected"
    await create_check(client)


async def test_check_stores_nothing(ctx, tmp_path):
    client, mgr, *_ = ctx
    c = await create_check(client)
    await offer(client, c)
    await until(lambda: _check_is(client, c, "live"))
    await client.post(f"/checks/{c['check_id']}/end")
    sessions = tmp_path / "sessions"
    assert not sessions.exists() or list(sessions.iterdir()) == []


async def test_check_and_session_ids_do_not_cross(client):
    c = await create_check(client)
    r = await client.get(f"/sessions/{c['check_id']}")
    assert r.status_code == 404 and r.json()["detail"]["code"] == "unknown_session"
    await client.post(f"/checks/{c['check_id']}/end")
    sess = await create(client)
    r = await client.get(f"/checks/{sess['session_id']}")
    assert r.status_code == 404 and r.json()["detail"]["code"] == "unknown_check"


async def test_unknown_check_404(client):
    unknown = "0" * 32
    for r in (await client.get(f"/checks/{unknown}"), await client.post(f"/checks/{unknown}/end"),
              await client.post(f"/checks/{unknown}/offer", json=OFFER, headers={"X-Session-Token": "x" * 43})):
        assert r.status_code == 404 and r.json()["detail"]["code"] == "unknown_check"


async def test_finished_checks_are_forgotten_after_10_minutes(ctx):
    client, mgr, clock, *_ = ctx
    c = await create_check(client)
    await client.post(f"/checks/{c['check_id']}/end")
    clock.t += 599
    await mgr.housekeeping(once=True)
    assert (await client.get(f"/checks/{c['check_id']}")).status_code == 200
    clock.t += 2
    await mgr.housekeeping(once=True)
    r = await client.get(f"/checks/{c['check_id']}")
    assert r.status_code == 404 and r.json()["detail"]["code"] == "unknown_check"


async def test_checks_need_smallwebrtc(tmp_path):
    client, *_ = make(tmp_path, transport="websocket")
    async with client:
        r = await client.post("/checks")
        assert r.status_code == 409 and r.json()["detail"]["code"] == "wrong_state"
