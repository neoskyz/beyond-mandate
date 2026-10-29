"""Offline tests for the gpt-4o-mini second run (no API calls)."""

from types import SimpleNamespace

import pytest
from agentdojo.functions_runtime import FunctionsRuntime
from agentdojo.task_suite.load_suites import get_suite
from agentdojo.types import text_content_block_from_string

from mandate_bench.openai_run import (
    MODEL,
    GPTLLM,
    OpenAIUsageMeter,
    load_recorded_mandates,
    make_attack,
    openai_usage_cost,
    replay_mandate_fn,
)
from mandate_bench.run import FatalRunError


def _usage(prompt, cached, completion):
    return SimpleNamespace(prompt_tokens=prompt, completion_tokens=completion, prompt_tokens_details=SimpleNamespace(cached_tokens=cached))


def _completion(model=MODEL, content="done", tool_calls=None, finish="stop", usage=None):
    message = SimpleNamespace(content=content, tool_calls=tool_calls, refusal=None)
    return SimpleNamespace(model=model, choices=[SimpleNamespace(message=message, finish_reason=finish)], usage=usage or _usage(1000, 0, 100))


def test_pricing():
    usd, toks = openai_usage_cost(_usage(1_000_000, 0, 1_000_000))
    assert usd == pytest.approx(0.15 + 0.60)
    usd, toks = openai_usage_cost(_usage(1_000_000, 400_000, 0))
    assert usd == pytest.approx(0.6 * 0.15 + 0.4 * 0.075)
    assert toks == {"input": 600_000, "cache_write": 0, "cache_read": 400_000, "output": 0}
    usd, _ = openai_usage_cost(SimpleNamespace(prompt_tokens=10, completion_tokens=0, prompt_tokens_details=None))
    assert usd == pytest.approx(10 * 0.15 / 1e6)


def test_meter_rejects_other_model():
    m = OpenAIUsageMeter()
    m.add(_completion())
    assert m.calls == 1
    with pytest.raises(FatalRunError):
        m.add(_completion(model="gpt-4o-mini-2024-12-01"))
    assert m.calls == 2  # the mismatched call is still costed


def test_attack_uses_agentdojo_model_name():
    suite = get_suite("v1.2.2", "banking")
    attack = make_attack(suite)
    assert attack.name == "important_instructions" and attack.model_name == "GPT-4"
    inj = attack.attack(suite.user_tasks["user_task_0"], suite.injection_tasks["injection_task_0"])
    assert "GPT-4" in inj["injection_bill_text"]


def test_replayed_mandates_cover_grid():
    mandates = load_recorded_mandates()
    assert len(mandates) == 4000
    rec = mandates[("M2-stepup", 1, "user_task_14", "injection_task_7")]
    result = replay_mandate_fn(rec)("ignored prompt")
    assert result.mandate.allowed_actions == ["update_password"] and not result.failed


def test_gptllm_converts_and_meters():
    suite = get_suite("v1.2.2", "banking")
    sent = {}

    class FakeCompletions:
        def create(self, **kw):
            sent.update(kw)
            call = SimpleNamespace(id="c1", function=SimpleNamespace(name="get_balance", arguments="{}"))
            return _completion(content=None, tool_calls=[call], finish="tool_calls")

    client = SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions()))
    meter = OpenAIUsageMeter()
    msgs = [
        {"role": "system", "content": [text_content_block_from_string("sys")]},
        {"role": "user", "content": [text_content_block_from_string("hi")]},
    ]
    _, _, _, out, _ = GPTLLM(client, meter).query("hi", FunctionsRuntime(suite.tools), messages=msgs)
    assert sent["model"] == MODEL and sent["temperature"] == 0.0 and sent["tool_choice"] == "auto"
    assert [m["role"] for m in sent["messages"]] == ["developer", "user"]
    assert out[-1]["tool_calls"][0].function == "get_balance"
    assert meter.calls == 1
