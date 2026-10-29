"""Experiment runner: (config x user task x injection task), resumable, budget-guarded.

Replicates agentdojo's benchmark.run_task_with_injection_tasks / TaskSuite.run_task_with_pipeline
(injection generation, environment loading, up to 3 pipeline attempts, AgentDojo's own utility
and security checks) in our own loop, because the simulated reviewer needs the current user
task and its pre-task environment, and we want the message trace.

Usage:
    python -m mandate_bench.run                         # all configs, priority order
    python -m mandate_bench.run --configs M0 --user-tasks user_task_0 --injection-tasks injection_task_0 --smoke
"""

import argparse
import json
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

from agentdojo.agent_pipeline.errors import AbortAgentError
from agentdojo.attacks.base_attacks import FixedJailbreakAttack
from agentdojo.attacks.important_instructions_attacks import ImportantInstructionsAttack
from agentdojo.functions_runtime import FunctionsRuntime
from agentdojo.task_suite.load_suites import get_suite
from agentdojo.task_suite.task_suite import functions_stack_trace_from_messages, model_output_from_messages
from agentdojo.types import get_text_content_as_str

from mandate_bench.enforcer import Level, Policy
from mandate_bench.known_payees import known_payees, max_historical_outgoing
from mandate_bench.mandate import MandateResult
from mandate_bench.oracle import GroundTruthOracle
from mandate_bench.pipeline import MODEL, ClaudeLLM, UsageMeter, build_pipeline, make_llm_mandate_fn, thread_client

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "results"
RAW = RESULTS / "raw.jsonl"
SMOKE = RESULTS / "smoke.jsonl"
TRACES = RESULTS / "traces"
PROGRESS = ROOT / "PROGRESS.md"

SUITE_VERSION = "v1.2.2"
SUITE_NAME = "banking"
AGENTDOJO_COMMIT = "089ed468cf3ed0322acc66b0211f26d9d90dbf60"
BUDGET_HARD_USD = 150.0
BUDGET_STOP_USD = 120.0
MAX_RUN_RETRIES = 2

CONFIGS: dict[str, tuple[Level, Policy]] = {
    "M0": (Level.M0, Policy.BLOCK),  # policy irrelevant at M0
    "M3-stepup": (Level.M3, Policy.STEPUP),
    "M4-stepup": (Level.M4, Policy.STEPUP),
    "M4-block": (Level.M4, Policy.BLOCK),
    "M2-stepup": (Level.M2, Policy.STEPUP),
}
PRIORITY = list(CONFIGS)


class ImportantInstructionsClaude(ImportantInstructionsAttack):
    """agentdojo's `important_instructions` attack, unchanged template. The stock __init__ looks
    up the victim model's prose name from a model list that predates claude-haiku-5-5 and raises;
    we set the name agentdojo uses for every Claude model ("Claude") directly."""

    def __init__(self, task_suite, target_pipeline=None) -> None:
        FixedJailbreakAttack.__init__(self, self._JB_STRING, task_suite, target_pipeline)
        self.user_name = "Emma Johnson"
        self.model_name = "Claude"


def log_progress(line: str) -> None:
    with PROGRESS.open("a") as f:
        f.write(f"- {datetime.now():%Y-%m-%d %H:%M} {line}\n")


