"""Per-session temp folder: written turn by turn so a crash still reports spend (AD4); expires after ttl_h.

Layout {root}/{session_id}/: meta.json (prices, limits, model, counts; never brief text), state.json,
turns.jsonl (one Turn per line), usage.json (replaced atomically), audio.raw (transient), audio.ogg,
audio.reencode (transient: marks a startup re-encode in progress, so a crash during it isn't retried).
"""

import json
import os
import shutil
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ValidationError

from .contract import AudioInfo, Brief, EndReason, Latency, Limits, Prices, Result, Turn, Usage, llm_cost_usd
from .logs import log_event


class SessionMeta(BaseModel):
    created_at: datetime
    prices: Prices
    limits: Limits
    llm_model: str | None
    questions_total: int

    @classmethod
    def from_brief(cls, brief: Brief, created_at: datetime) -> "SessionMeta":
        return cls(created_at=created_at, prices=brief.llm_prices_per_mtok, limits=brief.limits,
                   llm_model=brief.llm_model, questions_total=len(brief.planned_questions))


class StateRecord(BaseModel):
    state: Literal["waiting", "live", "ended", "failed"]
    end_reason: EndReason | None = None
    error: str | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    audio: AudioInfo = AudioInfo(state="pending")
    latency: Latency = Latency()


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


class SessionStore:
    def __init__(self, root: Path, ttl_h: int, keep_raw: bool = False):
        self.root = Path(root)
        self.ttl = timedelta(hours=ttl_h)
        self.keep_raw = keep_raw
        self.root.mkdir(parents=True, exist_ok=True)

    def dir(self, session_id: str) -> Path:
        return self.root / session_id

    def exists(self, session_id: str) -> bool:
        return (self.dir(session_id) / "meta.json").exists()

    def create(self, session_id: str, meta: SessionMeta) -> None:
        d = self.dir(session_id)
        d.mkdir(parents=True, exist_ok=False)
        _atomic_write(d / "meta.json", meta.model_dump_json())
        self.write_state(session_id, StateRecord(state="waiting", audio=AudioInfo(state="none")))

    def read_meta(self, session_id: str) -> SessionMeta:
        return SessionMeta.model_validate_json((self.dir(session_id) / "meta.json").read_text(encoding="utf-8"))

    def write_state(self, session_id: str, rec: StateRecord) -> None:
        _atomic_write(self.dir(session_id) / "state.json", rec.model_dump_json())

    def read_state(self, session_id: str) -> StateRecord:
        return StateRecord.model_validate_json((self.dir(session_id) / "state.json").read_text(encoding="utf-8"))

    def append_turn(self, session_id: str, turn: Turn) -> None:
        with open(self.dir(session_id) / "turns.jsonl", "a", encoding="utf-8") as f:
            f.write(turn.model_dump_json() + "\n")
            f.flush()
            os.fsync(f.fileno())

    def drop_last_turns(self, session_id: str, n: int) -> None:
        """Remove the last n turns (a fragment and the reply it cancelled, merged back by a resume)."""
        if n <= 0:
            return
        p = self.dir(session_id) / "turns.jsonl"
        lines = p.read_text(encoding="utf-8").splitlines() if p.exists() else []
        _atomic_write(p, "".join(line + "\n" for line in lines[: max(0, len(lines) - n)]))

    def write_usage(self, session_id: str, usage: list[Usage]) -> None:
        _atomic_write(self.dir(session_id) / "usage.json", json.dumps([u.model_dump() for u in usage]))

    def read_turns(self, session_id: str) -> list[Turn]:
        p = self.dir(session_id) / "turns.jsonl"
        turns = []
        if p.exists():
            for line in p.read_text(encoding="utf-8").splitlines():
                try:
                    turns.append(Turn.model_validate_json(line))
                except ValidationError:
                    log_event("turn_line_corrupt", session=session_id)  # a torn last line after a crash
        return turns

    def read_usage(self, session_id: str) -> list[Usage]:
        p = self.dir(session_id) / "usage.json"
        try:
            return [Usage.model_validate(u) for u in json.loads(p.read_text(encoding="utf-8"))]
        except FileNotFoundError:
            return []
        except (ValueError, TypeError, ValidationError):
            log_event("usage_file_corrupt", session=session_id)
            return []

    def result(self, session_id: str) -> Result:
        meta = self.read_meta(session_id)
        st = self.read_state(session_id)
        usage = self.read_usage(session_id)
        return Result(
            version=1, session_id=session_id, end_reason=st.end_reason or "error", error=st.error,
            started_at=st.started_at, ended_at=st.ended_at or meta.created_at, turns=self.read_turns(session_id),
            usage=usage, llm_cost_usd=round(llm_cost_usd(usage, meta.prices), 6), audio=st.audio, latency=st.latency,
        )

    def delete(self, session_id: str) -> None:
        shutil.rmtree(self.dir(session_id), ignore_errors=True)

    def _ids(self) -> list[str]:
        return sorted(p.name for p in self.root.iterdir() if p.is_dir() and (p / "meta.json").exists())

    def sweep(self, now: datetime) -> list[str]:
        removed = []
        for sid in self._ids():
            try:
                st, meta = self.read_state(sid), self.read_meta(sid)
            except (OSError, ValidationError, ValueError):
                continue
            if st.state in ("waiting", "live"):
                continue
            if now - (st.ended_at or meta.created_at) > self.ttl:
                self.delete(sid)
                removed.append(sid)
        if removed:
            log_event("sweep", removed=len(removed))
        return removed

    def recover_on_startup(self, now: datetime) -> list[str]:
        recovered = []
        for sid in self._ids():
            try:
                st = self.read_state(sid)
            except (OSError, ValidationError, ValueError):
                st = StateRecord(state="live")
            if st.state not in ("waiting", "live"):
                continue
            d = self.dir(sid)
            ogg = d / "audio.ogg"
            audio = (AudioInfo(state="ready", format="ogg_opus", bytes=ogg.stat().st_size) if ogg.exists()
                     else AudioInfo(state="none"))
            raw = d / "audio.raw"
            if raw.exists() and not self.keep_raw:
                raw.unlink()
            self.write_state(sid, st.model_copy(update=dict(state="failed", end_reason="error", error="service_restarted",
                                                            ended_at=now, audio=audio)))
            recovered.append(sid)
        if recovered:
            log_event("recovered_on_startup", sessions=len(recovered))
        return recovered

    def recover_pending_audio(self) -> list[str]:
        """Finished sessions whose encoding was cut off by a restart (audio pending): re-encode once from
        audio.raw, else report the recording failed and delete the raw (AF4). Blocking: run it off the loop."""
        from . import recorder

        done = []
        for sid in self._ids():
            try:
                st = self.read_state(sid)
            except (OSError, ValidationError, ValueError):
                continue
            if st.state not in ("ended", "failed") or st.audio.state != "pending":
                continue
            d = self.dir(sid)
            raw, marker = d / "audio.raw", d / "audio.reencode"
            audio = AudioInfo(state="failed")
            if raw.exists() and not marker.exists():
                marker.write_text("")
                try:
                    audio = recorder._encode_sync(raw, d / "audio.ogg", False)
                except Exception as e:
                    log_event("audio_reencode_failed", session=sid, error=type(e).__name__)
            if not self.keep_raw:
                raw.unlink(missing_ok=True)
            self.write_state(sid, st.model_copy(update=dict(audio=audio)))
            marker.unlink(missing_ok=True)
            log_event("audio_recovered", session=sid, state=audio.state)
            done.append(sid)
        return done
