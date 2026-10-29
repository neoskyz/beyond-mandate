"""Second-model run: the same grid with gpt-4o-mini-2024-07-18 as the agent.

Everything except the agent is the Haiku 5.5 setup: AgentDojo's default system prompt, tools,
pipeline order, enforcer, oracle, the stock `important_instructions` attack and the 5 configs.
The mandate is NOT regenerated: each run replays the Haiku 5.5 mandate recorded in
results/raw.jsonl for the same (config, epoch, user task, injection task), so only the agent changes.

The agent element reuses agentdojo's own OpenAI message/tool converters (they work with the
installed openai SDK 3.41.0) but calls the API itself, because agentdojo's OpenAILLM discards
the response's `usage` (needed for spend tracking) and `model` (checked against the pinned snapshot).
Sampling matches agentdojo's OpenAILLM: temperature 0, tool_choice "auto".

Usage:
    python -m mandate_bench.openai_run --configs M0 --user-tasks user_task_0 --injection-tasks injection_task_0 --smoke
    python -m mandate_bench.openai_run --epoch 1
"""

import argparse
import json
import random
import threading
import time
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import openai
from agentdojo.agent_pipeline.base_pipeline_element import BasePipelineElement
from agentdojo.agent_pipeline.llms.openai_llm import _function_to_openai, _message_to_openai, _openai_to_assistant_message
from agentdojo.attacks.important_instructions_attacks import ImportantInstructionsAttack
from agentdojo.functions_runtime import EmptyEnv, Env, FunctionsRuntime
from agentdojo.task_suite.load_suites import get_suite
from agentdojo.types import ChatAssistantMessage, ChatMessage, text_content_block_from_string

from mandate_bench.mandate import Mandate, MandateResult
from mandate_bench.pipeline import UsageMeter
from mandate_bench.run import (
    BUDGET_STOP_USD,
    PRIORITY,
    RAW as HAIKU_RAW,
    RESULTS,
    ROOT,
    SUITE_NAME,
    SUITE_VERSION,
    FatalRunError,
    SpendTracker,
    read_records,
    run_with_retries,
    total_recorded_spend,
)

MODEL = "gpt-4o-mini-2024-07-18"  # pinned snapshot (shutdown 2027-01-14 per OpenAI's deprecations page)
TEMPERATURE = 0.0
OUT = RESULTS / "gpt4omini"
RAW = OUT / "raw.jsonl"
SMOKE = OUT / "smoke.jsonl"
TRACES = OUT / "traces"
PROGRESS = ROOT / "writeup" / "PROGRESS_gpt4omini.md"
RUN_CAP_USD = 50.0  # cap on this run's own spend (results/gpt4omini/*.jsonl); the global stop also applies

# USD per million tokens, gpt-4o-mini, from developers.openai.com/api/docs/pricing (fetched 2026-10-29).
# Cached prompt tokens are a subset of prompt_tokens and are billed at the cached rate.
PRICING = {"input": 0.15, "cached_input": 0.075, "output": 0.60}


def openai_usage_cost(usage) -> tuple[float, dict]:
    """Cost of one chat completion from its `usage`. Token dict uses the same keys as the Haiku
    records: input = uncached prompt tokens, cache_read = cached prompt tokens."""
    details = getattr(usage, "prompt_tokens_details", None)
    cached = (getattr(details, "cached_tokens", 0) or 0) if details else 0
    uncached = usage.prompt_tokens - cached
    usd = (uncached * PRICING["input"] + cached * PRICING["cached_input"] + usage.completion_tokens * PRICING["output"]) / 1e6
    return usd, {"input": uncached, "cache_write": 0, "cache_read": cached, "output": usage.completion_tokens}


class OpenAIUsageMeter(UsageMeter):
    """UsageMeter for chat completions. Raises FatalRunError (after counting the cost) if the API
    served a model other than the pinned snapshot."""

    def add(self, response) -> None:
        usd, toks = openai_usage_cost(response.usage)
        self.calls += 1
        self.cost_usd += usd
        for k, v in toks.items():
            self.tokens[k] += v
        choice = response.choices[0]
        if getattr(choice.message, "refusal", None):
            self.refusals += 1
        if choice.finish_reason == "length":
            self.max_tokens_stops += 1
        if response.model != MODEL:
            raise FatalRunError(f"API served model {response.model!r}, expected {MODEL!r}")


RATE_LIMIT_HITS = 0
_rl_lock = threading.Lock()
_RETRYABLE = (openai.RateLimitError, openai.APIConnectionError, openai.InternalServerError)


