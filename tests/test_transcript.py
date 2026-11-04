from pipecat.frames.frames import (
    BotStartedSpeakingFrame, BotStoppedSpeakingFrame, InterruptionFrame, LLMFullResponseEndFrame,
    LLMFullResponseStartFrame, TranscriptionFrame, TTSSpeakFrame, TTSTextFrame, UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame, VADUserStartedSpeakingFrame, VADUserStoppedSpeakingFrame,
)

from iassistpro.transcript import TranscriptBuilder, TurnLabelFrame


def tts(text):
    return TTSTextFrame(text=text, text_type="sentence")


def tr(text):
    return TranscriptionFrame(text, "user", "2026-11-04T00:00:00Z")


def bot_reply(b, t, texts, label=None, end_gap=3000):
    b.on_frame(LLMFullResponseStartFrame(), t)
    for x in texts:
        b.on_frame(tts(x), t + 10)
    if label:
        b.on_frame(TurnLabelFrame(*label), t + 20)
    b.on_frame(LLMFullResponseEndFrame(), t + 30)
    b.on_frame(BotStartedSpeakingFrame(), t + 100)
    b.on_frame(BotStoppedSpeakingFrame(), t + end_gap)


def answer(b, t_start, t_vad_stop, texts):
    b.on_frame(VADUserStartedSpeakingFrame(start_secs=0.2), t_start)
    b.on_frame(UserStartedSpeakingFrame(), t_start + 10)
    for x in texts:
        b.on_frame(tr(x), t_vad_stop + 50)
    b.on_frame(VADUserStoppedSpeakingFrame(stop_secs=0.2), t_vad_stop)
    b.on_frame(UserStoppedSpeakingFrame(), t_vad_stop + 300)


def script(b):
    bot_reply(b, 1000, ["Hello Sam.", "Tell me about yourself."], label=("q1", "main"))
    answer(b, 6000, 10_000, ["I led the team.", "We shipped."])
    bot_reply(b, 13_500, ["Thanks. What was your part?"], label=("q1", "follow_up"))
    answer(b, 18_000, 21_000, ["The data model."])


def test_turns_labels_and_offsets():
    closed = []
    b = TranscriptBuilder(on_turn=closed.append)
    script(b)
    assert [(t.speaker, t.kind, t.question_id) for t in b.turns] == [
        ("interviewer", "main", "q1"), ("candidate", "main", "q1"), ("interviewer", "follow_up", "q1"),
        ("candidate", "follow_up", "q1")]
    assert b.turns[1].text == "I led the team. We shipped." and b.turns[1].end_ms == 9_800   # VAD stop at 10.0 s − 0.2
    assert b.turns[1].start_ms == 5_800 and b.turns[0].text == "Hello Sam. Tell me about yourself."
    assert (b.turns[0].start_ms, b.turns[0].end_ms) == (1100, 4000)
    assert b.reply_gaps_ms == [13_600 - 9_800]


def test_greeting_without_tool_is_greeting():
    b = TranscriptBuilder(on_turn=lambda t: None)
    bot_reply(b, 0, ["Hello Sam, ready?"])
    answer(b, 4000, 5000, ["Yes."])
    assert [(t.speaker, t.kind, t.question_id) for t in b.turns] == [("interviewer", "greeting", None), ("candidate", "greeting", None)]


def test_set_label_directly_applies_to_open_reply():
    b = TranscriptBuilder(on_turn=lambda t: None)
    b.on_frame(LLMFullResponseStartFrame(), 0)
    b.on_frame(tts("Next question."), 5)
    b.set_label("q2", "main")
    b.on_frame(LLMFullResponseEndFrame(), 10)
    b.on_frame(BotStartedSpeakingFrame(), 20)
    b.on_frame(BotStoppedSpeakingFrame(), 900)
    assert (b.turns[0].question_id, b.turns[0].kind) == ("q2", "main")


