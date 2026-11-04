from iassistpro.prompt import JOIN_MESSAGE, TOOLS, build_system_prompt


def test_prompt_lists_planned_questions_in_order_with_probes(example_brief):
    p = build_system_prompt(example_brief)
    positions = [p.index(f"{q.id}: {q.text}") for q in example_brief.questions]
    assert positions == sorted(positions)
    for q in example_brief.questions:
        for probe in q.probe_for:
            assert probe in p
    short = example_brief.model_copy(update={"limits": example_brief.limits.model_copy(update={"max_main_questions": 3})})
    p3 = build_system_prompt(short)
    assert "q3: " in p3 and "q4: " not in p3 and example_brief.questions[3].text not in p3


def test_prompt_rules_present(example_brief):
    p = build_system_prompt(example_brief)
    for phrase in ["short clause", "never give feedback", "speak first, then call", "under 30 words",
                   "Backend Engineer", "Northwind Freight", "Sam", "25 minutes", "at most 2 follow-ups",
                   "never say these", "ask_question", "follow_up", "end_interview"]:
        assert phrase.lower() in p.lower(), phrase
    assert example_brief.candidate.summary in p and example_brief.role.summary in p


def test_prompt_is_deterministic(example_brief):
    assert build_system_prompt(example_brief) == build_system_prompt(example_brief.model_copy())


def test_tool_enums_are_the_planned_ids(example_brief):
    tools = {t.name: t for t in TOOLS(example_brief).standard_tools}
    ids = [q.id for q in example_brief.planned_questions]
    assert set(tools) == {"ask_question", "follow_up", "end_interview"}
    assert tools["ask_question"].properties["question_id"]["enum"] == ids
    assert tools["follow_up"].properties["question_id"]["enum"] == ids
    assert tools["end_interview"].properties["reason"]["enum"] == ["completed", "candidate_request", "time_limit", "budget"]
    assert all(t.required for t in tools.values())


def test_join_message():
    assert JOIN_MESSAGE == "(The candidate has joined the call.)"
