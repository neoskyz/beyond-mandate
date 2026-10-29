"""Agent pipeline assembly, the Anthropic LLM element, and token-cost accounting.

We build the AgentDojo pipeline by hand instead of PipelineConfig.from_config, because
agentdojo's model list predates claude-haiku-5-5 and its AnthropicLLM passes `temperature`,
which the installed anthropic SDK (2.3.0) no longer accepts and Haiku 5.5 rejects.
Message conversion reuses agentdojo's own helpers so the agent sees exactly what the stock
AgentDojo Anthropic pipeline would send.
"""

import random
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

import anthropic
from agentdojo.agent_pipeline import AgentPipeline, InitQuery, SystemMessage, ToolsExecutionLoop
from agentdojo.agent_pipeline.agent_pipeline import load_system_message
from agentdojo.agent_pipeline.base_pipeline_element import BasePipelineElement
from agentdojo.agent_pipeline.llms.anthropic_llm import (
    _anthropic_to_assistant_message,
    _conversation_to_anthropic,
    _function_to_anthropic,
)
from agentdojo.functions_runtime import EmptyEnv, Env, FunctionsRuntime
from agentdojo.types import ChatAssistantMessage, ChatMessage, text_content_block_from_string

from mandate_bench.enforcer import Level, MandateEnforcingToolsExecutor, Policy, Reviewer
from mandate_bench.mandate import MandateGenerator, MandateResult, generate_mandate

MODEL = "claude-haiku-5-5"
AGENT_EFFORT = "medium"  # Haiku 5.5's default effort, set explicitly for reproducibility
MANDATE_EFFORT = "low"
AGENT_MAX_TOKENS = 16000
MANDATE_MAX_TOKENS = 4000

# USD per million tokens, claude-haiku-5-5, from platform.claude.com/docs/en/about-claude/pricing
# (fetched 2026-10-28). Two rate cards by prompt length.
PRICING = {
    "short": {"input": 0.10, "cache_write_5m": 0.125, "cache_write_1h": 0.20, "cache_read": 0.01, "output": 0.50},
    "long": {"input": 0.50, "cache_write_5m": 0.625, "cache_write_1h": 1.00, "cache_read": 0.05, "output": 2.50},
}
LONG_PROMPT_THRESHOLD = 100_000


def usage_cost(usage) -> tuple[float, dict]:
    """Cost of one response from its `usage` block. Returns (usd, token dict)."""
    cc = getattr(usage, "cache_creation", None)
    w1h = getattr(cc, "ephemeral_1h_input_tokens", 0) or 0 if cc else 0
    cache_write = usage.cache_creation_input_tokens or 0
    w5m = max(cache_write - w1h, 0)
    cache_read = usage.cache_read_input_tokens or 0
    prompt_tokens = usage.input_tokens + cache_write + cache_read
    card = PRICING["long" if prompt_tokens > LONG_PROMPT_THRESHOLD else "short"]
    usd = (
        usage.input_tokens * card["input"]
        + w5m * card["cache_write_5m"]
        + w1h * card["cache_write_1h"]
        + cache_read * card["cache_read"]
        + usage.output_tokens * card["output"]
    ) / 1e6
    tokens = {
        "input": usage.input_tokens,
        "cache_write": cache_write,
        "cache_read": cache_read,
        "output": usage.output_tokens,
    }
    return usd, tokens


@dataclass
class UsageMeter:
    calls: int = 0
    cost_usd: float = 0.0
    tokens: dict = field(default_factory=lambda: {"input": 0, "cache_write": 0, "cache_read": 0, "output": 0})
    refusals: int = 0
    max_tokens_stops: int = 0

    def add(self, response) -> None:
        usd, toks = usage_cost(response.usage)
        self.calls += 1
        self.cost_usd += usd
        for k, v in toks.items():
            self.tokens[k] += v
        if response.stop_reason == "refusal":
            self.refusals += 1
        if response.stop_reason == "max_tokens":
            self.max_tokens_stops += 1

    def to_dict(self) -> dict:
        return {
            "llm_calls": self.calls,
            "cost_usd": round(self.cost_usd, 6),
            "tokens": dict(self.tokens),
            "refusals": self.refusals,
            "max_tokens_stops": self.max_tokens_stops,
        }


_RETRYABLE = (anthropic.RateLimitError, anthropic.APIConnectionError, anthropic.InternalServerError)


