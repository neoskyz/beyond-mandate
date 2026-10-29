"""Agent pipeline assembly, the Anthropic LLM element, and token-cost accounting.

We build the AgentDojo pipeline by hand instead of PipelineConfig.from_config, because
agentdojo's model list predates claude-haiku-5-5 and its AnthropicLLM passes `temperature`,
which the installed anthropic SDK (2.3.0) no longer accepts and Haiku 5.5 rejects.
agentdojo's message-conversion helpers also break on this SDK (MessageParam is no longer
callable), so the conversion below re-implements agentdojo's mapping with plain dicts: same
system prompt, same tool schemas, tool errors sent as is_error tool results, consecutive
user/tool messages merged. Each assistant turn is replayed with the exact content blocks the
API returned (kept under the extra key "_raw_content"), so thinking blocks are passed back
unchanged, as Haiku 5.5 requires.
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
from agentdojo.functions_runtime import EmptyEnv, Env, Function, FunctionCall, FunctionsRuntime
from agentdojo.types import (
    ChatAssistantMessage,
    ChatMessage,
    RedactedThinkingContentBlock,
    TextContentBlock,
    ThinkingContentBlock,
    text_content_block_from_string,
)

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


def _block_to_api(block) -> dict | None:
    t = block["type"]
    if t == "text":
        return {"type": "text", "text": block["content"]} if block["content"] else None
    if t == "thinking" and block.get("id") is not None:
        return {"type": "thinking", "thinking": block["content"], "signature": block["id"]}
    if t in ("thinking", "redacted_thinking"):
        return {"type": "redacted_thinking", "data": block["content"]}
    raise ValueError(f"unsupported content block {t}")


def _message_to_api(message: ChatMessage) -> dict:
    role = message["role"]
    if role == "user":
        return {"role": "user", "content": [b for b in map(_block_to_api, message["content"]) if b]}
    if role == "tool":
        if message["error"] is not None:
            content = [{"type": "text", "text": message["error"]}]
        else:
            text = message["content"][0]["content"] if message["content"] else ""
            content = [{"type": "text", "text": text}] if text else []
        block = {"type": "tool_result", "tool_use_id": message["tool_call_id"], "content": content}
        if message["error"] is not None:
            block["is_error"] = True
        return {"role": "user", "content": [block]}
    if role == "assistant":
        raw = message.get("_raw_content")
        if raw is not None:
            return {"role": "assistant", "content": raw}
        content = [b for b in map(_block_to_api, message["content"] or []) if b]
        content += [
            {"type": "tool_use", "id": tc.id, "name": tc.function, "input": tc.args} for tc in message["tool_calls"] or []
        ]
        return {"role": "assistant", "content": content}
    raise ValueError(f"invalid role {role}")


def conversation_to_api(messages: Sequence[ChatMessage]) -> tuple[str | None, list[dict]]:
    system = None
    if messages and messages[0]["role"] == "system":
        system = messages[0]["content"][0]["content"]
        messages = messages[1:]
    out: list[dict] = []
    for m in map(_message_to_api, messages):
        if out and out[-1]["role"] == "user" and m["role"] == "user":
            out[-1]["content"] = [*out[-1]["content"], *m["content"]]
        else:
            out.append(m)
    return system, out


def function_to_api(tool: Function) -> dict:
    return {"name": tool.name, "description": tool.description, "input_schema": tool.parameters.model_json_schema()}


def response_to_assistant_message(resp) -> ChatAssistantMessage:
    content = []
    for b in resp.content:
        if b.type == "text":
            content.append(TextContentBlock(type="text", content=b.text))
        elif b.type == "thinking":
            content.append(ThinkingContentBlock(type="thinking", content=b.thinking, id=b.signature))
        elif b.type == "redacted_thinking":
            content.append(RedactedThinkingContentBlock(type="redacted_thinking", content=b.data))
    tool_calls = [  # as agentdojo: tool calls only count when the model stopped to use tools
        FunctionCall(id=b.id, function=b.name, args=dict(b.input))
        for b in resp.content
        if b.type == "tool_use" and resp.stop_reason == "tool_use"
    ]
    msg = ChatAssistantMessage(role="assistant", content=content, tool_calls=tool_calls or None)
    msg["_raw_content"] = [b.to_dict() for b in resp.content]
    return msg


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
        system_prompt, anthropic_messages = conversation_to_api(messages)
        tools = [function_to_api(tool) for tool in runtime.functions.values()]
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
            output = response_to_assistant_message(completion)
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
