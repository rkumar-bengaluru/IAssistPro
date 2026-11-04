"""Spike bot server: one Pipecat voice pipeline reachable over SmallWebRTC (/api/offer) or
FastAPI WebSocket (/ws). Throwaway code for docs/spike-transport-latency.md.

Env:
  STT_MODEL      faster-whisper model (base.en | small.en | distil-small.en)
  STT_BEAM       beam size (Pipecat's service uses faster-whisper's default 5; we override)
  TURN           smart | silence   (Silero VAD + smart-turn v3, or VAD + silence timeout)
  SILENCE_S      total silence for TURN=silence (default 0.8)
  LLM_MODEL      claude-sonnet-5-5 | claude-haiku-5-5
  KOKORO_INT8    1 = use the int8 Kokoro model
  ICE_PORTS      UDP range for WebRTC host candidates, e.g. 40000-40009
  ICE_HOST       address advertised in the SDP answer (127.0.0.1 for Docker Desktop port publishing)
  TAG            label written into result files
Logs carry timings only, never transcript text.
"""

import asyncio
import json
import os
import statistics
import time
import uuid
import wave
from pathlib import Path

import psutil
from fastapi import BackgroundTasks, FastAPI, WebSocket
from fastapi.responses import FileResponse, JSONResponse
from loguru import logger

OUT = Path(os.environ.get("SPIKE_OUT", "/app/out"))
OUT.mkdir(parents=True, exist_ok=True)
MODELS = Path(os.environ.get("MODELS_DIR", "/models"))
STT_MODEL = os.environ.get("STT_MODEL", "small.en")
STT_BEAM = int(os.environ.get("STT_BEAM", "1"))
TURN = os.environ.get("TURN", "smart")
SILENCE_S = float(os.environ.get("SILENCE_S", "0.8"))
LLM_MODEL = os.environ.get("LLM_MODEL", "claude-haiku-5-5")
KOKORO_INT8 = os.environ.get("KOKORO_INT8", "0") == "1"
TAG = os.environ.get("TAG", "run")
VAD_STOP_S = 0.2


# --------------------------------------------------------------------------- ICE patch
def patch_aioice(port_lo: int, port_hi: int, advertised: str):
    """Make aiortc/aioice bind host candidates inside a fixed UDP range (published by Docker)
    and advertise `advertised` (127.0.0.1) instead of the container's private IP."""
    import aioice.ice as ice
    from aioice.candidate import Candidate, candidate_foundation, candidate_priority

    async def get_component_candidates(self, component, addresses, timeout=5):
        loop = asyncio.get_running_loop()
        for port in range(port_lo, port_hi + 1):
            try:
                _, protocol = await loop.create_datagram_endpoint(
                    lambda: ice.StunProtocol(self), local_addr=("0.0.0.0", port)
                )
                break
            except OSError:
                continue
        else:
            raise OSError(f"no free UDP port in {port_lo}-{port_hi}")
        protocol.local_candidate = Candidate(
            foundation=candidate_foundation("host", "udp", advertised),
            component=component,
            transport="udp",
            priority=candidate_priority(component, "host"),
            host=advertised,
            port=port,
            type="host",
        )
        self._protocols.append(protocol)
        logger.info(f"ICE host candidate {advertised}:{port}")
        return [protocol.local_candidate]

    ice.Connection.get_component_candidates = get_component_candidates


if os.environ.get("ICE_PORTS"):
    lo, hi = (int(x) for x in os.environ["ICE_PORTS"].split("-"))
    patch_aioice(lo, hi, os.environ.get("ICE_HOST", "127.0.0.1"))

# --------------------------------------------------------------------------- model caches
from faster_whisper import WhisperModel  # noqa: E402
from kokoro_onnx import Kokoro  # noqa: E402

import pipecat.services.kokoro.tts as kokoro_mod  # noqa: E402
import pipecat.services.whisper.stt as whisper_mod  # noqa: E402

_whisper_cache: dict = {}
_kokoro_cache: dict = {}


