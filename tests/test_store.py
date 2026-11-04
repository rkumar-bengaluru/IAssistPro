import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from iassistpro.contract import AudioInfo, Brief, Limits, Prices, Turn, Usage
from iassistpro.store import SessionMeta, SessionStore, StateRecord
from tests.conftest import CANARY

NOW = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)
HAIKU = Prices(input=0.10, cached_input=0.01, cache_write=0.125, output=0.50)
EXAMPLE = Path(__file__).resolve().parents[1] / "contracts" / "examples" / "brief.example.json"


def meta(created_at=NOW) -> SessionMeta:
    return SessionMeta(created_at=created_at, prices=HAIKU, limits=Limits(), llm_model="claude-haiku-5-5", questions_total=6)


def live() -> StateRecord:
    return StateRecord(state="live", started_at=NOW, audio=AudioInfo(state="pending"))


def turn(speaker, i=0) -> Turn:
    return Turn(speaker=speaker, text=f"t{i}", start_ms=i * 1000, end_ms=i * 1000 + 500, question_id="q1", kind="main")


def haiku_usage(**kw) -> Usage:
    return Usage(provider="anthropic", model="claude-haiku-5-5", **kw)


def test_usage_is_replaced_atomically_after_each_turn(tmp_path):
    st = SessionStore(tmp_path, 24)
    st.create("s1", meta())
    st.write_state("s1", live())
    st.write_usage("s1", [haiku_usage(input_tokens=10)])
    st.write_usage("s1", [haiku_usage(input_tokens=10), haiku_usage(input_tokens=20)])
    st.write_state("s1", StateRecord(state="ended", end_reason="completed", started_at=NOW, ended_at=NOW,
                                     audio=AudioInfo(state="pending")))
    assert len(st.result("s1").usage) == 2
    assert not list(tmp_path.rglob("*.tmp"))


def test_turns_append_in_order_and_survive_reopen(tmp_path):
    st = SessionStore(tmp_path, 24)
    st.create("s1", meta())
    st.write_state("s1", live())
    for i, sp in enumerate(["interviewer", "candidate", "interviewer"]):
        st.append_turn("s1", turn(sp, i))
    r = SessionStore(tmp_path, 24).result("s1")
    assert [(t.speaker, t.text) for t in r.turns] == [("interviewer", "t0"), ("candidate", "t1"), ("interviewer", "t2")]


def test_recover_on_startup_marks_live_failed_with_usage(tmp_path):
    st = SessionStore(tmp_path, 24)
    st.create("s1", meta())
    st.write_state("s1", live())
    st.append_turn("s1", turn("candidate"))
    st.write_usage("s1", [haiku_usage(input_tokens=1000, output_tokens=100)])
    (st.dir("s1") / "audio.raw").write_bytes(b"\0" * 64)
    assert SessionStore(tmp_path, 24).recover_on_startup(NOW) == ["s1"]
    r = SessionStore(tmp_path, 24).result("s1")
    assert (r.end_reason, r.error, len(r.turns), r.audio.state) == ("error", "service_restarted", 1, "none")
    assert r.llm_cost_usd == pytest.approx((1000 * 0.10 + 100 * 0.50) / 1e6)
    assert r.ended_at == NOW and not (st.dir("s1") / "audio.raw").exists()
    assert st.read_state("s1").state == "failed"


def test_recover_waiting_session_too(tmp_path):
    st = SessionStore(tmp_path, 24)
    st.create("w1", meta())
    st.write_state("w1", StateRecord(state="waiting", audio=AudioInfo(state="none")))
    assert st.recover_on_startup(NOW) == ["w1"]


def test_ended_sessions_are_not_touched_by_recovery(tmp_path):
    st = SessionStore(tmp_path, 24)
    st.create("s1", meta())
    rec = StateRecord(state="ended", end_reason="completed", started_at=NOW, ended_at=NOW - timedelta(minutes=5),
                      audio=AudioInfo(state="ready", format="ogg_opus", duration_ms=1000, bytes=10))
    st.write_state("s1", rec)
    assert st.recover_on_startup(NOW) == []
    assert st.read_state("s1") == rec


# ---- AF4: a crash during encoding must not leave the audio pending until the TTL

def ended_pending(st, sid="s1"):
    st.create(sid, meta())
    st.write_state(sid, StateRecord(state="ended", end_reason="completed", started_at=NOW, ended_at=NOW,
                                    audio=AudioInfo(state="pending")))


