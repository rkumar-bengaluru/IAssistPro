"""Scripted interviewer LLM (LLM_PROVIDER=fake): no network, no cost. Used by the pipeline test and the free e2e.

It drives the real tool path (run_function_calls) and reports usage like Anthropic does, so the director,
limits, transcript labels and per-turn usage are exercised end to end.
"""

import uuid

from pipecat.frames.frames import (
    FunctionCallFromLLM, LLMContextFrame, LLMFullResponseEndFrame, LLMFullResponseStartFrame, LLMTextFrame,
)
from pipecat.metrics.metrics import LLMTokenUsage
from pipecat.services.llm_service import LLMService
from pipecat.services.settings import LLMSettings

from .contract import Brief
from .limits import NOTE_PREFIX
from .prompt import JOIN_MESSAGE

FAKE_MODEL = "fake-interviewer"


def _texts(content) -> list[str]:
    if isinstance(content, str):
        return [content]
    if isinstance(content, list):
        return [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
    return []


class FakeInterviewerLLM(LLMService):
    def __init__(self, brief: Brief, *, tool_first_no_text: bool = False, **kwargs):
        super().__init__(settings=LLMSettings(
            model=FAKE_MODEL, system_instruction=None, temperature=None, max_tokens=None, top_p=None, top_k=None,
            frequency_penalty=None, presence_penalty=None, seed=None, filter_incomplete_user_turns=False,
            user_turn_completion_config=None), **kwargs)
        self.brief = brief
        self.tool_first_no_text = tool_first_no_text
        self.runs = 0
        self._asked: list[str] = []
        self._followed_up: set[str] = set()
        self._pending_text: str | None = None
        self._closed = False

    def can_generate_metrics(self) -> bool:
        return True

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if isinstance(frame, LLMContextFrame):
            await self._respond(frame.context)
        else:
            await self.push_frame(frame, direction)

    # ---- script
    def _candidate_turns_and_notes(self, context) -> tuple[int, list[str]]:
        k, notes = 0, []
        for m in context.get_messages():
            if not isinstance(m, dict) or m.get("role") != "user":
                continue
            for t in _texts(m.get("content")):
                if t.startswith(NOTE_PREFIX):
                    notes.append(t)
                elif t and t != JOIN_MESSAGE:
                    k += 1
        return k, notes

    def _last_is_tool_result(self, context) -> bool:
        msgs = context.get_messages()
        return bool(msgs) and isinstance(msgs[-1], dict) and msgs[-1].get("role") == "tool"

    def _script_step(self, context) -> tuple[str, str | None, dict]:
        b, name = self.brief, self.brief.candidate.name
        planned = b.planned_questions
        k, notes = self._candidate_turns_and_notes(context)
        closing = f"Thank you, {name}. That's the end of the interview."
        if any("Time is up" in n for n in notes):
            return closing, "end_interview", {"reason": "time_limit"}
        if any("Close the interview" in n for n in notes):
            return closing, "end_interview", {"reason": "budget"}
        if k == 0 or not self._asked:
            q = planned[0]
            text = (f"Hello {name}, thanks for joining. This is a mock interview for the {b.role.title} role at "
                    f"{b.role.company}, about {b.limits.max_minutes} minutes. {q.text}")
            return text, "ask_question", {"question_id": q.id}
        current = self._asked[-1]
        if len(planned) > 1 and current == planned[1].id and current not in self._followed_up:
            return "Thanks. Can you say more about your own part in that?", "follow_up", {"question_id": current}
        ids = [q.id for q in planned]
        idx = ids.index(current)
        if idx + 1 < len(planned):
            q = planned[idx + 1]
            return f"Thanks. {q.text}", "ask_question", {"question_id": q.id}
        return closing, "end_interview", {"reason": "completed"}

    async def _respond(self, context) -> None:
        self.runs += 1
        if self._pending_text is not None and self._last_is_tool_result(context):
            text, tool, args = self._pending_text, None, {}  # the re-run after a tool call that spoke nothing
            self._pending_text = None
        else:
            text, tool, args = self._script_step(context)
            if tool == "ask_question":
                self._asked.append(args["question_id"])
            elif tool == "follow_up":
                self._followed_up.add(args["question_id"])
            if self.tool_first_no_text and self.runs == 1:
                self._pending_text, text = text, ""
        await self.push_frame(LLMFullResponseStartFrame())
        for sentence in _sentences(text):
            await self.push_frame(LLMTextFrame(sentence))
        if tool:
            await self.run_function_calls([FunctionCallFromLLM(function_name=tool, tool_call_id=uuid.uuid4().hex,
                                                               arguments=args, context=context)])
        await self.push_frame(LLMFullResponseEndFrame())
        first = self.runs == 1
        await self.start_llm_usage_metrics(LLMTokenUsage(
            prompt_tokens=200, completion_tokens=40, total_tokens=1240,
            cache_read_input_tokens=0 if first else 1000, cache_creation_input_tokens=1000 if first else 0))


def _sentences(text: str) -> list[str]:
    out, cur = [], ""
    for ch in text:
        cur += ch
        if ch in ".?!":
            out.append(cur)
            cur = ""
    if cur.strip():
        out.append(cur)
    return out