def _cached_whisper(name, device="cpu", compute_type="int8"):
    if name not in _whisper_cache:
        _whisper_cache[name] = WhisperModel(name, device="cpu", compute_type="int8", cpu_threads=int(os.environ.get("STT_THREADS", "4")))
    return _whisper_cache[name]


def _cached_kokoro(model, voices):
    if model not in _kokoro_cache:
        import onnxruntime as rt

        so = rt.SessionOptions()
        so.intra_op_num_threads = int(os.environ.get("KOKORO_THREADS", "4"))
        so.inter_op_num_threads = 1
        _kokoro_cache[model] = Kokoro.from_session(rt.InferenceSession(model, so, providers=["CPUExecutionProvider"]), voices)
    return _kokoro_cache[model]


whisper_mod.WhisperModel = _cached_whisper
kokoro_mod.Kokoro = _cached_kokoro

from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3  # noqa: E402
from pipecat.audio.vad.silero import SileroVADAnalyzer  # noqa: E402
from pipecat.audio.vad.vad_analyzer import VADParams  # noqa: E402
from pipecat.frames.frames import (  # noqa: E402
    BotStartedSpeakingFrame,
    InterruptionFrame,
    LLMRunFrame,
    LLMTextFrame,
    MetricsFrame,
    TranscriptionFrame,
    TTSAudioRawFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.metrics.metrics import TTFBMetricsData, TurnMetricsData  # noqa: E402
from pipecat.observers.base_observer import BaseObserver, FramePushed  # noqa: E402
from pipecat.pipeline.pipeline import Pipeline  # noqa: E402
from pipecat.pipeline.runner import PipelineRunner  # noqa: E402
from pipecat.pipeline.task import PipelineParams, PipelineTask  # noqa: E402
from pipecat.processors.aggregators.llm_context import LLMContext  # noqa: E402
from pipecat.processors.aggregators.llm_response_universal import (  # noqa: E402
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.processors.audio.audio_buffer_processor import AudioBufferProcessor  # noqa: E402
from pipecat.serializers.protobuf import ProtobufFrameSerializer  # noqa: E402
from pipecat.services.anthropic.llm import AnthropicLLMService  # noqa: E402
from pipecat.services.kokoro.tts import KokoroTTSService  # noqa: E402
from pipecat.services.whisper.stt import WhisperSTTService  # noqa: E402
from pipecat.transports.base_transport import TransportParams  # noqa: E402
from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection  # noqa: E402
from pipecat.transports.smallwebrtc.request_handler import (  # noqa: E402
    SmallWebRTCRequest,
    SmallWebRTCRequestHandler,
)
from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport  # noqa: E402
from pipecat.transports.websocket.fastapi import FastAPIWebsocketParams, FastAPIWebsocketTransport  # noqa: E402
from pipecat.turns.user_stop import (  # noqa: E402
    SpeechTimeoutUserTurnStopStrategy,
    TurnAnalyzerUserTurnStopStrategy,
)
from pipecat.turns.user_turn_strategies import UserTurnStrategies  # noqa: E402

from common import INTERVIEWER_VOICE, ensure_kokoro  # noqa: E402


class WhisperBeam(WhisperSTTService):
    """Pipecat's Whisper service with a configurable beam size (it uses faster-whisper's default 5)."""

    async def run_stt(self, audio: bytes):
        import numpy as np
        from pipecat.frames.frames import TranscriptionFrame as TF
        from pipecat.utils.time import time_now_iso8601

        await self.start_processing_metrics()
        a = np.frombuffer(audio, dtype=np.int16).astype(np.float32) / 32768.0

        def go():
            segs, _ = self._model.transcribe(a, language="en", beam_size=STT_BEAM)
            return [s for s in segs if s.no_speech_prob < 0.4]

        segs = await asyncio.to_thread(go)
        text = " ".join(s.text for s in segs).strip()
        await self.stop_processing_metrics()
        if text:
            yield TF(text, self._user_id, time_now_iso8601())


SYSTEM = """You are a job interviewer running a short voice mock interview. Role: Backend Engineer at Northwind
Freight (an invented company): Python and Go services, Postgres, Kafka, real time routing at scale.
Candidate: Sam (invented), six years of backend work in logistics.
Main questions, in order:
q1: Tell me about yourself.
q2: Tell me about a project you are proud of and your part in it.
q3: Tell me about a time you disagreed with a colleague.
q4: What is your weakest area for this role?
q5: Why do you want this role?
Rules: this is spoken audio, so plain sentences only, no lists or markdown. One question per turn, under 30 words.
After each answer either ask one short follow-up that refers to what the candidate actually said, or move to the
next main question. Never give feedback or hints. Open with one short greeting naming the role, then ask q1.
If the candidate says they are done, thank them in one sentence."""


# --------------------------------------------------------------------------- timing observer
class TurnTimer(BaseObserver):
    def __init__(self):
        super().__init__()
        self.seen = set()
        self.turns = []
        self.cur = None
        self.bot_first_audio_at = None
        self.t0 = time.time()

    def _first(self, key):
        if self.cur is not None and key not in self.cur:
            self.cur[key] = round(time.time() - self.t0, 3)

    async def on_push_frame(self, data: FramePushed):
        f = data.frame
        if f.id in self.seen:
            return
        self.seen.add(f.id)
        now = round(time.time() - self.t0, 3)
        if isinstance(f, UserStartedSpeakingFrame):
            if self.cur is None or "turn_end" in self.cur:
                self.cur = {"user_started": now}
        elif isinstance(f, VADUserStoppedSpeakingFrame):
            # Last moment of speech heard: VAD fires stop_secs after it.
            self.last_speech_end = round(f.timestamp - f.stop_secs - self.t0, 3)
            self.vad_stops = getattr(self, "vad_stops", 0) + 1
        elif isinstance(f, TranscriptionFrame) and self.cur is not None:
            self.cur["transcript"] = now
            self.cur.setdefault("text", "")
            self.cur["text"] += " " + f.text
        elif isinstance(f, UserStoppedSpeakingFrame) and self.cur is not None:
            self.cur["turn_end"] = now
            self.cur["speech_end_est"] = getattr(self, "last_speech_end", None)
        elif isinstance(f, InterruptionFrame):
            self.interruptions = getattr(self, "interruptions", 0) + 1
        elif isinstance(f, LLMTextFrame):
            self._first("llm_first_text")
        elif isinstance(f, TTSAudioRawFrame):
            self._first("tts_first_audio")
        elif isinstance(f, BotStartedSpeakingFrame):
            if self.bot_first_audio_at is None:
                self.bot_first_audio_at = now
            if self.cur is not None and "turn_end" in self.cur:
                self.cur["bot_started"] = now
                self.turns.append(self.cur)
                self.cur = None
        elif isinstance(f, MetricsFrame) and self.cur is not None:
            for d in f.data:
                if isinstance(d, TTFBMetricsData) and d.value:
                    self.cur.setdefault("ttfb", {})[d.processor.split("#")[0]] = round(d.value, 3)
                elif isinstance(d, TurnMetricsData):
                    self.cur["smart_turn_ms"] = round(d.e2e_processing_time_ms, 1)
                    self.cur["smart_turn_complete"] = d.is_complete


class ResourceSampler:
    def __init__(self):
        self.samples = []
        self._task = None

    async def _run(self):
        p = psutil.Process()
        psutil.cpu_percent(None)
        p.cpu_percent(None)
        while True:
            await asyncio.sleep(1.0)
            self.samples.append(
                {"sys_cpu": psutil.cpu_percent(None), "proc_cpu": p.cpu_percent(None) / os.cpu_count(),
                 "rss_mb": p.memory_info().rss / 2**20, "mem_used_mb": psutil.virtual_memory().used / 2**20}
            )

    def start(self):
        self._task = asyncio.create_task(self._run())

    def stop(self):
        if self._task:
            self._task.cancel()


def _write_wav(path: Path, pcm: bytes, sr: int, ch: int):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(ch)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm)


async def run_bot(transport, kind: str, session_id: str):
    t_start = time.time()
    stt = WhisperBeam(device="cpu", compute_type="int8", settings=WhisperSTTService.Settings(model=STT_MODEL))
    if os.environ.get("TTS_PROVIDER", "kokoro") == "piper":
        from pipecat.services.piper.tts import PiperTTSService

        tts = PiperTTSService(download_dir=MODELS / "piper",
                              settings=PiperTTSService.Settings(voice=os.environ.get("PIPER_VOICE", "en_US-lessac-medium")))
    else:
        tts = None
    model_path, voices_path = ensure_kokoro(int8=KOKORO_INT8)
    tts = tts or KokoroTTSService(
        model_path=str(model_path), voices_path=str(voices_path),
        settings=KokoroTTSService.Settings(voice=INTERVIEWER_VOICE),
        # Pipecat ends a TTS audio context after 3 s without audio; local Kokoro can take longer
        # than that for a long first sentence on this CPU, which drops/garbles the reply.
        stop_frame_timeout_s=10.0,
    )
    llm = AnthropicLLMService(
        api_key=os.environ["ANTHROPIC_API_KEY"],
        settings=AnthropicLLMService.Settings(
            model=LLM_MODEL, max_tokens=150, system_instruction=SYSTEM, enable_prompt_caching=True
        ),
    )
    if TURN == "smart":
        stop = [TurnAnalyzerUserTurnStopStrategy(turn_analyzer=LocalSmartTurnAnalyzerV3())]
    else:
        stop = [SpeechTimeoutUserTurnStopStrategy(user_speech_timeout=max(0.0, SILENCE_S - VAD_STOP_S))]
    context = LLMContext(messages=[{"role": "user", "content": "(The candidate has joined the call.)"}])
    aggs = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(
            vad_analyzer=SileroVADAnalyzer(params=VADParams(stop_secs=VAD_STOP_S)),
            user_turn_strategies=UserTurnStrategies(stop=stop),
        ),
    )
    recorder = AudioBufferProcessor(sample_rate=16000, num_channels=2)
    pipeline = Pipeline(
        [transport.input(), stt, aggs.user(), llm, tts, transport.output(), recorder, aggs.assistant()]
    )
    timer = TurnTimer()
    task = PipelineTask(
        pipeline,
        params=PipelineParams(enable_metrics=True, enable_usage_metrics=True,
                              audio_in_sample_rate=16000, audio_out_sample_rate=24000),
        observers=[timer],
    )
    sampler = ResourceSampler()
    rec = {}

    @recorder.event_handler("on_audio_data")
    async def on_audio(_, audio, sr, ch):
        rec["stereo"] = (audio, sr, ch)

    @recorder.event_handler("on_track_audio_data")
    async def on_tracks(_, user, bot, sr, ch):
        rec["tracks"] = (user, bot, sr)

    @transport.event_handler("on_client_connected")
    async def on_connected(_t, _c):
        rec["connected_at"] = time.time()
        timer.t0 = time.time()
        logger.info(f"client connected ({kind}) setup {rec['connected_at'] - t_start:.2f}s")
        sampler.start()
        await recorder.start_recording()
        await task.queue_frames([LLMRunFrame()])

    @transport.event_handler("on_client_disconnected")
    async def on_disconnected(_t, _c):
        logger.info("client disconnected")
        await recorder.stop_recording()
        await task.cancel()

    await PipelineRunner(handle_sigint=False).run(task)
    sampler.stop()

    # ---- write results
    base = OUT / f"{TAG}_{session_id[:8]}"
    if "stereo" in rec:
        audio, sr, ch = rec["stereo"]
        _write_wav(base.with_suffix(".stereo.wav"), audio, sr, ch)
    if "tracks" in rec:
        u, b, sr = rec["tracks"]
        _write_wav(Path(f"{base}.user.wav"), u, sr, 1)
        _write_wav(Path(f"{base}.bot.wav"), b, sr, 1)
    gaps = []
    for t in timer.turns:
        if "speech_end_est" in t and "bot_started" in t:
            t["reply_ms"] = round(1000 * (t["bot_started"] - t["speech_end_est"]))
            gaps.append(t["reply_ms"])
    s = sampler.samples
    summary = {
        "tag": TAG, "transport": kind, "tts": os.environ.get("TTS_PROVIDER", "kokoro"), "stt_model": STT_MODEL, "stt_beam": STT_BEAM, "turn": TURN,
        "llm": LLM_MODEL, "kokoro_int8": KOKORO_INT8,
        "connect_to_first_bot_audio_s": timer.bot_first_audio_at,
        "interruptions": getattr(timer, "interruptions", 0), "vad_stops": getattr(timer, "vad_stops", 0),
        "turns": timer.turns,
        "reply_ms": {"n": len(gaps), "median": statistics.median(gaps) if gaps else None,
                     "p90": sorted(gaps)[max(0, int(round(0.9 * len(gaps))) - 1)] if gaps else None},
        "resources": {
            "proc_cpu_avg_pct": round(statistics.mean(x["proc_cpu"] for x in s), 1) if s else None,
            "proc_cpu_max_pct": round(max(x["proc_cpu"] for x in s), 1) if s else None,
            "rss_max_mb": round(max(x["rss_mb"] for x in s)) if s else None,
            "container_mem_used_max_mb": round(max(x["mem_used_mb"] for x in s)) if s else None,
        },
    }
    (base.with_suffix(".json")).write_text(json.dumps(summary, indent=1))
    logger.info(f"result {base}.json reply_ms={summary['reply_ms']}")


