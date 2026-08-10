---
name: decide-investment
description: Evaluate a user-selected Korean or US stock or ETF at a stated price and amount as a provider-neutral AI CIO. Use when the user asks whether buying will win or fail, expected return, a preferred entry, downside stop, scale-out start, recheck prices, position amount, thesis challenge, or later evaluation of a recorded investment assessment.
---

# Decide Investment

The user selects the security and authorizes every real order. The local program
collects, calculates, stores and checks integrity. **You are the sole investment
reasoner.** Never replace the CIO verdict with a Python rule, and never present PC
validation as investment judgment.

This file is the **workflow**. The judgment rules live in `prompts/cio-system.md`,
which step 5 loads. Field semantics live in each packet field's `_role`/`_note`.
Nothing is repeated across the three.

## Workflow

1. Confirm one asset, a candidate price, an amount, and the amount currency. Ask only
   for a value that cannot be inferred safely.
2. `python3.13 -m cio assets`. If absent, register it with the correct `KR|US`,
   `STOCK|ETF`, exchange, price symbol, filing source or ETF structure, benchmarks,
   analysis lens, and valuation families.
3. `python3.13 -m cio refresh ASSET`. Add `--run` when required artifacts are stale or
   missing; add `--optional` when the asset's lens makes those inputs useful.

   Then `python3.13 -m cio score ASSET`, **before** building the packet. The packet
   reads stored evaluations off disk, so scoring afterwards leaves
   `prior_assessments[].latest_evaluation` one step behind — you would judge without
   seeing how your own previous judgment actually did.
4. Build the packet. **Carry the user's own words through** — they are verified, not
   assumed.

   ```bash
   python3.13 -m cio prepare ASSET --price PRICE --amount AMOUNT \
     --amount-currency KRW_OR_USD \
     --intent NEW_BUY|ADD|HOLD_REVIEW|TRIM_REVIEW|EXIT_REVIEW|REBALANCE \
     --verbatim "what the user actually said" \
     --claim "each thing the user asserted as fact"   # repeatable
   ```

   Pass `--intent` always: a trim review is not a new-buy question. Pass `--claim` for
   every factual assertion in the request; each one must later be closed as
   `CONFIRMED`, `REFUTED`, or `UNVERIFIABLE` or the decision will not record.
   Use `--no-save` only for a dry run — a decision that will be recorded needs the
   saved context because `record` verifies the referenced snapshot.
5. Read the saved context, then `prompts/cio-system.md` and
   `schemas/cio-decision.schema.json`. Open evidence files only when a material
   question requires them; do not load the repository indiscriminately.
6. Search local originals before the web. `local_documents` lists what is stored; the
   body is not in the packet because one annual report runs to a million characters.

   ```bash
   python3.13 -m cio docs ASSET                # what is stored, what is not fetched
   python3.13 -m cio search ASSET "부 문 \|"   # segment revenue table, for example
   ```

   Then research what is genuinely absent — market reaction, competitor moves, pricing,
   anything after the last filing. Preserve decision-critical originals before citing:

   ```bash
   python3.13 -m cio snapshot URL --asset ASSET --label short-name --note "why"
   python3.13 -m collectors.dart document RCEPT_NO   # Korean filing original
   ```

   Korean accession numbers are in `filings.latest_material_events`. The DART viewer
   URL is a frame shell and `snapshot` will say so.
7. Produce one decision JSON matching the schema. Follow `prompts/cio-system.md`.
   Before you write the price plan, read `prior_assessments[].latest_evaluation` and
   `pace_since_decision`. If the price already reached what you called a twelve-month
   target, say so and act on it — a threshold does not look at the clock, so half a
   horizon of return arriving in two days trips nothing on its own.
8. `python3.13 -m cio validate DECISION.json` for a dry run (writes nothing), or
   `python3.13 -m cio record DECISION.json` for an official attempt — it validates,
   stores valid output, and quarantines invalid output with its errors.
9. Report the verdict, win/failure definitions, horizon, probabilities and scenarios,
   preferred entry, stop, scale-out start, recheck prices and events, proposed amount,
   the action on any existing holding, the strongest counter-thesis, and unresolved
   risks. **State that the user alone authorizes the real trade.**

## Operating guardrails

These are about running the tools. The judgment rules are in the prompt.

- `validation = PASS|BLOCKED` describes data, arithmetic, freshness and reference
  integrity only. You own `verdict`. From a `BLOCKED` context, repair the inputs and
  prepare again rather than issuing a numeric verdict.
- **Quote numbers; do not manufacture them.** Every figure you report — realized P&L,
  transaction tax, post-trade weight, days to an event — must come from the packet, a
  calculator, or a command. Arithmetic done in your reply is not reproducible, is not
  in the record, and is not checked by anything. The division of labour is the point:
  the local program calculates and verifies, you judge. If a number you need is
  missing, say it is missing and file a `research_request`; do not fill the gap by
  hand. This is the one rule no validator can enforce, which is exactly why it is here.
- Preserve currency boundaries. A USD price and a KRW budget need a dated FX
  conversion; never divide them directly.
- Keep local source IDs and every newly researched source in the decision record.
- Never mark your own `research_requests` resolved. Only the user closes one, with
  `python3.13 -m cio requests ASSET --resolve REQ-xxxx --state DONE --note "..."`.
- Do not place or simulate an order unless the user separately requests it.

## Investor policy

Read it from the packet's `investor_policy` block — values, `unset`,
`intentionally_unset`, and a `checks` array with the arithmetic (`now`, `breach_now`,
exposure against each limit). A limit that is unset is **not** unlimited; see the
prompt for how to answer when a conclusion depends on one. Note that deliberate gaps
are split by shape: a gap in a checkable limit shows up in `checks` as
`NO_LIMIT_BY_CHOICE`, a gap in a non-limit field shows up in `intentionally_unset`.
Look in both before concluding a limit is missing.

`python3.13 -m cio policy` is a **user-facing view, not a workflow step.** It shows the
same policy without requiring an asset and exits non-zero when the file is absent or
invalid, which makes it useful to a human and to a shell script. It has no logic the
packet does not already run, and running it changes nothing about your decision — a
step whose omission changes no outcome is not a step.

## Evaluation

`python3.13 -m cio score ASSET` scores stored judgments against later prices. It keeps
one evaluation per judgment, scores live and retired ones separately
(`source: LIVE|RETIRED`), and adds fixed 1/3/6/12/24-month returns and
benchmark-relative excess alongside the model's self-declared thresholds. Compare
models on the fixed horizons, never on self-chosen ones, and never on a single outcome.

The packet carries only the live judgments' `latest_evaluation` — five scalars each.
That is deliberate: a full scoring history would eat the budget and go unread in most
judgments. When you need the deeper view, ask for it:

```bash
python3.13 -m cio evals ASSET           # latest evaluation per judgment
python3.13 -m cio evals ASSET --plan    # with each judgment's entry, stop, scale-out
```

`--plan` puts the thresholds in a column, which is how a real defect surfaced: across
eleven judgments on one asset the `preferred_entry` tracked that day's quote at
r = +0.827 — a fixed discount below the screen rather than an estimate of worth, so the
entry band was never reached even when the price fell into it. **No single judgment can
see that about itself.** Read this before repeating a plan you have written before.
