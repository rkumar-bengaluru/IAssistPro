"""The LLM-less audio check pipeline (spec §3.1; UX7)."""

import asyncio
import uuid
from dataclasses import dataclass, field

from pipecat.frames.frames import TTSSpeakFrame
from pipecat.processors.frame_processor import FrameDirection

from iassistpro.config import Settings
from tests.conftest import CANARY
from tests.pipeline_harness import FakeTransport, FakeTTS, Passthrough, external_strategies


class RecordingTTS(FakeTTS):
    def __init__(self):
        super().__init__()
        self.spoken: list[str] = []

    async def process_frame(self, frame, direction):
        if direction == FrameDirection.DOWNSTREAM and isinstance(frame, TTSSpeakFrame):
            self.spoken.append(frame.text)
        await super().process_frame(frame, direction)


@dataclass
class FakeCheckHooks:
    check_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    end_requested: asyncio.Event = field(default_factory=asyncio.Event)
    live: bool = False
    ended: list = field(default_factory=list)

    def on_live(self):
        self.live = True

    def on_end(self, reason, error):
        self.ended.append((reason, error))


@dataclass
class CheckRun:
    hooks: FakeCheckHooks
    transport: FakeTransport
    tts: RecordingTTS

    @property
    def server_messages(self) -> list[dict]:
        return self.transport.output().server_messages

    def of(self, type_: str) -> list[dict]:
        return [m for m in self.server_messages if m["type"] == type_]


async def wait_for(cond, timeout: float = 3.0):
    for _ in range(int(timeout / 0.01)):
        if cond():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met in time")


async def run_fake_check(script=None, *, end: str | None = "request", max_s: float = 30.0, s: Settings | None = None,
                         timeout: float = 10.0) -> CheckRun:
    """Run a check with fakes: connect, run `script(run)`, then end it (`request`, `disconnect`, or None = wait)."""
    from iassistpro.check import run_check

    run = CheckRun(FakeCheckHooks(), FakeTransport(), RecordingTTS())
    task = asyncio.create_task(run_check(run.hooks, run.transport, s or Settings(llm_provider="fake"),
                                         stt=Passthrough(), tts=run.tts, turn_strategies=external_strategies(),
                                         vad=None, max_s=max_s))

    async def drive():
        await run.transport.input().start_event.wait()
        await run.transport.fire("on_client_connected")
        await wait_for(lambda: run.tts.spoken)
        if script is not None:
            await script(run)
        await asyncio.sleep(0.1)
        if end == "request":
            run.hooks.end_requested.set()
        elif end == "disconnect":
            await run.transport.fire("on_client_disconnected")

    driver = asyncio.create_task(drive())
    await asyncio.wait_for(task, timeout)
    await asyncio.wait_for(driver, 1)
    return run


async def test_check_speaks_the_prompt_on_connect():
    from iassistpro.check import CHECK_PROMPT

    run = await run_fake_check()
    assert run.tts.spoken == [CHECK_PROMPT] and run.hooks.live
    assert CHECK_PROMPT == "This is your interviewer. If you can hear me, press 'I heard it'."


async def test_play_prompt_speaks_it_again():
    async def script(run):
        await run.transport.input().client_message("play_prompt")
        await wait_for(lambda: len(run.tts.spoken) == 2)

    run = await run_fake_check(script)
    assert len(run.tts.spoken) == 2


async def test_listen_then_a_turn_sends_heard():
    async def script(run):
        await run.transport.input().client_message("listen")
        await run.transport.input().say("I am ready for my interview")
        await wait_for(lambda: run.of("heard"))

    run = await run_fake_check(script)
    assert run.of("heard") == [{"type": "heard", "text": "I am ready for my interview"}]


async def test_a_turn_without_listen_sends_nothing():
    async def script(run):
        await run.transport.input().say("I am ready for my interview")
        await asyncio.sleep(0.3)

    run = await run_fake_check(script)
    assert run.of("heard") == []


async def test_each_listen_gives_one_heard():
    async def script(run):
        inp = run.transport.input()
        await inp.client_message("listen")
        await inp.say("first answer")
        await wait_for(lambda: len(run.of("heard")) == 1)
        await inp.say("second answer")
        await asyncio.sleep(0.3)
        assert len(run.of("heard")) == 1
        await inp.client_message("listen")
        await inp.say("third answer")
        await wait_for(lambda: len(run.of("heard")) == 2)

    run = await run_fake_check(script)
    assert [m["text"] for m in run.of("heard")] == ["first answer", "third answer"]


