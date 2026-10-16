---
name: decide-investment
description: Analyze a user-selected Korean or US stock or ETF with the local Personal CIO project. Use for buy, hold, trim or exit questions, including a stated holding and average cost, price plans, thesis challenges and evaluation of recorded assessments. Produces a researched, validated decision; does not place orders.
---

# Decide Investment

The user selects the security and authorizes every real order. The local program
collects, calculates, stores and checks integrity. **You are the sole investment
reasoner.** Never replace the CIO verdict with a Python rule, and never present PC
validation as investment judgment.

This file is the **workflow**. The judgment rules live in `prompts/cio-system.md`,
which step 5 loads. Field semantics live in each packet field's `_role`/`_note`.
Nothing is repeated across the three.

## Project location

This skill runs the existing Personal CIO repository, not a standalone copy. Resolve
this SKILL.md's symlink, then go up two directories to the repository root. Read its
README.md quick start and run all commands there. From another working directory,
`python <installed-skill>/scripts/run.py assets` runs the CIO CLI in that root;
all arguments after the script name are CIO arguments. Never copy personal policy,
holdings or credentials into the skill installation.

For investment analysis, use only the user request, packet and CIO prompt; do not
read session memory. If the user also asks for development, follow the repository's
AGENTS.md development bootstrap, but do not use development memory as investment
evidence. Do not stage or commit changes.

## Workflow

1. Identify one asset, a candidate price and the intent. New capital (`NEW_BUY`/`ADD`)
   needs an amount and currency. A holding review (`HOLD_REVIEW`/`TRIM_REVIEW`/
   `EXIT_REVIEW`) needs no buy budget. Ask only for a value that cannot be inferred
   safely. "Should I sell?" without a chosen sell size is `HOLD_REVIEW`; do not turn
   the entire holding into `--quantity` (that flag means the proposed sell size).
2. `python -m cio assets`. If absent, register it with the correct `KR|US`,
   `STOCK|ETF`, exchange, price symbol, filing source or ETF structure, benchmarks,
   analysis lens, and valuation families. For an ETF also pass `--issuer`/`--isin`
   when that issuer's route exists, `--currency-hedged`, `--total-fee-pct` and
   `--optional-collector ETF_CONSTITUENTS`; pick benchmarks that have price history.
3. `python -m cio refresh ASSET`. Add `--run` when required artifacts are stale or
   missing; add `--optional` when the asset's lens makes those inputs useful.

   Then `python -m cio score ASSET`, **before** building the packet. The packet
   reads stored evaluations off disk, so scoring afterwards leaves
   `prior_assessments[].latest_evaluation` one step behind — you would judge without
   seeing how your own previous judgment actually did.
4. Build the packet. **Carry the user's own words through** — they are verified, not
   assumed.

   ```bash
   python -m cio prepare ASSET --price PRICE --amount AMOUNT \
     --amount-currency KRW_OR_USD \
     --intent NEW_BUY|ADD|HOLD_REVIEW|TRIM_REVIEW|EXIT_REVIEW|REBALANCE \
     --verbatim "what the user actually said" \
     --claim "each thing the user asserted as fact"   # repeatable
   ```

   For a holding review, omit `--amount`/`--amount-currency`. When the user states a
   holding, pass `--held-quantity HELD` and, if stated, `--average-cost COST` plus
   `--average-cost-currency KRW|USD` in the currency the user actually stated (the
   default is asset currency). These preserve this question's conditions without
   editing the ledger:

   ```bash
   python -m cio prepare ASSET --price PRICE --intent HOLD_REVIEW \
     --held-quantity HELD --average-cost COST --verbatim "user's exact question" \
     --claim "each factual assertion"
   ```

   For example, a US holding of 100 shares with a KRW 6,000 average uses
   `--held-quantity 100 --average-cost 6000 --average-cost-currency KRW`.
   Never divide that average by current FX and label the result a historical USD
   acquisition cost. The packet calculates KRW valuation/P&L with dated FX, leaves
   unknown native cost/P&L null, and labels any current-FX break-even separately.
   Conversely, a USD average alone does not establish historical KRW P&L.

   If the user chose a sell size, add `--quantity SELL`, or use
   `--target-quantity REMAINING`. Check `request.position_input`,
   `portfolio.local_position_comparison` and both readiness states. User inputs are
   scenario conditions, not broker-verified facts: never confirm them using their
   own derived calculations. Explain any conflict with the local holding. A
   position-only scenario has no reliable account cash or weights; do not infer them.

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
   For each material gap, identify which requested answer or assumption it could
   change, then collect or calculate that input. Within authorized project work,
   build a reusable extractor or calculator when the gap recurs. Do not remove a
   requested output merely because its supporting evidence needs more work.

   ```bash
   python -m cio docs ASSET                # what is stored, what is not fetched
   python -m cio search ASSET "부 문 \|"   # segment revenue table, for example
   ```

   Then research what is genuinely absent — market reaction, competitor moves, pricing,
   anything after the last filing. Preserve decision-critical originals before citing:

   ```bash
   python -m cio snapshot URL --asset ASSET --label short-name --note "why"
   python -m collectors.dart document RCEPT_NO   # Korean filing original
   ```

   Korean accession numbers are in `filings.latest_material_events`. The DART viewer
   URL is a frame shell and `snapshot` will say so.
