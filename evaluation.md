# PR Risk Agent

Offline PR risk classification with [Typesafe AI Jev](https://vercel.com/changelog/typesafe-ai-jev-now-available-on-ai-gateway), exact Git snapshots and a held-out acceptance gate. Python owns datasets/evaluation; a Node worker uses AI SDK `7.0.106` and Vercel AI Gateway for one `Choice` question per PR.

Jev is currently an **evaluation candidate**. The public `score`, `build` and `score-pr` commands use the same engine and require `--candidate` until an accepted holdout receipt exists. Historical logistic/rule helpers remain only for frozen benchmark reproduction.

The standard input is **paths/status, additions/deletions per file and totals, PR title/description, and context-bounded diffs** (`metadata-diff`). It is the default for all three commands and the Python request/scoring APIs. Complete files and repository context are optional experiments.

## Setup

Use an editable checkout with Python ≥3.9 and Node ≥22:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
npm ci --prefix worker
export GITHUB_TOKEN="$(gh auth token)"
```

Put `AI_GATEWAY_API_KEY=...` in the root `.env`, or export it. Only that key is loaded from `.env`; existing environment values take precedence. Run commands from this checkout; the worker is not bundled into a standalone Python wheel. With dependencies installed, `PYTHONPATH=src python3 -m risk_pr_agent.cli` can also run the source checkout.

Run the chosen input configuration without profile flags:

```bash
risk-pr score --dataset data/low-review-v1 --candidate --out data/runs/metadata-diff
```

Use `--dry-run` instead of `--candidate` to inspect the exact prepared requests without calling Jev. The default local request guard is 1 MiB, configurable with `--max-bytes`; it is not the model context window. The default retains all PR metadata and deterministically trims code to fit this guard and a 30,000-token proxy estimate with a 2,000-token reserve. See [context limits and fallback](jev-risk-classification.md). Dry-run output records the estimate, truncation and omissions; actual Jev token usage is unknown until inference.

## Prepare the benchmark

Collect metadata using `backfill` (GitHub) or `git-backfill` (local Git):

```bash
risk-pr backfill --repo getsentry/cli --months 12 --out data/raw --resume
risk-pr prepare-dataset \
  --input data/raw/getsentry__sentry/prs.jsonl \
  --input data/raw/getsentry__cli/prs.jsonl \
  --input data/raw/getsentry__sentry-mcp/prs.jsonl \
  --input data/raw/getsentry__snuba/prs.jsonl \
  --git-repo getsentry/sentry=/path/to/sentry \
  --git-repo getsentry/cli=/path/to/cli \
  --git-repo getsentry/sentry-mcp=/path/to/sentry-mcp \
  --git-repo getsentry/snuba=/path/to/snuba \
  --refresh-metadata --out data/jev-v1
```

Seed `jev-v1` selects 250 representative merged PRs per repo independently of outcomes, then 30 disjoint review candidates per repo covering docs, tests, mechanical, functional, auth, migration, configuration and large diffs. Each review cohort splits chronologically into 20 development and 10 holdout examples. Timestamp ties stay together and shortfalls are reported.

`--refresh-metadata` caches authoritative GitHub spans and captured title/description for selected Git-derived rows. Use `--snapshot-metadata data/jev-v1/snapshot-metadata.json` to reuse the mapping offline. Existing per-PR metadata caches are immutable; older span-only caches do not establish that a description was captured. Required Git objects must exist in the supplied repositories. Missing objects, ambiguous rebase spans and inconsistent counts yield incomplete snapshots.

Snapshots bind exact base/head SHAs and provenance. Merge reconstructions are explicitly marked as merge snapshots, never as opening-time snapshots. Multi-commit rebases use the original PR head and merge base when verifiable. Renames/deletions are preserved. Snapshot version 6 supports binary and non-UTF-8 changes with per-side `content_metadata` (`kind`, byte size and SHA-256); an absent side is distinct from an empty text file. Content profiles send this metadata and any readable text side, without binary bytes or Git binary payloads. They record `content_not_inspected` in the request and `binary_files` inventory in the result. Binary presence does not impose a class; genuinely missing Git objects still prevent classification. Request context version 7 and rubric version 4 invalidate older inference caches, and binary hashes distinguish same-size content changes. New snapshots also capture available PR title/description and their collection provenance; blind review packets exclude those fields. Metadata is refreshed when reusing the immutable Git snapshot cache.

Previously exported snapshots may lack this metadata. The standard input returns `description_unavailable` for them; reconstruct a new dataset from captured raw PR metadata or use an explicit legacy `--variant A` to reproduce the previous diff-only experiment. An observed empty description is valid; an uncollected description is not silently replaced with an empty one.

Artifacts are separate:

- `inputs-{dev,holdout,representative}.jsonl`: source snapshots for each split.
- `review/index.md`: blind packets without predictions, outcomes or PR titles.
- `labels.jsonl`: human labels, rationale, reviewer/date, rubric version and exact snapshot.
- `outcomes.jsonl`: subsequent merged fixes/reverts with explicit references.
- `manifest.json`: selection, hashes, revisions, observation cutoff and coverage.
- `snapshots/`: resumable reconstruction cache.

Use the paths recorded in the manifest. The default observation cutoff is the earliest raw collection timestamp. Outcome metrics require at least 30 days since merge. No detected incident never implies a human `low` label. Historical proxies are incomplete observations, not proof of causality.

Freeze both existing engines before changing them further:

```bash
risk-pr freeze-baselines --dataset data/jev-v1 \
  --input data/raw/getsentry__sentry/prs.jsonl \
  --input data/raw/getsentry__cli/prs.jsonl \
  --input data/raw/getsentry__sentry-mcp/prs.jsonl \
  --input data/raw/getsentry__snuba/prs.jsonl \
  --model data/processed/expanded-2020/model_selected_static_v1_medium_outcome_strict.json
```

Baselines are immutable and include source/model hashes and exact revisions.

## Risk rubric and context

Classify the whole PR by plausible regressions, impact, scope and recovery:

- **low**: limited regression risk and impact, including editorial docs and mechanical changes.
- **medium**: behavioral or contract changes with plausible regressions and bounded impact/recovery.
- **high**: supported potential for severe consequences, broad disruption, weakened trust boundaries, data loss or difficult recovery.

Size, sensitive names, unchanged tests and absence of an obvious bug are not automatic rules. Precise criteria and counterexamples from the local `jev-review` reference are adapted to technical risk. Source content, including text requesting a label, is untrusted evidence.

The following legacy variants require an explicit `--variant`; omitting context flags selects `metadata-diff`.

| Variant | Context |
| --- | --- |
| A | Paths, status and complete diff |
| B | A plus old/new source windows around changes |
| C | B plus bounded tree, nearby README/manifests and up to four existing related tests |

Related tests are selected deterministically by filename and directory with path-component boundaries. Changed tests are already in the diff. Optional files are included whole when they fit; omissions are recorded.

Legacy experiments default to **65,536 bytes of serialized SDK request**, configurable with `--max-bytes`; bytes are not tokens. This is a local application guard, not Jev's context window. Additional context is removed before essential diff. Missing/oversized diff and provider context rejection return an explicit status with `risk_label: null`.

Rubric 2 makes the intended review effort explicit: low supports routine author/agent review, medium merits focused behavioral review, and high requires a credible mechanism for substantial production impact. Novelty alone is not high. See [risk boundaries, source guidance and fallback behavior](jev-risk-classification.md).

## Compare which inputs Jev needs

An input evaluation dataset contains complete source snapshots and an expected risk judgment per case, rather than PR links that must be fetched again. `cases.jsonl` pairs `input` with `expected.risk_label`, rationale, evidence and reviewer provenance. The CLI reads the separate `inputs-dev.jsonl`; `references.jsonl` is used only for evaluation. Assistant references remain explicitly attributed to an agent.

```bash
risk-pr prepare-input-eval \
  --snapshots data/jev-v1/inputs-dev.jsonl \
  --references data/input-ablation-v1/reviews/frozen-references.jsonl \
  --raw data/raw/getsentry__cli/prs.jsonl \
  --raw data/raw/getsentry__snuba/prs.jsonl \
  --repo getsentry/cli --repo getsentry/snuba \
  --out data/input-ablation-v1
```

The export checks the reference against the exact base/head revisions and keeps development cases only. It refuses to overwrite a frozen dataset. Captured empty PR descriptions are distinct from unavailable descriptions synthesized by Git collection. Description capture timestamps are saved separately: historical descriptions collected after merge are not opening-time snapshots.

Use descriptive input profiles to change only the supplied fields, keeping the same model, risk criteria and cases:

| `--input-profile` | Information sent |
| --- | --- |
| `paths` | Changed paths, file status and previous path for renames |
| `paths-lines` | Paths plus additions/deletions per file and totals |
| `description` | PR title and description |
| `paths-lines-description` | Paths, line counts, title and description |
| `diff` | Paths, status and complete diff; the input previously called A |
| `diff-description` | Complete diff, paths, status, title and description |
| `files` | Paths, status and complete before/after content of changed files |
| `metadata-diff` (default) | Full paths, line counts, title/description and context-bounded diffs |
| `metadata-files` | Paths, line counts, title/description, complete diff and complete final file contents (previous contents for deleted files) |

Counts come from the reconstructed diff, not title heuristics. Each profile has an explicit field allowlist: risk references, outcomes, reviewer reasoning and history never enter the request. Missing required fields remain unavailable; no profile silently substitutes zero counts. Only the default `metadata-diff` profile trims code, recording this explicitly while preserving metadata. Other profiles retain their complete-evidence requirements. Profiles are separate experiments and cannot be combined with legacy B/C context variants.

The two `metadata-*` profiles default to a **1 MiB local guard**. The default also applies its token proxy budget and permits code truncation; protected metadata that still cannot fit produces no label. It allows four total calls, including context fallback, with 10/20/30-second waits. Transient retries keep the same request; only explicit context rejection reduces code. Each distinct request has a separate hash and journal. Report quality and coverage for truncated cases separately; prior complete-diff results do not establish their quality.

The `metadata-files` experiment keeps its earlier evidence policy. It tries complete files first; an explicit provider context rejection triggers one reduced context with the complete diff and metadata. A local guard overflow can also remove optional file contents before sending. Omitted files and the final `context_stage` are recorded. If the complete diff is still too large, no label is produced. Network/auth errors never trigger context reduction. The two contexts have separate hashes/caches and their costs are accounted together, including unknown costs for rejected calls.

```bash
risk-pr score --dataset data/input-ablation-v1 \
  --input-profile paths-lines-description --candidate \
  --out data/input-ablation-v1/runs/paths-lines-description
risk-pr evaluate --dataset data/input-ablation-v1 \
  --predictions data/input-ablation-v1/runs/paths-lines-description/predictions.jsonl \
  --out data/input-ablation-v1/reports/paths-lines-description.json
```

To compare full source with automatic context fallback using the v2 references audited against rubric 2:

```bash
risk-pr score --dataset data/input-ablation-v2 \
  --input-profile metadata-files --candidate \
  --out data/input-ablation-v2/runs/metadata-files
risk-pr evaluate --dataset data/input-ablation-v2 \
  --predictions data/input-ablation-v2/runs/metadata-files/predictions.jsonl \
  --out data/input-ablation-v2/reports/metadata-files.json
```

For this dataset kind, `evaluate` automatically uses its assistant references. `score --dry-run` with a named profile saves the exact request in `prepared.jsonl`, so the supplied fields can be inspected without an API call. `build` and `score-pr` support the same profiles and attach captured metadata when needed.

The separate `data/low-review-v1` diagnostic dataset contains 20 new CLI and 20 new Snuba PRs. Selection is deterministic within predeclared change categories, excludes all original reviewed/representative IDs, and stays before each repository's holdout boundary. Its assistant references are frozen before inference. It deliberately targets review boundaries, so report it separately from the earlier development sample and do not interpret its low fraction as a repository-wide rate. `cases.jsonl` contains exact inputs plus expected judgments; `inputs-dev.jsonl` excludes the judgments. Run the same commands above with this dataset path, or regenerate the recorded comparisons offline with `PYTHONPATH=src python3 data/low-review-v1/run-experiment.py --summarize-only` when the local dataset is present.

Compare both full-cohort coverage and quality on the same classified cases; metadata-only profiles can fit PRs whose diffs or complete files exceed the budget. Report class support as well: a common subset with no high-risk examples cannot measure detection of that class. These development experiments do not select or promote the production candidate.

## Compare candidates

```bash
risk-pr score --dataset data/jev-v1 --split dev --variant A \
  --candidate --out data/jev-v1/runs/dev-A
risk-pr score --dataset data/jev-v1 --split dev --variant B \
  --candidate --out data/jev-v1/runs/dev-B
risk-pr score --dataset data/jev-v1 --split dev --variant C \
  --candidate --out data/jev-v1/runs/dev-C
risk-pr evaluate --dataset data/jev-v1 --split dev \
  --predictions data/jev-v1/runs/dev-A/predictions.jsonl \
  --out data/jev-v1/reports/dev-A.json
```

Evaluate B/C with their respective paths. Use `--dry-run` to inspect coverage without inference or credentials, and `--split representative` for the 1,000-PR operational/outcome benchmark. Reports allow pending human labels but cannot select/promote a model until review is complete.

Quality metrics include macro-F1, per-class precision/recall, confusion, low→high, total false highs, high→low, repo/change-stratum breakdowns and class calibration. These probabilities describe risk classes, **not incident probability**. Historical Recall/Precision@Top-5/10/30% uses `P(high) + 0.5 × P(medium)`.

Operational reports include coverage, errors, retries, latency and per-PR cost mean/p50/p95/max plus 1,000/10,000-PR projections. Gateway-reported cost and live-price estimates are separate; missing usage/cost stays unknown. The current catalog price is saved with every run. No price threshold is imposed.

Each report also includes `low_review` curves for valid predictions labeled `low`. They separately apply fixed thresholds of 0, 0.5, 0.7, 0.8, 0.9, 0.95 and 0.99 to `P(low)` and provider confidence. Each threshold reports coverage across all expected PRs, precision against known references with a 95% Wilson interval, medium/high references incorrectly labeled low, and recall across all expected low references, including those with missing predictions. Unknown references remain visible but never earn precision credit; missing confidence stays unknown. These are descriptive comparisons: no threshold is selected automatically, and they do not authorize merging or alter acceptance gates.

Low-review eligibility requires a prediction matching a known expected base/head snapshot. A prediction with no verifiable expected revision counts as `snapshot_unverified` in this metric and cannot qualify, even if its label and probabilities are otherwise valid.

For a development comparison against assistant review proposals:

```bash
risk-pr evaluate --dataset data/jev-v1 --split dev \
  --predictions data/jev-v1/runs/dev-A/predictions.jsonl \
  --assisted-labels data/jev-v1/assisted-review/references-dev.jsonl \
  --out data/jev-v1/reports/assisted-dev-A.json
```

Assisted references require `source: "agent"`, `review_status: "proposed"`, `proposed_risk_label`, a rationale, reviewer, review date, rubric version and matching base/head snapshots. Freeze these reviews independently before inspecting predictions. Null proposals and invalid or stale references are excluded with explicit reasons. Reports record `assisted_references` counts/hash and calibration against assistant-reviewed classes; they do not claim human ground truth. This mode only supports `dev`, always leaves acceptance false, and cannot feed `select-candidate` or the holdout gate. The human-label workflow below is unchanged.

After blind human review and development evaluation:

```bash
risk-pr select-candidate \
  --report data/jev-v1/reports/dev-A.json \
  --report data/jev-v1/reports/dev-B.json \
  --report data/jev-v1/reports/dev-C.json \
  --out data/jev-v1/selection.json
risk-pr score --dataset data/jev-v1 --split holdout \
  --selection data/jev-v1/selection.json --candidate \
  --out data/jev-v1/runs/holdout
risk-pr evaluate-holdout --dataset data/jev-v1 \
  --selection data/jev-v1/selection.json \
  --predictions data/jev-v1/runs/holdout/predictions.jsonl \
  --out data/jev-v1/reports/holdout.json
```

Selection needs human labels for every development example and all three classes in exactly the same evaluable subset across variants; it prefers quality on that subset, then known cost. Full-cohort metrics and coverage remain visible, including abstentions. Holdout inference checks the entire selected configuration. Final evaluation reserves the frozen cohort once, independent of output names or subsequent prompt changes.

Promotion requires better macro-F1, fewer total false highs and no increase in true highs classified low on the same evaluable holdout examples, with matched baseline revisions for every classified example and at least five examples per class. Acceptance reports evaluated/expected counts, coverage and abstention reasons; unclassified examples retain null labels, and the quality claim does not extend to them. Full-cohort metrics, support and uncertainty remain visible; acceptance records observed evidence, not statistical significance. Insufficient evidence keeps Jev a candidate. Supply an accepted report/receipt via `--acceptance` to run without `--candidate`.

## Inference commands and schema

```bash
risk-pr score-pr --repo getsentry/cli --pr 123 \
  --git-repo /path/to/cli --candidate --out result.json
risk-pr build --repo getsentry/cli --months 3 --max-prs 50 \
  --git-repo getsentry/cli=/path/to/cli --candidate --out data/jev-build --resume
```

Inference needs no trained model or historical features. Schema version 2 retains `risk_label` as primary output and records status, probabilities, available confidence, exact snapshot, versions, full request hash, provider-reported model, usage, attempts, latency and cost. Results and attempt journals persist per PR; successful requests are cached by complete request/configuration. SDK retries are disabled; Python controls transient retries.

Old `train`, `apply-model`, `combine` and live `risk_tagger run-open` interfaces are retired. Offline baseline helpers/tests remain for reproducibility. `ideas.md` and `risk-system-mvp-roadmap.md` contain earlier research, not the current inference contract.

## Validation and scope

```bash
PYTHONPATH=src python3 -m unittest discover -s tests
PYTHONPATH=src python3 -m unittest discover -s risk_tagger/tests
npm test --prefix worker
npm run check --prefix worker
```

Tests cover exact snapshots, rebases, renames/deletions, binary/incomplete context, outcome isolation, chronology, budgets, existing tests, misleading docs claims, prompt injection, cache/retries, unknown accounting and holdout integrity.

GitHub Apps, label publication, deployment, databases and new service infrastructure are out of scope.
