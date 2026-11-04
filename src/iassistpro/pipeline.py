"""One live interview: the Pipecat pipeline, per-turn persistence (AD4), and the end-of-session hand-off."""

import asyncio
import time
from typing import Protocol

from pipecat.frames.frames import LLMRunFrame, MetricsFrame
from pipecat.metrics.metrics import LLMUsageMetricsData, STTUsageMetricsData, TTSUsageMetricsData
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair, LLMUserAggregatorParams
from pipecat.workers.runner import WorkerRunner

from .config import Settings
from .contract import AudioInfo, Brief, EndReason, Latency, Turn, Usage
from .director import InterviewDirector, InterviewState, RerunGate
from .levels import InputLevelMonitor
from .limits import CostMeter, LimitTracker
from .logs import log_event
from .prompt import JOIN_MESSAGE, TOOLS, build_system_prompt
from .providers import make_llm, make_stt, make_tts, make_turn_strategies, spec
from .recorder import AudioSink, encode, make_buffer
from .store import SessionStore
from .transcript import TranscriptBuilder, TranscriptObserver


class SessionHooks(Protocol):
    brief: Brief
    session_id: str
    store: SessionStore
    end_requested: asyncio.Event
    latency: Latency  # set by run_interview just before on_end

    def on_live(self) -> None: ...
    def on_progress(self, question_id: str | None, kind: str, number: int) -> None: ...
    def on_end(self, reason: EndReason, error: str | None) -> None: ...
    def on_audio(self, info: AudioInfo) -> None: ...


def _usage_rows(frame: MetricsFrame, kind: str, provider: str, default_model: str) -> list[Usage]:
    rows = []
    for d in frame.data:
        model = getattr(d, "model", None) or default_model
        if kind == "llm" and isinstance(d, LLMUsageMetricsData):
            v = d.value
            rows.append(Usage(kind="llm", provider=provider, model=model, input_tokens=v.prompt_tokens or 0,
                              cached_input_tokens=v.cache_read_input_tokens or 0,
                              cache_write_tokens=v.cache_creation_input_tokens or 0,
                              output_tokens=v.completion_tokens or 0))
        elif kind == "tts" and isinstance(d, TTSUsageMetricsData):
            rows.append(Usage(kind="tts", provider=provider, model=model, characters=int(d.value)))
        elif kind == "stt" and isinstance(d, STTUsageMetricsData):
            rows.append(Usage(kind="stt", provider=provider, model=model,
                              seconds=getattr(d.value, "seconds", None) or getattr(d.value, "duration_s", None)))
    return rows