7. Produce one decision JSON matching the schema. Follow `prompts/cio-system.md`.
   For a staged-buy proposal, include `entry_plan`; its budget is checked against
   the saved request and its arithmetic is rendered by `show`. Read
   `docs/advisory-plans.md` for the small plan and condition-review contract.
   For a future staged-sell proposal, include `exit_plan` with future quantities,
   limit prices, dependencies and cancellation conditions; keep the current sell
   solely in `action`. Review prior stage conditions with `exit_plan_checks` and
   do not infer that a touched limit price was filled.
   If `capital_changes` is missing or invalid, run `collectors.kr_capital` for
   supported Korean issuance forms before using its figures. An EPS assumption
   with `earnings_basis_required` must include the bridge in `docs/assumptions.md`.
   When `model_contract.assumption_review_required` is true, read
   `docs/assumptions.md` for the input contract and examples. Select your method,
   bases, plausible alternatives and boundary search intervals, then run:

   ```bash
   python -m cio assumptions data/decision-assumptions.json --context CTX_ID
   python -m cio assumptions ASM_ID --asset ASSET --json
   ```

   The report belongs to that saved context; no second `prepare` is needed. Use its
   baseline returns and combined win probability in the decision. Answer every
   alternative and boundary in `assumption_review.responses`. New research cited
   there must already be preserved by `snapshot` or the filing collector; bare URLs
   cannot replace files. Read previous assumption reports by ID when carried forward.
   Before you write the price plan, read `prior_assessments[].latest_evaluation` and
   `pace_since_decision`. If the price already reached what you called a twelve-month
   target, explicitly reassess it. Prior plans are hypotheses to evaluate and learn
   from; replace them whenever current reasoning warrants it, including when the
   earlier analysis was mistaken. Explain current evidence for keeping a plan too.
8. `python -m cio validate DECISION.json` for a dry run (writes nothing), or
   `python -m cio record DECISION.json` for an official attempt — it validates,
   stores valid output, and quarantines invalid output with its errors. A packet
   serves a decision for 12 hours after `prepare`, and a decision is recorded within
   12 hours of its `created_at`; past either limit, prepare again rather than reuse
   the old packet or draft.
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
  `python -m cio requests ASSET --resolve REQ-xxxx --state DONE --note "..."`.
- Do not place or simulate an order unless the user separately requests it.

## Investor policy

Read it from the packet's `investor_policy` block — values, `unset`,
`intentionally_unset`, and a `checks` array with the arithmetic (`now`, `breach_now`,
exposure against each limit). A limit that is unset is **not** unlimited; see the
prompt for how to answer when a conclusion depends on one. Note that deliberate gaps
are split by shape: a gap in a checkable limit shows up in `checks` as
`NO_LIMIT_BY_CHOICE`, a gap in a non-limit field shows up in `intentionally_unset`.
Look in both before concluding a limit is missing.

`python -m cio policy` is a **user-facing view, not a workflow step.** It shows the
same policy without requiring an asset and exits non-zero when the file is absent or
invalid, which makes it useful to a human and to a shell script. It has no logic the
packet does not already run, and running it changes nothing about your decision — a
step whose omission changes no outcome is not a step.

## Evaluation

`python -m cio score ASSET` scores stored judgments against later prices. It keeps
one evaluation per judgment, scores live and retired ones separately
(`source: LIVE|RETIRED`), and adds fixed 1/3/6/12/24-month returns and
benchmark-relative excess alongside the model's self-declared thresholds. Compare
models on the fixed horizons, never on self-chosen ones, and never on a single outcome.

The packet carries only the live judgments' `latest_evaluation` — five scalars each.
That is deliberate: a full scoring history would eat the budget and go unread in most
judgments. When you need the deeper view, ask for it:

```bash
python -m cio evals ASSET           # latest evaluation per judgment
python -m cio evals ASSET --plan    # with each judgment's entry, stop, scale-out
```

`--plan` puts the thresholds in a column, which is how a real defect surfaced: across
eleven judgments on one asset the `preferred_entry` tracked that day's quote at
r = +0.827 — a fixed discount below the screen rather than an estimate of worth, so the
entry band was never reached even when the price fell into it. **No single judgment can
see that about itself.** Read this before repeating a plan you have written before.