def stereo_raw(seconds=0.5) -> bytes:
    import numpy as np

    n = int(16000 * seconds)
    tone = (8000 * np.sin(np.arange(n) * 2 * np.pi * 440 / 16000)).astype(np.int16)
    return np.stack([tone, tone], axis=1).tobytes()


def test_recover_reencodes_pending_audio(tmp_path):
    st = SessionStore(tmp_path, 24)
    ended_pending(st)
    (st.dir("s1") / "audio.raw").write_bytes(stereo_raw())
    st2 = SessionStore(tmp_path, 24)
    assert st2.recover_on_startup(NOW) == []
    assert st2.recover_pending_audio() == ["s1"]
    a = st2.read_state("s1").audio
    assert (a.state, a.format) == ("ready", "ogg_opus") and a.duration_ms == 500 and a.bytes > 0
    assert (st.dir("s1") / "audio.ogg").exists() and not (st.dir("s1") / "audio.raw").exists()
    assert st2.read_state("s1").state == "ended"


def test_recover_marks_audio_failed_without_raw(tmp_path):
    st = SessionStore(tmp_path, 24)
    ended_pending(st)
    assert st.recover_pending_audio() == ["s1"]
    assert st.read_state("s1").audio.state == "failed"


def test_recover_encodes_only_once(tmp_path):
    # A crash during the recovery's own encode leaves its marker: the next start gives up and deletes the raw.
    st = SessionStore(tmp_path, 24)
    ended_pending(st)
    (st.dir("s1") / "audio.raw").write_bytes(stereo_raw())
    (st.dir("s1") / "audio.reencode").write_text("")
    assert st.recover_pending_audio() == ["s1"]
    assert st.read_state("s1").audio.state == "failed"
    assert not (st.dir("s1") / "audio.raw").exists() and not (st.dir("s1") / "audio.reencode").exists()


def test_recover_marks_audio_failed_when_encoding_fails(tmp_path, monkeypatch):
    from iassistpro import recorder

    def boom(*a, **k):
        raise RuntimeError("bad raw")

    monkeypatch.setattr(recorder, "_encode_sync", boom)
    st = SessionStore(tmp_path, 24)
    ended_pending(st)
    (st.dir("s1") / "audio.raw").write_bytes(stereo_raw())
    assert st.recover_pending_audio() == ["s1"]
    assert st.read_state("s1").audio.state == "failed" and not (st.dir("s1") / "audio.raw").exists()


def test_sweep_deletes_after_ttl_only(tmp_path):
    st = SessionStore(tmp_path, 24)
    for sid, age in [("keep", timedelta(hours=23, minutes=59)), ("old", timedelta(hours=24, minutes=1))]:
        st.create(sid, meta(created_at=NOW - age - timedelta(minutes=30)))
        st.write_state(sid, StateRecord(state="ended", end_reason="completed", ended_at=NOW - age,
                                        audio=AudioInfo(state="none")))
    st.create("never_ended", meta(created_at=NOW - timedelta(hours=25)))
    st.write_state("never_ended", StateRecord(state="failed", end_reason="error", error="not_connected",
                                              audio=AudioInfo(state="none")))
    assert sorted(st.sweep(NOW)) == ["never_ended", "old"]
    assert st.exists("keep") and not st.exists("old")


def test_corrupt_usage_file_gives_empty_usage_not_crash(tmp_path):
    st = SessionStore(tmp_path, 24)
    st.create("s1", meta())
    st.write_state("s1", StateRecord(state="failed", end_reason="error", error="boom", ended_at=NOW,
                                     audio=AudioInfo(state="none")))
    (st.dir("s1") / "usage.json").write_text("{not json")
    r = st.result("s1")
    assert r.usage == [] and r.error == "boom" and r.llm_cost_usd == 0


def test_meta_holds_no_brief_text(tmp_path):
    data = json.loads(EXAMPLE.read_text())
    data["role"]["summary"] += f" {CANARY}"
    data["candidate"]["summary"] += f" {CANARY}"
    data["questions"][0]["text"] += f" {CANARY}"
    brief = Brief.model_validate(data)
    st = SessionStore(tmp_path, 24)
    st.create("s1", SessionMeta.from_brief(brief, NOW))
    text = (st.dir("s1") / "meta.json").read_text()
    assert CANARY not in text and "Northwind" not in text and json.loads(text)["questions_total"] == 6


def test_delete_removes_folder(tmp_path):
    st = SessionStore(tmp_path, 24)
    st.create("s1", meta())
    st.delete("s1")
    assert not st.exists("s1")