def create_with_backoff(client: openai.OpenAI, max_attempts: int = 6, **kwargs):
    """chat.completions.create with exponential backoff on 429 / 5xx / connection errors
    (the client's own retries are disabled, so this is the single retry policy)."""
    global RATE_LIMIT_HITS
    for attempt in range(max_attempts):
        try:
            return client.chat.completions.create(**kwargs)
        except _RETRYABLE as e:
            if isinstance(e, openai.RateLimitError):
                with _rl_lock:
                    RATE_LIMIT_HITS += 1
            if attempt == max_attempts - 1:
                raise
            retry_after = None
            resp = getattr(e, "response", None)
            if resp is not None:
                try:
                    retry_after = float(resp.headers.get("retry-after"))
                except (TypeError, ValueError):
                    retry_after = None
            time.sleep(retry_after or min(2**attempt + random.random(), 60))


class GPTLLM(BasePipelineElement):
    """Agent element: agentdojo's OpenAI converters, temperature 0, usage recorded in `meter`."""

    def __init__(self, client: openai.OpenAI, meter: UsageMeter, model: str = MODEL) -> None:
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
        oai_messages = [_message_to_openai(m, self.model) for m in messages]
        tools = [_function_to_openai(t) for t in runtime.functions.values()]
        kwargs = dict(model=self.model, messages=oai_messages, temperature=TEMPERATURE)
        if tools:
            kwargs.update(tools=tools, tool_choice="auto")
        completion = create_with_backoff(self.client, **kwargs)
        self.meter.add(completion)
        message = completion.choices[0].message
        if getattr(message, "refusal", None) and not message.tool_calls:
            output = ChatAssistantMessage(role="assistant", content=[text_content_block_from_string("[refusal]")], tool_calls=None)
        else:
            output = _openai_to_assistant_message(message)
        return query, runtime, env, [*messages, output], extra_args


_thread_local = threading.local()


def thread_client() -> openai.OpenAI:
    """One client per worker thread; reads OPENAI_API_KEY from the environment."""
    if not hasattr(_thread_local, "client"):
        _thread_local.client = openai.OpenAI(max_retries=0, timeout=120)
    return _thread_local.client


def load_recorded_mandates(path: Path = HAIKU_RAW) -> dict[tuple, dict]:
    """(config, epoch, user_task, injection_task) -> the Haiku 5.5 run record holding its mandate."""
    return {
        (r["config"], r.get("epoch", 1), r["user_task"], r["injection_task"]): r
        for r in read_records(path)
        if r.get("status") == "ok"
    }


def replay_mandate_fn(rec: dict):
    result = MandateResult(
        mandate=Mandate(**rec["mandate"]),
        raw_responses=rec.get("mandate_raw") or [],
        attempts=rec.get("mandate_attempts") or 1,
        failed=bool(rec.get("mandate_failed")),
    )
    return lambda prompt: result


class _Named:
    """Stands in for the target pipeline so agentdojo's stock attack picks the model's prose name."""

    name = MODEL


def make_attack(suite) -> ImportantInstructionsAttack:
    # Stock attack, no workaround needed: agentdojo's MODEL_NAMES maps this snapshot to "GPT-4".
    return ImportantInstructionsAttack(suite, _Named())


def run_spend() -> float:
    return sum(r.get("cost_usd_all_attempts", r.get("cost_usd", 0.0)) for p in sorted(OUT.glob("*.jsonl")) for r in read_records(p))


class DualSpendTracker(SpendTracker):
    """Global cumulative stop (all providers) plus a cap on this run's own spend."""

    def __init__(self, global_initial: float, global_stop: float, run_initial: float, run_cap: float) -> None:
        super().__init__(global_initial, global_stop)
        self.run_spent = run_initial
        self.run_cap = run_cap

    def add(self, usd: float) -> float:
        with self.lock:
            self.spent += usd
            self.run_spent += usd
            return self.spent

    def exhausted(self) -> bool:
        with self.lock:
            return self.spent >= self.stop_usd or self.run_spent >= self.run_cap


