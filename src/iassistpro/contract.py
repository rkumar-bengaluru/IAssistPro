"""Versioned brief/result contract (version 1). JSON Schemas in contracts/ are generated from these models."""

import re
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator

CONTACT_RULE = (
    "No contact details are allowed in a brief: any string holding an email address, a URL (http(s):// or www.), "
    "or a run of digits, spaces and ().+- with 10 or more digits (a phone number) is refused. "
    "Year ranges such as 2019-2023 are fine."
)

_EMAIL = re.compile(r"\S+@\S+\.\S+")
_URL = re.compile(r"https?://|www\.", re.IGNORECASE)
_DIGIT_RUN = re.compile(r"[\d\s().+-]+")


def _has_contact(text: str) -> bool:
    if _EMAIL.search(text) or _URL.search(text):
        return True
    return any(sum(c.isdigit() for c in m.group()) >= 10 for m in _DIGIT_RUN.finditer(text))


def _strings(value, path: str):
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, dict):
        for k, v in value.items():
            yield from _strings(v, f"{path}.{k}" if path else str(k))
    elif isinstance(value, list):
        for i, v in enumerate(value):
            yield from _strings(v, f"{path}.{i}")


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Role(_Model):
    title: str = Field(min_length=1, max_length=120)
    company: str = Field(min_length=1, max_length=120)
    summary: str = Field(min_length=1, max_length=2000)


class Candidate(_Model):
    name: str = Field(min_length=1, max_length=40, pattern=r"^[^\d@/:]+$", description="First name only.")
    summary: str = Field(min_length=1, max_length=3000)


QuestionKind = Literal["must_have", "cv_deep_dive", "gap", "behavioural", "motivation", "own"]
ProbeText = Annotated[str, StringConstraints(min_length=1, max_length=200)]


class Question(_Model):
    id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,32}$")
    text: str = Field(min_length=1, max_length=500)
    kind: QuestionKind
    probe_for: list[ProbeText] = Field(default_factory=list, max_length=8)


class Limits(_Model):
    max_minutes: int = Field(default=25, ge=1, le=45)
    max_main_questions: int = Field(default=8, ge=1, le=15)
    max_follow_ups: int = Field(default=2, ge=0, le=3)
    max_llm_cost_usd: float = Field(default=0.50, gt=0, le=2.0)


class Prices(_Model):
    """USD per million tokens."""

    input: float = Field(ge=0, le=100)
    cached_input: float = Field(ge=0, le=100)
    cache_write: float = Field(ge=0, le=100)
    output: float = Field(ge=0, le=100)


class Brief(_Model):
    model_config = ConfigDict(extra="forbid", json_schema_extra={"description": CONTACT_RULE})

    version: Literal[1]
    role: Role
    candidate: Candidate
    questions: list[Question] = Field(min_length=1, max_length=20)
    limits: Limits = Field(default_factory=Limits)
    llm_prices_per_mtok: Prices
    llm_model: str | None = Field(default=None, pattern=r"^[a-z0-9.-]{1,64}$")
    language: Literal["en"] = "en"

    @field_validator("questions")
    @classmethod
    def _unique_ids(cls, qs: list[Question]) -> list[Question]:
        ids = [q.id for q in qs]
        if len(set(ids)) != len(ids):
            raise ValueError("question ids must be unique")
        return qs

    @model_validator(mode="after")
    def _no_contact_details(self) -> "Brief":
        for path, text in _strings(self.model_dump(), ""):
            if _has_contact(text):
                raise ValueError(f"contact details are not allowed in a brief ({path})")
        return self

    @property
    def planned_questions(self) -> list[Question]:
        return self.questions[: self.limits.max_main_questions]


EndReason = Literal["completed", "time_limit", "budget", "ended_by_candidate", "disconnected", "error"]
TurnKind = Literal["main", "follow_up", "greeting", "closing"]


class Turn(_Model):
    speaker: Literal["interviewer", "candidate"]
    text: str
    start_ms: int = Field(ge=0)
    end_ms: int = Field(ge=0)
    question_id: str | None
    kind: TurnKind


class Usage(_Model):
    kind: Literal["llm", "stt", "tts"] = "llm"
    provider: str
    model: str
    input_tokens: int = 0
    cached_input_tokens: int = 0
    cache_write_tokens: int = 0
    output_tokens: int = 0
    seconds: float | None = None
    characters: int | None = None


class AudioInfo(_Model):
    state: Literal["pending", "ready", "none", "failed"]
    format: Literal["ogg_opus", "wav"] | None = None
    duration_ms: int = 0
    bytes: int = 0


class Latency(_Model):
    n: int = 0
    median_reply_ms: int | None = None
    p90_reply_ms: int | None = None
    measured: Literal["server"] = "server"
    resumes: int = 0  # answers resumed right after a turn end and merged back (AT2); added field, optional


class Result(_Model):
    version: Literal[1]
    session_id: str
    end_reason: EndReason
    error: str | None
    started_at: datetime | None
    ended_at: datetime
    turns: list[Turn]
    usage: list[Usage]
    llm_cost_usd: float
    audio: AudioInfo
    latency: Latency


def llm_cost_usd(usage: list[Usage], prices: Prices) -> float:
    """Cost of the LLM rows only. input_tokens is Anthropic's uncached input."""
    total = 0.0
    for u in usage:
        if u.kind != "llm":
            continue
        total += (
            u.input_tokens * prices.input
            + u.cached_input_tokens * prices.cached_input
            + u.cache_write_tokens * prices.cache_write
            + u.output_tokens * prices.output
        )
    return total / 1e6


def export_schemas() -> dict[str, dict]:
    return {
        "brief.v1.schema.json": Brief.model_json_schema(),
        "result.v1.schema.json": Result.model_json_schema(),
    }