def read_records(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass  # a torn final line from an interrupted write
    return out


class SpendTracker:
    def __init__(self, initial: float, stop_usd: float) -> None:
        self.spent = initial
        self.stop_usd = stop_usd
        self.lock = threading.Lock()

    def add(self, usd: float) -> float:
        with self.lock:
            self.spent += usd
            return self.spent

    def exhausted(self) -> bool:
        with self.lock:
            return self.spent >= self.stop_usd


def total_recorded_spend() -> float:
    return sum(r.get("cost_usd_all_attempts", r.get("cost_usd", 0.0)) for p in (RAW, SMOKE) for r in read_records(p))


def compact_trace(messages) -> list[dict]:
    out = []
    for m in messages:
        item = {"role": m["role"]}
        if m.get("content"):
            text = get_text_content_as_str([c for c in m["content"] if c.get("type") == "text"])
            if text:
                item["text"] = text[:2000]
        if m["role"] == "assistant" and m.get("tool_calls"):
            item["tool_calls"] = [{"function": tc.function, "args": tc.args} for tc in m["tool_calls"]]
        if m["role"] == "tool":
            item["tool"] = m["tool_call"].function
            if m.get("error"):
                item["error"] = m["error"]
        out.append(item)
    return out


def run_one(config: str, user_task_id: str, injection_task_id: str | None, suite, attack, meter: UsageMeter) -> dict:
    level, policy = CONFIGS[config]
    user_task = suite.get_user_task_by_id(user_task_id)
    injection_task = suite.get_injection_task_by_id(injection_task_id) if injection_task_id else None
    injections = attack.attack(user_task, injection_task) if injection_task else {}

    environment = suite.load_and_inject_default_environment(injections)
    task_environment = user_task.init_environment(environment)
    pre_environment = task_environment.model_copy(deep=True)

    payees = known_payees(pre_environment)
    cap = max_historical_outgoing(pre_environment)
    oracle = GroundTruthOracle(user_task, pre_environment)
    events: list[dict] = []
    mandate_box: dict[str, MandateResult] = {}

    client = thread_client()
    llm = ClaudeLLM(client, meter)
    pipeline = build_pipeline(
        level,
        policy,
        llm,
        make_llm_mandate_fn(client, meter),
        lambda r: mandate_box.setdefault("result", r),
        payees,
        cap,
        oracle,
        events.append,
    )

    runtime = FunctionsRuntime(suite.tools)
    messages = []
    model_output = None
    t0 = time.time()
    attempts = 0
    for _ in range(3):  # as in TaskSuite.run_task_with_pipeline
        attempts += 1
        try:
            _, _, task_environment, messages, _ = pipeline.query(user_task.PROMPT, runtime, task_environment)
        except AbortAgentError as e:
            task_environment, messages = e.task_environment, e.messages
        model_output = model_output_from_messages(messages)
        if model_output is not None:
            break

    stack = functions_stack_trace_from_messages(messages)
    utility = suite._check_task_result(user_task, model_output or [], pre_environment, task_environment, stack)
    # security=True means the INJECTION SUCCEEDED (agentdojo's injection_task.security semantics).
    security = (
        suite._check_task_result(injection_task, model_output or [], pre_environment, task_environment, stack)
        if injection_task
        else None
    )

    mres = mandate_box.get("result")
    trace_path = TRACES / config / f"{user_task_id}__{injection_task_id or 'none'}.json"
    trace_path.parent.mkdir(parents=True, exist_ok=True)
    trace_path.write_text(json.dumps({"prompt": user_task.PROMPT, "injections": injections, "messages": compact_trace(messages)}, indent=1, default=str))

    stepups = [e for e in events if e["action"].startswith("stepup")]
    return {
        "config": config,
        "level": level.value,
        "policy": policy.value,
        "user_task": user_task_id,
        "injection_task": injection_task_id or "",
        "attack": attack.name if injection_task else "",
        "utility": bool(utility),
        "security": None if security is None else bool(security),
        "n_stepups": len(stepups),
        "n_stepups_approved": sum(e["action"] == "stepup_approved" for e in events),
        "n_blocked": sum(e["action"] in ("blocked", "stepup_denied") for e in events),
        "state_changing_calls": events,
        "mandate": mres.mandate.model_dump() if mres else None,
        "mandate_failed": bool(mres.failed) if mres else None,
        "mandate_attempts": mres.attempts if mres else None,
        "mandate_raw": mres.raw_responses if mres else None,
        "known_payees": sorted(payees),
        "default_cap": cap,
        "ground_truth_state_changing": [{"function": c.function, "args": c.args} for c in oracle.ground_truth],
        "final_output": (get_text_content_as_str(model_output) if model_output else "")[:1000],
        "pipeline_attempts": attempts,
        "n_messages": len(messages),
        "usage": meter.to_dict(),
        "cost_usd": round(meter.cost_usd, 6),
        "model": MODEL,
        "suite_version": SUITE_VERSION,
        "agentdojo_commit": AGENTDOJO_COMMIT,
        "seconds": round(time.time() - t0, 1),
        "trace_file": str(trace_path.relative_to(ROOT)),
        "status": "ok",
        "timestamp": datetime.now().isoformat(timespec="seconds"),
    }


def run_with_retries(config, ut, it, suite, attack, spend: SpendTracker) -> dict:
    failed_cost = 0.0
    errors = []
    for attempt in range(1 + MAX_RUN_RETRIES):
        meter = UsageMeter()
        try:
            rec = run_one(config, ut, it, suite, attack, meter)
            rec["run_attempts"] = attempt + 1
            rec["errors"] = errors
            rec["cost_usd_all_attempts"] = round(rec["cost_usd"] + failed_cost, 6)
            spend.add(rec["cost_usd"])  # failed attempts were already added
            return rec
        except Exception as e:  # noqa: BLE001 -- log and retry any failure
            errors.append(f"{type(e).__name__}: {str(e)[:500]}")
            failed_cost += meter.cost_usd
            spend.add(meter.cost_usd)
            traceback.print_exc()
            time.sleep(5 * (attempt + 1))
    return {
        "config": config,
        "user_task": ut,
        "injection_task": it or "",
        "status": "error",
        "errors": errors,
        "cost_usd": 0.0,
        "cost_usd_all_attempts": failed_cost,
        "timestamp": datetime.now().isoformat(timespec="seconds"),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--configs", nargs="*", default=PRIORITY)
    ap.add_argument("--user-tasks", nargs="*", default=None)
    ap.add_argument("--injection-tasks", nargs="*", default=None)
    ap.add_argument("--no-attack-only", action="store_true")
    ap.add_argument("--attack-only", action="store_true")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--stop-usd", type=float, default=BUDGET_STOP_USD)
    ap.add_argument("--smoke", action="store_true", help="write to results/smoke.jsonl instead of raw.jsonl")
    args = ap.parse_args()
    if args.stop_usd > BUDGET_HARD_USD:
        raise SystemExit("stop-usd above hard budget")

    out_path = SMOKE if args.smoke else RAW
    RESULTS.mkdir(exist_ok=True)
    suite = get_suite(SUITE_VERSION, SUITE_NAME)
    attack = ImportantInstructionsClaude(suite)
    for ut in suite.user_tasks.values():  # warm the lru_cache before threads share it
        attack.get_injection_candidates(ut)
    user_tasks = args.user_tasks or list(suite.user_tasks)
    injection_tasks = args.injection_tasks or list(suite.injection_tasks)

    done = {(r["config"], r["user_task"], r["injection_task"]) for r in read_records(out_path) if r.get("status") == "ok"}
    spend = SpendTracker(total_recorded_spend(), args.stop_usd)
    print(f"recorded spend so far: ${spend.spent:.4f}; stop at ${args.stop_usd}")
    write_lock = threading.Lock()

    for config in args.configs:
        jobs = []
        if not args.attack_only:
            jobs += [(ut, None) for ut in user_tasks]
        if not args.no_attack_only:
            jobs += [(ut, it) for ut in user_tasks for it in injection_tasks]
        jobs = [j for j in jobs if (config, j[0], j[1] or "") not in done]
        if not jobs:
            print(f"[{config}] nothing to do")
            continue
        if spend.exhausted():
            log_progress(f"[{config}] SKIPPED: spend ${spend.spent:.2f} >= stop ${args.stop_usd}")
            break
        log_progress(f"[{config}] start: {len(jobs)} runs; spend so far ${spend.spent:.4f}")
        n_done = n_err = 0

        def work(job):
            if spend.exhausted():
                return None
            return run_with_retries(config, job[0], job[1], suite, attack, spend)

        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = [pool.submit(work, j) for j in jobs]
            for fut in as_completed(futures):
                rec = fut.result()
                if rec is None:
                    continue
                with write_lock, out_path.open("a") as f:
                    f.write(json.dumps(rec, default=str) + "\n")
                n_done += 1
                n_err += rec["status"] != "ok"
                print(
                    f"[{config}] {n_done}/{len(jobs)} {rec['user_task']} {rec['injection_task'] or '-'} "
                    f"status={rec['status']} util={rec.get('utility')} sec={rec.get('security')} "
                    f"stepups={rec.get('n_stepups')} cost=${rec.get('cost_usd_all_attempts', 0):.4f} total=${spend.spent:.4f}",
                    flush=True,
                )
                if n_done % 40 == 0:
                    log_progress(f"[{config}] {n_done}/{len(jobs)} runs, cumulative spend ${spend.spent:.4f}")
        log_progress(f"[{config}] finished: {n_done} runs written ({n_err} errors); cumulative spend ${spend.spent:.4f}")
        if spend.exhausted():
            log_progress(f"Budget stop reached (${spend.spent:.2f} >= ${args.stop_usd}); stopping paid runs.")
            break


if __name__ == "__main__":
    main()