def test_reply_spanning_two_bot_speaking_spans_is_one_turn():
    b = TranscriptBuilder(on_turn=lambda t: None)
    b.on_frame(LLMFullResponseStartFrame(), 0)
    b.on_frame(tts("One."), 5)
    b.on_frame(BotStartedSpeakingFrame(), 20)
    b.on_frame(BotStoppedSpeakingFrame(), 500)          # response still streaming
    b.on_frame(tts("Two."), 600)
    b.on_frame(LLMFullResponseEndFrame(), 610)
    b.on_frame(BotStartedSpeakingFrame(), 700)
    b.on_frame(BotStoppedSpeakingFrame(), 1500)
    assert [(t.text, t.start_ms, t.end_ms) for t in b.turns] == [("One. Two.", 20, 1500)]


def test_interrupted_reply_keeps_spoken_words_only():
    b = TranscriptBuilder(on_turn=lambda t: None)
    b.on_frame(LLMFullResponseStartFrame(), 0)
    b.on_frame(tts("First sentence."), 5)
    b.on_frame(BotStartedSpeakingFrame(), 20)
    b.on_frame(InterruptionFrame(), 800)
    b.on_frame(BotStoppedSpeakingFrame(), 820)
    b.on_frame(tts("Never spoken."), 830)               # late frame of the cancelled reply
    b.on_frame(LLMFullResponseEndFrame(), 840)
    assert [t.text for t in b.turns] == ["First sentence."]


def test_reply_interrupted_before_any_audio_makes_no_turn():
    b = TranscriptBuilder(on_turn=lambda t: None)
    b.on_frame(LLMFullResponseStartFrame(), 0)
    b.on_frame(tts("Queued only."), 5)
    b.on_frame(InterruptionFrame(), 10)
    b.on_frame(TTSSpeakFrame("Thanks, Sam. We'll stop here."), 20)
    b.on_frame(TurnLabelFrame(None, "closing"), 21)
    b.on_frame(tts("Thanks, Sam. We'll stop here."), 30)
    b.on_frame(BotStartedSpeakingFrame(), 100)
    b.on_frame(BotStoppedSpeakingFrame(), 2000)
    assert [(t.text, t.kind, t.question_id) for t in b.turns] == [("Thanks, Sam. We'll stop here.", "closing", None)]


def test_blank_transcription_makes_no_turn():
    b = TranscriptBuilder(on_turn=lambda t: None)
    answer(b, 0, 1000, ["  "])
    answer(b, 2000, 3000, [])
    assert b.turns == []


def test_latency_median_and_p90():
    b = TranscriptBuilder(on_turn=lambda t: None)
    b.reply_gaps_ms = [3000, 3500, 3900, 4000, 4100, 4200, 4300, 4400, 4500, 9000]
    lat = b.latency()
    assert (lat.n, lat.median_reply_ms, lat.p90_reply_ms, lat.measured) == (10, 4150, 4500, "server")
    assert TranscriptBuilder(on_turn=lambda t: None).latency().median_reply_ms is None


def test_long_gaps_are_not_counted():
    b = TranscriptBuilder(on_turn=lambda t: None)
    answer(b, 0, 1000, ["Hi."])
    b.on_frame(BotStartedSpeakingFrame(), 1000 - 200 + 12_000)
    assert b.reply_gaps_ms == []


def test_on_turn_called_once_per_closed_turn():
    closed = []
    b = TranscriptBuilder(on_turn=closed.append)
    script(b)
    b.on_frame(BotStoppedSpeakingFrame(), 30_000)        # a stray stop closes nothing new
    assert closed == b.turns and len(closed) == 4


def test_flush_closes_open_turns_at_session_end():
    b = TranscriptBuilder(on_turn=lambda t: None)
    b.on_frame(LLMFullResponseStartFrame(), 0)
    b.on_frame(tts("Cut off by a disconnect."), 5)
    b.on_frame(BotStartedSpeakingFrame(), 20)
    b.on_frame(VADUserStartedSpeakingFrame(start_secs=0.2), 3000)
    b.flush(4000)
    assert [(t.speaker, t.end_ms) for t in b.turns] == [("interviewer", 4000)]
