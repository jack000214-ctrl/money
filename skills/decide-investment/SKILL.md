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
- Preserve currency boundaries. A USD price and a KRW budget need a dated FX
  conversion; never divide them directly.
- Keep local source IDs and every newly researched source in the decision record.
- Never mark your own `research_requests` resolved. Only the user closes one, with
  `python3.13 -m cio requests ASSET --resolve REQ-xxxx --state DONE --note "..."`.
- Do not place or simulate an order unless the user separately requests it.

## Investor policy

`python3.13 -m cio policy` shows the user's capital constraints and which limits are
still unset. The packet carries the same values plus arithmetic comparisons. A limit
that is unset is **not** unlimited — see the prompt for how to answer when a conclusion
depends on one.

## Evaluation

`python3.13 -m cio score ASSET` when price history has accumulated. It scores live and
retired judgments separately (`source: LIVE|RETIRED`) and adds fixed 1/3/6/12/24-month
returns and benchmark-relative excess alongside the model's self-declared thresholds.
Compare models on the fixed horizons, never on self-chosen ones, and never on a single
outcome.