async def run_interview(hooks: SessionHooks, transport, s: Settings, *, stt=None, tts=None, llm=None,
                        turn_strategies=None, vad=None, close_timeout_s: float = 15.0,
                        level_monitor: InputLevelMonitor | None = None) -> None:
    brief, store, sid = hooks.brief, hooks.store, hooks.session_id
    t_setup = time.monotonic()
    meter = CostMeter(brief.llm_prices_per_mtok, brief.limits.max_llm_cost_usd)
    state = InterviewState(brief, meter, LimitTracker(brief.limits, [q.id for q in brief.planned_questions]))
    llm_model = brief.llm_model or s.llm_model
    stt = stt if stt is not None else make_stt(s)
    tts = tts if tts is not None else make_tts(s)
    llm = llm if llm is not None else make_llm(s, brief, build_system_prompt(brief))
    if turn_strategies is None:
        turn_strategies, vad = make_turn_strategies(s)

    def on_turn(turn: Turn) -> None:
        store.append_turn(sid, turn)
        log_event("turn_closed", speaker=turn.speaker, kind=turn.kind, chars=len(turn.text),
                  ms=turn.end_ms - turn.start_ms)

    def on_retract(n: int) -> None:
        store.drop_last_turns(sid, n)

    builder = TranscriptBuilder(on_turn=on_turn, vad_stop_s=s.vad_stop_s, on_retract=on_retract)
    observer = TranscriptObserver(builder)
    director = InterviewDirector(state, llm, elapsed=observer.elapsed_s, on_progress=hooks.on_progress,
                                 close_timeout_s=close_timeout_s, resume_window_s=s.resume_window_s)

    def persist_usage(rows: list[Usage]) -> None:
        for r in rows:
            meter.add(r)
        store.write_usage(sid, meter.rows())

    @llm.event_handler("on_before_push_frame")
    async def _llm_usage(_proc, frame):
        if isinstance(frame, MetricsFrame):
            rows = _usage_rows(frame, "llm", s.llm_provider, llm_model)
            if rows:
                persist_usage(rows)
                log_event("llm_usage", spent_usd=round(meter.spent_usd, 6), runs=director.llm_runs)

    for kind, proc, name, model in (("tts", tts, s.tts_provider, s.tts_voice_resolved),
                                    ("stt", stt, s.stt_provider, s.stt_model)):
        try:
            remote = spec(kind, name).remote
        except Exception:
            remote = False
        if remote:
            @proc.event_handler("on_before_push_frame")
            async def _speech_usage(_proc, frame, kind=kind, name=name, model=model):
                if isinstance(frame, MetricsFrame):
                    rows = _usage_rows(frame, kind, name, model)
                    if rows:
                        persist_usage(rows)

    context = LLMContext(messages=[{"role": "user", "content": JOIN_MESSAGE}], tools=TOOLS(brief))
    aggs = LLMContextAggregatorPair(
        context, user_params=LLMUserAggregatorParams(vad_analyzer=vad, user_turn_strategies=turn_strategies))
    buffer = make_buffer()
    raw_path = store.dir(sid) / "audio.raw"
    sink = AudioSink(raw_path)
    sink.attach(buffer)

    monitor = level_monitor if level_monitor is not None else InputLevelMonitor()
    pipeline = Pipeline([transport.input(), monitor, stt, aggs.user(), director, llm, RerunGate(director), tts, transport.output(), buffer,
                         aggs.assistant()])
    worker = PipelineWorker(
        pipeline,
        params=PipelineParams(enable_metrics=True, enable_usage_metrics=True, audio_in_sample_rate=16000,
                              audio_out_sample_rate=24000),
        observers=[observer],
        cancel_on_idle_timeout=False,
        idle_timeout_secs=None,
    )

    @transport.event_handler("on_client_connected")
    async def _connected(_t, _client):
        observer.start()
        log_event("client_connected", setup_ms=int(1000 * (time.monotonic() - t_setup)))
        await monitor.start_watchdog()
        hooks.on_live()
        await buffer.start_recording()
        await worker.queue_frames([LLMRunFrame()])

    @transport.event_handler("on_client_disconnected")
    async def _disconnected(_t, _client):
        log_event("client_disconnected", ending=state.ending)
        state.on_disconnect()
        await worker.cancel()

    async def watch_end_request():
        await hooks.end_requested.wait()
        await director.request_end()

    watcher = asyncio.create_task(watch_end_request())
    error: str | None = None
    try:
        runner = WorkerRunner(handle_sigint=False)
        await runner.add_workers(worker)
        await runner.run()
    except Exception as e:  # logged by type only: messages can carry content
        state.on_error()
        error = f"pipeline_error:{type(e).__name__}"
        log_event("pipeline_error", error=type(e).__name__)
    finally:
        watcher.cancel()
        await monitor.stop_watchdog()
        builder.flush(int(1000 * observer.elapsed_s()))
        try:
            await buffer.stop_recording()  # no-op if the End/Cancel frame already flushed it
        except Exception:
            pass
        await asyncio.sleep(0.2)  # on_audio_data handlers run as tasks; let the last chunk land
        sink.close()
        store.write_usage(sid, meter.rows())
        hooks.latency = builder.latency()
        reason = state.final_end_reason()
        log_event("session_end", reason=reason, turns=len(builder.turns), llm_runs=director.llm_runs,
                  spent_usd=round(meter.spent_usd, 6), median_reply_ms=hooks.latency.median_reply_ms)
        hooks.on_end(reason, error or state.error_code)
    info = await encode(raw_path, store.dir(sid) / "audio.ogg", s.debug_stereo_wav)
    hooks.on_audio(info)