def log_progress(line: str) -> None:
    if not PROGRESS.exists():
        PROGRESS.write_text("# Progress log: gpt-4o-mini-2024-07-18 second run\n\n")
    with PROGRESS.open("a") as f:
        f.write(f"- {datetime.now():%Y-%m-%d %H:%M} {line}\n")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--configs", nargs="*", default=PRIORITY)
    ap.add_argument("--user-tasks", nargs="*", default=None)
    ap.add_argument("--injection-tasks", nargs="*", default=None)
    ap.add_argument("--no-attack-only", action="store_true")
    ap.add_argument("--attack-only", action="store_true")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--epoch", type=int, default=1)
    ap.add_argument("--stop-usd", type=float, default=BUDGET_STOP_USD, help="global cumulative stop (all runs, all providers)")
    ap.add_argument("--run-cap-usd", type=float, default=RUN_CAP_USD, help="cap on this run's spend (results/gpt4omini/*.jsonl)")
    ap.add_argument("--smoke", action="store_true", help="write to results/gpt4omini/smoke.jsonl")
    args = ap.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    out_path = SMOKE if args.smoke else RAW
    suite = get_suite(SUITE_VERSION, SUITE_NAME)
    attack = make_attack(suite)
    for ut in suite.user_tasks.values():  # warm the lru_cache before threads share it
        attack.get_injection_candidates(ut)
    mandates = load_recorded_mandates()
    user_tasks = args.user_tasks or list(suite.user_tasks)
    injection_tasks = args.injection_tasks or list(suite.injection_tasks)

    done = {
        (r["config"], r["user_task"], r["injection_task"])
        for r in read_records(out_path)
        if r.get("status") == "ok" and r.get("epoch", 1) == args.epoch
    }
    spend = DualSpendTracker(total_recorded_spend(), args.stop_usd, run_spend(), args.run_cap_usd)
    print(f"global spend so far ${spend.spent:.4f} (stop ${args.stop_usd}); this run ${spend.run_spent:.4f} (cap ${args.run_cap_usd})")
    write_lock = threading.Lock()
    fatal = threading.Event()

    for config in args.configs:
        jobs = []
        if not args.attack_only:
            jobs += [(ut, None) for ut in user_tasks]
        if not args.no_attack_only:
            jobs += [(ut, it) for ut in user_tasks for it in injection_tasks]
        jobs = [j for j in jobs if (config, j[0], j[1] or "") not in done]
        missing = [j for j in jobs if (config, args.epoch, j[0], j[1] or "") not in mandates]
        if missing:
            raise SystemExit(f"no recorded Haiku mandate for {config} epoch {args.epoch}: {missing[:3]}")
        if not jobs:
            print(f"[{config}] nothing to do")
            continue
        if spend.exhausted():
            log_progress(f"[{config}] SKIPPED: global ${spend.spent:.2f} / run ${spend.run_spent:.2f} at a stop")
            break
        log_progress(f"[{config}] epoch {args.epoch} start: {len(jobs)} runs; global spend ${spend.spent:.4f}, this run ${spend.run_spent:.4f}")
        n_done = n_err = 0

        def work(job):
            if spend.exhausted() or fatal.is_set():
                return None
            rec_m = mandates[(config, args.epoch, job[0], job[1] or "")]

            def setup(meter):
                return {
                    "llm": GPTLLM(thread_client(), meter),
                    "make_mandate": replay_mandate_fn(rec_m),
                    "model": MODEL,
                    "trace_root": TRACES,
                }

            try:
                rec = run_with_retries(config, job[0], job[1], suite, attack, spend, args.epoch, meter_cls=OpenAIUsageMeter, setup=setup)
            except FatalRunError:
                fatal.set()
                raise
            rec["agent_temperature"] = TEMPERATURE
            rec["mandate_source"] = "replayed from results/raw.jsonl (claude-haiku-5-5 mandate generator)"
            rec["attack_model_name"] = attack.model_name
            return rec

        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = [pool.submit(work, j) for j in jobs]
            for fut in as_completed(futures):
                try:
                    rec = fut.result()
                except FatalRunError as e:
                    log_progress(f"FATAL: {e}. Stopping all runs.")
                    raise SystemExit(f"FATAL: {e}")
                if rec is None:
                    continue
                with write_lock, out_path.open("a") as f:
                    f.write(json.dumps(rec, default=str) + "\n")
                n_done += 1
                n_err += rec["status"] != "ok"
                print(
                    f"[{config}] {n_done}/{len(jobs)} {rec['user_task']} {rec['injection_task'] or '-'} "
                    f"status={rec['status']} util={rec.get('utility')} sec={rec.get('security')} "
                    f"stepups={rec.get('n_stepups')} cost=${rec.get('cost_usd_all_attempts', 0):.4f} "
                    f"run=${spend.run_spent:.4f} global=${spend.spent:.4f} 429s={RATE_LIMIT_HITS}",
                    flush=True,
                )
                if n_done % 40 == 0:
                    log_progress(f"[{config}] {n_done}/{len(jobs)} runs; this run ${spend.run_spent:.4f}, global ${spend.spent:.4f}; 429s so far {RATE_LIMIT_HITS}")
        log_progress(
            f"[{config}] epoch {args.epoch} finished: {n_done} runs written ({n_err} errors); "
            f"this run ${spend.run_spent:.4f}, global ${spend.spent:.4f}; 429s so far {RATE_LIMIT_HITS}"
        )
        if spend.exhausted():
            log_progress(f"Budget stop reached (global ${spend.spent:.2f} / run ${spend.run_spent:.2f}); stopping paid runs.")
            break


if __name__ == "__main__":
    main()