# --------------------------------------------------------------------------- HTTP
app = FastAPI()
webrtc = SmallWebRTCRequestHandler()
AUDIO_PARAMS = dict(audio_in_enabled=True, audio_out_enabled=True)


@app.get("/")
async def index():
    return FileResponse(Path(__file__).parent / "client.html")


@app.get("/health")
async def health():
    return {"ok": True, "stt": STT_MODEL, "turn": TURN, "llm": LLM_MODEL}


@app.post("/api/offer")
async def offer(request: dict, background_tasks: BackgroundTasks):
    async def cb(conn: SmallWebRTCConnection):
        transport = SmallWebRTCTransport(webrtc_connection=conn, params=TransportParams(**AUDIO_PARAMS))
        background_tasks.add_task(run_bot, transport, "smallwebrtc", conn.pc_id)

    req = SmallWebRTCRequest.from_dict(request)
    return await webrtc.handle_web_request(request=req, webrtc_connection_callback=cb)


@app.patch("/api/offer")
async def offer_patch(request: dict):
    from pipecat.transports.smallwebrtc.request_handler import IceCandidate, SmallWebRTCPatchRequest

    req = SmallWebRTCPatchRequest(
        pc_id=request["pc_id"],
        candidates=[IceCandidate(**c) for c in request.get("candidates", [])],
    )
    await webrtc.handle_patch_request(req)
    return {"status": "success"}


@app.websocket("/ws")
async def ws(websocket: WebSocket):
    await websocket.accept()
    transport = FastAPIWebsocketTransport(
        websocket=websocket,
        params=FastAPIWebsocketParams(**AUDIO_PARAMS, serializer=ProtobufFrameSerializer(), allowed_origins=[]),
    )
    await run_bot(transport, "websocket", uuid.uuid4().hex)


@app.post("/browser-result")
async def browser_result(body: dict):
    (OUT / f"{TAG}_browser_{int(time.time())}.json").write_text(json.dumps(body, indent=1))
    return JSONResponse({"ok": True})


@app.on_event("startup")
async def warm():
    # Load models once so connect time excludes model load (the real service does this at startup).
    t = time.time()
    await asyncio.to_thread(_cached_whisper, STT_MODEL)
    m, v = ensure_kokoro(int8=KOKORO_INT8)
    await asyncio.to_thread(_cached_kokoro, str(m), str(v))
    logger.info(f"models warm in {time.time() - t:.1f}s")
