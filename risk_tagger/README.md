# Archived risk_tagger baseline

This package remains for frozen offline comparisons. Live `run-open` and `relabel`
commands have been retired; use `risk-pr score-pr` with Jev as described in the
[root README](../README.md). The notes below document the earlier experiment.

A from-zero, self-contained PR risk tagger that labels every PR `low | medium | high`.
Built in its own folder so it can develop alongside the concurrently-edited
`src/risk_pr_agent` package without conflict. Its scoring/labeling/evaluation is a
historical reimplementation grounded in the papers in `../papers/`.

## The idea: two tails on one score

The operational decision is binary at each tail of a single continuous risk score:

- **`low` = bypass-eligible** — the small, gated slice safe enough to skip extra review.
- **`high` = needs-attention** — the small slice worth extra scrutiny.
- **`medium`** — the large residual (today's normal review).

The costly error is a risky PR landing in **`low`** (it would skip review). So the policy is
**recall-first**: any hard risk signal *forbids* `low`. We measure success primarily by
**leakage** — the fraction of bad-outcome PRs that got labeled `low` — and drive it to ~0.

### Policy

```
composite_pct = per-repo percentile (as_of) of a weighted, explainable risk score
gating        = risk signals that forbid bypass (see below)

high   if  any force_high signal  or  >=5 gating signals  or  composite_pct >= 90
low    if  no gating signals  and  composite_pct <= 40  and  is_safe_scope(...)
medium otherwise
```

- **`force_high` signals** (rare + empirically high outcome-lift on Sentry history):
  `migration_with_app_code` (lift ~1.9), `ci_or_deploy_changed` (~3.5),
  `public_api_broad_blast` (~3.2).
- **block-low signals** (common or moderate lift — they forbid bypass and feed the score
  but don't force `high`): `historically_unstable_area`, `large_relative_churn`,
  `broad_diffusion`, `dependency_change`, `data_deletion`, `auth_change_without_tests`,
  `code_changed_without_test_signal`, `low_area_familiarity`.
- **No per-repo custom rules.** Signals and cuts are global constants; only the percentile
  calibration is per-repo and data-driven.

Signal weights/tiers were calibrated from measured per-signal lift on 76k Sentry PRs
(`python -m risk_tagger.analyze`), not guessed. Notably `code_changed_without_test_signal`
(lift 0.71) and `auth_change_without_tests` (0.67) are *below* base rate for reverts, so they
only block bypass — they do not inflate risk.

## Results (getsentry/sentry, 76k PRs, 30-day maturity, chronological)

| metric | all matured rows | last-10% test slice |
| --- | --- | --- |
| label mix | low 1.2% / med 88% / high 11% | low 1.5% / med 86% / high 13% |
| **leakage into `low`** | **1 / 1192 (0.0008)** | **0 / 71 (0.0000)** |
| Recall@Top5% (lift) | 2.70× | strong |
| Recall@Top10% (lift) | 2.42× | — |

For comparison the prior logistic baseline had ~1.0× lift at top-10% on its held-out test.
Numbers do **not** transfer to other repos or to a clean SEV label — they are a proxy
(reverts + strict follow-up fixes) and must be re-derived per repo (see `../papers` briefs).

On the **currently-open** Sentry PRs the top of the ranking is migrations / broad refactors /
auth-SAML / billing changes; the only `low` is a docs-only PR — the intended conservative
bypass behavior.

## Commands

```bash
# Backtest on an existing feature dataset (self-contained, no API)
PYTHONPATH=. python -m risk_tagger.cli backtest \
  --input data/processed/expanded-2020/features.jsonl --maturity-days 30 --out tmp/bt

# Per-signal frequency + outcome lift (calibration diagnostic)
PYTHONPATH=. python -m risk_tagger.analyze data/processed/expanded-2020/features.jsonl

# Tests
PYTHONPATH=. python -m unittest risk_tagger.tests.test_labeling
```

## Files

- `labeling.py` — `LabelConfig`, signal detection, continuous score, two-tails decision.
- `percentiles.py` — self-contained `as_of` / `global` per-repo percentiles.
- `explanations.py` — deterministic descriptive→contextual→actionable reasons (no LLM, no
  person-blame).
- `evaluate.py` — leakage / non-low recall / Recall@Top-k + label distribution.
- `cli.py` — archived `backtest` command.
- `analyze.py` — per-signal lift diagnostic.

## Not in scope (post-MVP)

LLM scoring/explanation; reviewer recommendation + soft-block workflow; commit-level
`max(per-commit, full-diff)` aggregation; GitHub App / DB / deploy plumbing. The label payload
(structured signals + reasons) is designed so these can attach later without rework.
