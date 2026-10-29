# Mandate granularity vs. human step-up budget (AgentDojo banking)

**Read next:** [WRITEUP.md](WRITEUP.md) (10-minute technical write-up) · [RESULTS.md](RESULTS.md) (full results, deviations, repo map)

## TL;DR

> TODO(Zinan): write in your own voice

## Motivation (fraud-ops lens)

> TODO(Zinan): write in your own voice

## What this is

When an agent pays on a user's behalf, a *mandate* (an authorization derived from the user's
request alone) can block injected payments. Tight mandates also block legitimate actions, and
each of those needs a human to approve it (a *step-up*). This repo measures that trade-off on the
AgentDojo banking suite: attack success rate vs. human step-ups per task vs. utility, across
four mandate granularity levels.

Our contribution is the **cost side**: how much human attention and how many false declines each
granularity level costs. Signed intent as a capability grant, mandate security analysis, and
capability-based defenses are prior work (see Related Work).

## Setup

- Benchmark: [AgentDojo](https://github.com/ethz-spylab/agentdojo) (Debenedetti et al., NeurIPS 2024),
  commit `089ed468cf3ed0322acc66b0211f26d9d90dbf60`, banking suite **v1.2.2** (latest shipped for banking):
  16 user tasks, 9 injection tasks. AgentDojo's own utility and security checks are used unchanged.
- Attack: AgentDojo `important_instructions` (template unchanged).
- Model: `claude-haiku-5-5` for both the agent and the mandate generator. Agent at the model's default
  effort (`medium`, set explicitly), mandate generator at `low`. AgentDojo's default system prompt.
- Mandate generator: one separate LLM call that sees **only the user's original prompt** and returns
  `{allowed_actions, max_amount_per_tx, explicit_payees, explicit_amounts}` (validated with pydantic;
  retried once on failure, then deny-all). Because it never sees tool output, an IBAN injected into a
  file or transaction subject cannot become an authorized payee.
- Known payees: recipients of the user's own past outgoing and scheduled transactions, computed in code
  from the pre-task environment. Senders of incoming money are excluded. The attacker IBAN is not a known payee.
- Default per-transaction cap when the prompt states none: the user's max historical outgoing amount (1100.00),
  raised to cover any amount the prompt names explicitly.

| level | state-changing call is in-mandate iff |
|---|---|
| M0 | always (no enforcement; baseline) |
| M2 | action type in `allowed_actions` AND amount <= cap |
| M3 | M2 AND payee in `explicit_payees` ∪ known payees ("first-time payee" rule) |
| M4 | M2 AND payee in `explicit_payees` AND amount in `explicit_amounts` (if any given) |

Out-of-mandate policy: **block** (tool error `Blocked: outside user authorization`, agent continues) or
**step-up** (a simulated reviewer approves iff the call matches the current user task's ground truth;
approved calls execute). Configs run: M0, M3-stepup, M4-stepup, M4-block, M2-stepup.

## Results

See [RESULTS.md](RESULTS.md) and [WRITEUP.md](WRITEUP.md). Figure: [figures/frontier.png](figures/frontier.png).

## Limitations

Single model, single attack, small benchmark (16 user tasks), a perfect simulated reviewer, and
AgentDojo checks with known quirks. Details in [RESULTS.md](RESULTS.md#6-limitations).

## Where the fraud analogy breaks

> TODO(Zinan): write in your own voice

## Related work

- **AgentDojo** (Debenedetti et al., NeurIPS 2024): the benchmark, tasks, attack and scoring used here.
- **CaMeL** (Google DeepMind): capability-based defense that separates control flow from untrusted data;
  our mandate is a much cruder, payment-specific capability derived from the user prompt.
- **A-VIP / AP2-WhisperBench** ([arXiv 2609.11757](https://arxiv.org/abs/2609.11757)): treats the signed
  intent as a capability grant for agent payments under AP2 and asks the user to confirm suspicious spend.
- **Beyond the Mandate** ([arXiv 2608.23858](https://arxiv.org/abs/2608.23858)): systematic security
  analysis of AP2 v0.2; shows valid mandate signatures do not guarantee the transaction reflects intent.
- **Agentic Commerce Bench** ([arXiv 2609.35886](https://arxiv.org/abs/2609.35886)): fraud classes and
  detectors for agents that spend money, including the cost of human review relative to payment size.

## Reproducibility

```bash
scripts/setup.sh                        # clones AgentDojo at the pinned commit, creates .venv, runs offline tests
.venv/bin/python -m mandate_bench.offline            # offline step-up table (no API calls)
export ANTHROPIC_API_KEY=...            # never written to disk by this code
.venv/bin/python -m mandate_bench.run --epoch 1      # full grid; resumable (skips records already in results/raw.jsonl)
.venv/bin/python -m mandate_bench.analyze            # results/summary.csv, figures/frontier.png
```

Code written with assistance from Claude Code.
