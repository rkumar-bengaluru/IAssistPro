import copy
import json
from pathlib import Path

import jsonschema
import pytest
from pydantic import ValidationError

from iassistpro.contract import Brief, Prices, Result, Usage, export_schemas, llm_cost_usd

CONTRACTS = Path(__file__).resolve().parents[1] / "contracts"


def example_brief() -> dict:
    return json.loads((CONTRACTS / "examples" / "brief.example.json").read_text())


def test_example_brief_and_result_validate_with_pydantic_and_jsonschema():
    for name, model in [("brief", Brief), ("result", Result)]:
        data = json.loads((CONTRACTS / "examples" / f"{name}.example.json").read_text())
        model.model_validate(data)
        jsonschema.validate(data, json.loads((CONTRACTS / f"{name}.v1.schema.json").read_text()))


def test_committed_schemas_match_the_models():
    for fname, schema in export_schemas().items():
        assert json.loads((CONTRACTS / fname).read_text()) == schema


def test_brief_schema_states_the_contact_rule():
    assert "contact details" in export_schemas()["brief.v1.schema.json"]["description"]


@pytest.mark.parametrize("mutate, field", [
    (lambda b: b.update(version=2), "version"),
    (lambda b: b.update(questions=[]), "questions"),
    (lambda b: b.update(questions=b["questions"] * 4), "questions"),          # 24 > 20 and duplicate ids
    (lambda b: b.update(questions=b["questions"] + [b["questions"][0]]), "questions"),  # duplicate id only
    (lambda b: b["limits"].update(max_minutes=90), "max_minutes"),
    (lambda b: b["limits"].update(max_llm_cost_usd=5), "max_llm_cost_usd"),
    (lambda b: b["questions"][0].update(text="x" * 501), "text"),
    (lambda b: b.update(extra="no"), "extra"),
])
def test_brief_validation_rejects(mutate, field):
    b = example_brief()
    mutate(b)
    with pytest.raises(ValidationError) as e:
        Brief.model_validate(b)
    assert field in str(e.value)


@pytest.mark.parametrize("text", ["write to sam@example.org", "see https://example.org", "call +44 20 7946 0958", "www.example.org"])
def test_brief_rejects_contact_details(text):
    b = example_brief()
    b["candidate"]["summary"] = text
    with pytest.raises(ValidationError) as e:
        Brief.model_validate(b)
    assert "contact details" in str(e.value) and "candidate.summary" in str(e.value)


def test_contact_guard_covers_question_lists():
    b = example_brief()
    b["questions"][1]["probe_for"] = ["mentions sam@example.org"]
    with pytest.raises(ValidationError) as e:
        Brief.model_validate(b)
    assert "questions.1.probe_for.0" in str(e.value)


def test_year_ranges_are_not_phone_numbers():
    b = example_brief()
    b["role"]["summary"] = "2019-2023, then 2023 - 2026"
    Brief.model_validate(b)


def test_candidate_name_refuses_digits_and_separators():
    for bad in ["Sam1", "sam@x", "a/b", "x:y"]:
        b = example_brief()
        b["candidate"]["name"] = bad
        with pytest.raises(ValidationError):
            Brief.model_validate(b)


def test_own_question_kind_is_accepted():
    b = example_brief()
    b["questions"][0]["kind"] = "own"
    Brief.model_validate(b)


def test_planned_questions_respects_max_main_questions():
    b = copy.deepcopy(example_brief())
    b["limits"]["max_main_questions"] = 2
    assert [q.id for q in Brief.model_validate(b).planned_questions] == ["q1", "q2"]


def test_llm_cost_uses_all_four_prices():
    u = [Usage(provider="anthropic", model="m", input_tokens=1_000_000, cached_input_tokens=2_000_000,
               cache_write_tokens=1_000_000, output_tokens=1_000_000),
         Usage(kind="tts", provider="cartesia", model="v", characters=500)]
    assert llm_cost_usd(u, Prices(input=0.10, cached_input=0.01, cache_write=0.125, output=0.50)) == pytest.approx(0.745)