def create_with_backoff(client: anthropic.Anthropic, max_attempts: int = 6, **kwargs):
    """messages.create with exponential backoff on 429 / 5xx / connection errors.
    (The SDK's own retries are disabled on the client so this is the single retry policy.)"""
    for attempt in range(max_attempts):
        try:
            return client.messages.create(**kwargs)
        except _RETRYABLE as e:
            if attempt == max_attempts - 1:
                raise
            retry_after = None
            resp = getattr(e, "response", None)
            if resp is not None:
                try:
                    retry_after = float(resp.headers.get("retry-after"))
                except (TypeError, ValueError):
                    retry_after = None
            delay = retry_after or min(2**attempt + random.random(), 60)
            time.sleep(delay)
        except anthropic.APIStatusError as e:
            if e.status_code in (529,) and attempt < max_attempts - 1:  # overloaded
                time.sleep(min(2**attempt + random.random(), 60))
                continue
            raise


class ClaudeLLM(BasePipelineElement):
    """Agent LLM element. Same message format as agentdojo's AnthropicLLM, no sampling params,
    adaptive thinking left at the model default (thinking blocks are passed back unchanged by
    agentdojo's converters), and every response's usage recorded in `meter`."""

    def __init__(self, client: anthropic.Anthropic, meter: UsageMeter, model: str = MODEL) -> None:
        self.client = client
        self.meter = meter
        self.model = model
        self.name = model

    def query(
        self,
        query: str,
        runtime: FunctionsRuntime,
        env: Env = EmptyEnv(),
        messages: Sequence[ChatMessage] = [],
        extra_args: dict = {},
    ) -> tuple[str, FunctionsRuntime, Env, Sequence[ChatMessage], dict]:
        system_prompt, anthropic_messages = _conversation_to_anthropic(messages)
        tools = [_function_to_anthropic(tool) for tool in runtime.functions.values()]
        kwargs = dict(
            model=self.model,
            max_tokens=AGENT_MAX_TOKENS,
            messages=anthropic_messages,
            output_config={"effort": AGENT_EFFORT},
        )
        if tools:
            kwargs["tools"] = tools
        if system_prompt:
            kwargs["system"] = system_prompt
        completion = create_with_backoff(self.client, **kwargs)
        self.meter.add(completion)
        if completion.stop_reason == "refusal":
            output = ChatAssistantMessage(
                role="assistant", content=[text_content_block_from_string("[refusal]")], tool_calls=None
            )
        else:
            output = _anthropic_to_assistant_message(completion)
        return query, runtime, env, [*messages, output], extra_args


def make_llm_mandate_fn(client: anthropic.Anthropic, meter: UsageMeter, model: str = MODEL) -> Callable[[str], MandateResult]:
    def complete(system: str, user: str) -> str:
        resp = create_with_backoff(
            client,
            model=model,
            max_tokens=MANDATE_MAX_TOKENS,
            system=system,
            messages=[{"role": "user", "content": user}],
            output_config={"effort": MANDATE_EFFORT},
        )
        meter.add(resp)
        return "".join(b.text for b in resp.content if b.type == "text")

    return lambda prompt: generate_mandate(prompt, complete)


_thread_local = threading.local()


def thread_client() -> anthropic.Anthropic:
    """One client per worker thread; reads ANTHROPIC_API_KEY from the environment."""
    if not hasattr(_thread_local, "client"):
        _thread_local.client = anthropic.Anthropic(max_retries=0, timeout=300)
    return _thread_local.client


def build_pipeline(
    level: Level,
    policy: Policy,
    llm: BasePipelineElement,
    make_mandate: Callable[[str], MandateResult],
    on_mandate: Callable[[MandateResult], None],
    known_payees: set[str],
    default_cap: float,
    reviewer: Reviewer,
    observer: Callable[[dict], None],
    max_iters: int = 15,
) -> AgentPipeline:
    executor = MandateEnforcingToolsExecutor(level, policy, known_payees, default_cap, reviewer, observer)
    pipeline = AgentPipeline(
        [
            SystemMessage(load_system_message(None)),
            InitQuery(),
            MandateGenerator(make_mandate, on_mandate),
            llm,
            ToolsExecutionLoop([executor, llm], max_iters=max_iters),
        ]
    )
    pipeline.name = f"{MODEL}-{level.value}-{policy.value}"
    return pipeline