async def test_check_sends_mic_level_and_silent_input():
    async def script(run):
        await run.transport.input().audio(1.0, 0.1)
        await run.transport.input().audio(3.2, None)
        await wait_for(lambda: run.of("silent_input"))

    run = await run_fake_check(script)
    assert len(run.of("mic_level")) >= 5
    assert run.of("silent_input") == [{"type": "silent_input", "reason": "zeros"}]


async def test_check_never_builds_an_llm(monkeypatch):
    import iassistpro.check
    import iassistpro.providers

    def boom(*_a, **_k):
        raise AssertionError("the check must not build an LLM")

    monkeypatch.setattr(iassistpro.providers, "make_llm", boom)

    async def script(run):
        await run.transport.input().client_message("listen")
        await run.transport.input().say("hello")
        await wait_for(lambda: run.of("heard"))

    run = await run_fake_check(script)
    assert run.hooks.ended == [("ended_by_client", None)]
    assert "make_llm" not in vars(iassistpro.check)


async def test_end_request_ends_the_check():
    run = await run_fake_check()
    assert run.hooks.ended == [("ended_by_client", None)]
    assert run.server_messages[-1] == {"type": "check_ended", "reason": "ended_by_client"}


async def test_check_time_limit_ends_it():
    run = await run_fake_check(end=None, max_s=0.3)
    assert run.hooks.ended == [("time_limit", None)]
    assert {"type": "check_ended", "reason": "time_limit"} in run.server_messages


async def test_disconnect_ends_the_check():
    run = await run_fake_check(end="disconnect")
    assert run.hooks.ended == [("disconnected", None)]
    assert run.of("check_ended") == []


async def test_check_logs_no_transcript(captured_logs):
    async def script(run):
        await run.transport.input().client_message("listen")
        await run.transport.input().say(f"ready {CANARY}")
        await wait_for(lambda: run.of("heard"))

    run = await run_fake_check(script)
    assert run.of("heard")[0]["text"] == f"ready {CANARY}"
    logs = captured_logs()
    assert CANARY not in logs and "check_heard" in logs and "check_end" in logs


async def test_check_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("SESSIONS_DIR", str(tmp_path))

    async def script(run):
        await run.transport.input().audio(0.5, 0.1)
        await run.transport.input().client_message("listen")
        await run.transport.input().say("hello")
        await wait_for(lambda: run.of("heard"))

    await run_fake_check(script, s=Settings(llm_provider="fake"))
    assert list(tmp_path.iterdir()) == []


async def test_check_uses_local_speech_even_when_paid_tts_is_configured(monkeypatch):
    """Owner ruling (final review): a check is always $0, so it never builds a paid speech provider."""
    import iassistpro.check

    monkeypatch.setenv("TTS_PROVIDER", "cartesia")
    monkeypatch.setenv("TTS_VOICE", "some-voice-id")
    built = []

    def fake_make_tts(s):
        built.append((s.tts_provider, s.tts_voice_resolved))
        return RecordingTTS()

    monkeypatch.setattr(iassistpro.check, "make_tts", fake_make_tts)
    from iassistpro.check import run_check

    hooks, transport = FakeCheckHooks(), FakeTransport()
    task = asyncio.create_task(run_check(hooks, transport, Settings(llm_provider="fake"), stt=Passthrough(),
                                         turn_strategies=external_strategies(), vad=None, max_s=0.3))
    await transport.input().start_event.wait()
    await transport.fire("on_client_connected")
    await asyncio.wait_for(task, 5)
    assert built == [("piper", "en_US-lessac-medium")]


def test_check_speech_settings_keep_local_providers(monkeypatch):
    from iassistpro.check import check_speech_settings

    monkeypatch.setenv("TTS_PROVIDER", "kokoro")
    s = check_speech_settings(Settings())
    assert (s.stt_provider, s.tts_provider, s.tts_voice_resolved) == ("whisper", "kokoro", "af_heart")
    monkeypatch.setenv("TTS_PROVIDER", "cartesia")
    monkeypatch.setenv("TTS_VOICE", "v")
    s = check_speech_settings(Settings())
    assert (s.tts_provider, s.tts_voice) == ("piper", "")
