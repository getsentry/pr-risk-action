# Jev risk classification and review effort

Risk describes the change's plausible regression paths, production impact, reach and recovery. It supports deciding how much review a PR needs. It is not a claim that a bug exists, and the classifier does not perform or authorize a merge.

## Risk boundaries (rubric 4)

Rubric 4 keeps the class boundaries from rubric 2 and the binary-evidence guidance from rubric 3. It adds an explicit warning that truncated code is incomplete evidence, not evidence that omitted changes are safe. Earlier experiments remain historical development evidence; they do not validate the new bounded context.

| Label | Meaning | Examples |
| --- | --- | --- |
| low | Evidence supports a narrow, well-understood change with limited plausible regression risk and impact; routine author or agent review is appropriate alongside normal checks. | Editorial docs, mechanical moves without behavior changes, a contained and reversible correction. |
| medium | Plausible regressions have bounded impact or recovery; review the changed behavior. Also appropriate when the evidence does not support low and no substantial production-impact mechanism is established. | An isolated new feature, a localized contract change, bounded changes to permissions or error handling. |
| high | A credible mechanism could cause substantial production disruption, weakened trust boundaries, persistent data loss/corruption, or a failure with wide impact or difficult recovery. Careful owner review is appropriate. | Changing shared load controls without preserving existing configuration, or removing a production routing fallback while changing which pipeline handles traffic. |

A new feature is not automatically high. Consider whether it changes existing behavior, is actually isolated, runs by default, changes shared state or infrastructure, and can be disabled or reverted. A feature flag in the description is a claim to check against the code. Similarly, “any code could have a bug” is too broad a reason for high. Missing modified tests, sensitive paths, line counts and titles are not automatic labels.

## SDK repositories

Sentry SDK repositories share one meaning of the three labels, anchored on customer impact. A released SDK cannot be rolled back and customers upgrade slowly, so a wrong `low` is the expensive mistake; a wrong `medium` or `high` only costs a review. The label describes the change, never its author: the same diff gets the same label whoever wrote it.

### How SDK mode is selected

A repository is in SDK mode when the Action contains `sdk/repos/<owner>__<repo>.toml` (lower case, for example `getsentry__sentry-java.toml`). There is no action input. Repositories without a fact sheet keep today's request, hashes and result keys exactly. A repository string that is not an `owner/name` slug is treated as having no fact sheet. The rubric, fact sheets and rules load only from the Action's own checkout, never from the target repository or the pull request.

If a fact sheet or `sdk/rubric.toml` cannot be loaded or fails validation, the result has status `invalid_fact_sheet` and no label.

SDK mode applies to the default `metadata-diff` input profile, which is what the Action uses. Other experimental input profiles and legacy variants ignore fact sheets.

The first fact sheets cover sentry-java, sentry-javascript, sentry-python, sentry-dotnet, sentry-cocoa and sentry-react-native.

### What the levels mean

| Label | Meaning |
| --- | --- |
| low | The supplied code positively shows one of: (a) nothing that ships to customers changes at runtime (docs, comments, tests, samples, developer tooling that never affects the shipped artifact, mechanical moves with no behavior change); or (b) shipped behavior changes only in a narrow, fully visible way that cannot crash, hang or slow the host application, cannot change or drop data sent to Sentry, and changes nothing customers compile, link or configure against. "No obvious bug" is not enough. |
| medium | The default. Shipped behavior changes with a plausible way to regress and bounded impact: one integration, an opt-in or off-by-default feature, or a failure that degrades data sent to Sentry without harming the host application. Also the answer whenever the evidence does not support low and no high mechanism is established. |
| high | A credible, change-specific path to harming customers' applications or all users of an SDK area, on a default or widely used path. Examples: a crash or hang in the host application, a noticeable startup or size cost, an incompatible public API or default change, broad loss or personal-data exposure in what is sent to Sentry, or a dependency or bundled SDK change every customer inherits. |

### What Jev sees

The PR title and description are not sent, so editing them can never change a label. Text inside the diff is still shown and is treated as evidence, not instructions. Each changed file carries tags from the fact sheet (below), and the question includes an example block only for the families the PR touches, plus a sentence about shared core when it is touched. Snapshot, diff ordering, context budget, retries and fallback are unchanged.

### Families and kinds

A fact sheet holds two independent mappings from path patterns (`*` matches within a directory, `**` across directories).

- Areas say where code runs and give one or more families: `server`, `mobile-native`, `browser`, `hybrid`, `tooling`. An area may also be marked `shared_core`, meaning every platform in the repository builds on it.
- Kinds say what a file is: `tests`, `samples`, `docs`, `dev-tooling`, `public-api-record`, `dependencies`, `build-release`.

A file can have several kinds; every matching pattern applies and the blocking kinds still block. A file whose kinds are all non-shipping (`tests`, `samples`, `docs`, `dev-tooling`) counts towards no family and is never shared core. A file matching no area and no kind is unmapped. A rename is tagged by both its old and new path.

`sdk/defaults.toml` holds kinds that look the same everywhere. They are merged into every fact sheet; a repository sheet can add kinds but cannot remove or override them. Fact sheets carry no free text, only path patterns, family names, kind names and `shared_core`.

### Rules that block low

After the model answers, fixed rules run in Python. A rule never lowers a label and never produces `high`; it can only raise a model `low` to `medium`.

| Rule | Fires when |
| --- | --- |
| R1 | Any code was truncated or omitted, or the request fell back to a reduced context stage. |
| R2 | Any binary side was not inspected. |
| R3 | Any changed file is unmapped. |
| R4 | Any changed file is a `public-api-record`. |
| R5 | Any changed file is of kind `dependencies`. |
| R6 | Any changed file is of kind `build-release`. |
| R7 | The model chose low and `probabilities.low` is below 0.90. Provider confidence plays no part. |

The model is always called. Every rule that fires is recorded in `low_blocked_by` whatever the model chose, so analysis can see how often each rule would bind, but only a model `low` is raised.

### Result fields

- `risk_label`: the final label after the rules. Callers should publish and read this one.
- `model_label`: what the model chose.
- `low_blocked_by`: a list of `{"rule", "files"}` entries, one per rule that fired.
- `mechanisms`: for each risk mechanism, the model's probability from a yes/no question asked in the same request (`m_stability`, `m_performance`, `m_contract`, `m_data`, `m_inherited`, `m_reach`, `m_ships`). A response that lacks any of them is `invalid_response` with no label.
- `sdk`: the rubric version, a hash of the fact sheet and defaults, `families_touched`, `shared_core`, and per-file families, kinds and `unmapped`.

A result whose status is not `ok` carries no label, model label, rules or mechanisms: `risk_label` is null, and `model_label`, `low_blocked_by` and `mechanisms` are absent or null. This covers a model response that failed validation and, in the Action, a result discarded because the pull request moved during scoring or the result failed the Action's own checks. Only `sdk`, the path facts, may remain.

A consumer must treat a `low` as having passed the rules only when `model_label` is present in the result. A `low` without `model_label` did not come from SDK mode and was not gated by R1–R7.

The per-attempt log inside a result (`attempts`) keeps the model's raw label. Read the top-level `risk_label`, not an attempt's.

Mechanism answers are shown in the summary and recorded, but they do not change the label. Jev returns probabilities, not written reasoning, so the summary explains a label with the facts instead: the final and model labels, the families touched, each rule that fired with up to five of its files, and the mechanism probabilities, highest first. The SDK explanation comes before the closing disclaimer.

### Adding a repository

1. Write `sdk/repos/<owner>__<repo>.toml` (lower case) with `[[area]]` and `[[kind]]` tables, reading the repository's layout. Use patterns rather than lists so new modules need no edit.
2. In a clone of the repository, run `git ls-files | risk-pr sdk-coverage --repo OWNER/NAME` until it exits 0. It prints counts per family and kind and lists unmapped paths; exit status 1 means some are unmapped, 2 means no fact sheet was found. Exit status 1 also covers a fact sheet or `sdk/defaults.toml` that fails to load or validate; the reason is printed on stderr.
3. Add spot-check tests in `tests/test_sdk_facts.py` for the paths that matter most (shared core, public API records, samples, build files).

Never give a non-shipping kind (`tests`, `samples`, `docs`, `dev-tooling`) to something that ships, such as a published test-support module or docs that carry runnable configuration. Such a file would stop counting towards its families and could be labelled low. A stale sheet fails safe: new paths are unmapped, which blocks low through R3, but someone must keep sheets current.

Changing the wording in `sdk/rubric.toml` needs a bump of its `version`, so SDK requests never share a cache entry with an older rubric. Changes under `sdk/` and the `sdk_*.py` modules change what every SDK repository's label means and deserve owner review.

## Label publishing

With the Action input `publish-label: true`, the Action applies the labels `risk: low`, `risk: medium` and `risk: high` (colours `0E8A16`, `FBCA04`, `B60205`; description `PR risk score: <class>`). With any other value it makes no write call to GitHub. In order:

1. After validating the repository and PR number and confirming credentials, and before scoring, remove all three risk labels from the PR. If that fails, the run ends `label_error` without scoring, because the old label may still be there.
2. Score as usual. The scoring process does not receive the GitHub token.
3. Only when the status is `ok`: re-read the PR; if it is closed or its base or head differs from the scored revision, add nothing and report `pr_changed`. Otherwise create the repository label if missing (a concurrent creation is fine), add the final `risk_label` (never `model_label`), and re-read the PR again; if it moved or closed, remove the label and report `pr_changed`.
4. Any failure in step 3 removes the label if it may have been added and reports `label_error`.

`label_error` and `pr_changed` carry no label and no model verdict, like every non-`ok` status, and fail the job. Since step 1 runs first, a PR whose run did not finish step 3 carries no risk label. The result field `label_published` (present only with the input on) and the output `label-published` are `true` only when step 3 completed.

Write requests go only to the label endpoints of the repository and PR number given as inputs, with label names taken from the constants above. Anyone with triage rights can still add a risk label by hand, so automation that acts on `low` must read the check's result for the exact head commit rather than trust the label.

## Evidence sent to Jev

Outside SDK mode, the default `metadata-diff` profile sends the PR title/description, changed paths/status (including previous paths for renames), additions/deletions per file and totals, and text diffs within the context budget. `score`, `build`, `score-pr` and the Python request/scoring APIs resolve omitted context options to this same input. Omitted and explicit profile options have the same request and cache identity at the current versions; context 7/rubric 4 intentionally invalidate the earlier evaluation cache. Selecting this input does not count as a passed holdout evaluation.

Binary and non-UTF-8 files contribute Git headers and per-side kinds, byte sizes and SHA-256 hashes. The request explicitly lists uninspected binary sides and includes readable text sides within the code budget. Binary-only PRs can be classified from this evidence; binary presence and zero textual line counts do not imply a risk class. Missing Git objects or incomplete binary metadata still leave the PR unclassified.

The default preserves all title/description text, paths, status, line counts and binary metadata. If needed, it compactly represents files as rows of shared columns, with sparse per-file details. It then cuts an ordered prefix of code fields at a Unicode-safe boundary. The final snippet may end mid-line; an explicit marker warns Jev that code is incomplete. Results record each omitted or shortened field and its original/included byte sizes. Captured empty descriptions are valid; unknown descriptions remain unavailable.

The request targets at most 30,000 `cl100k_base` tokens, leaving a 2,000-token reserve. Pinned `tiktoken==0.14.0` counts the serialized SDK request, including the question. This is a proxy: Jev's tokenizer and internal prompt rendering are not public, so it is not an exact Jev input count or a guarantee of fit. Actual provider usage remains separate. If an explicit context rejection still occurs, retry with half the bounded code prefix, then with metadata only. If protected metadata alone cannot fit, return `metadata_exceeds_budget` without a label. Missing source evidence still fails before trimming; trimming does not hide snapshot errors.

Complete files remain available through the explicit `metadata-files` experiment. That profile adds complete final contents of changed files; deleted files contribute their previous contents. Its fallback policy is:

1. Try that complete request first.
2. If the provider explicitly rejects the context size, remove the optional complete-file contents and try the same metadata plus complete diff.
3. If the essential diff is also rejected, persist `context_rejected` with no label. Do not silently classify only metadata or truncate the diff.

Missing optional file contents are recorded as omissions. The local serialized-request guard defaults to 1 MiB for `metadata-files` and `metadata-diff`; it is configurable with `--max-bytes`. The default trims code to meet both guards; `metadata-files` only reduces optional contents and remains unclassified if its complete diff exceeds the byte guard. Older experimental profiles keep their 64 KiB default. These byte guards are application settings, not model context limits.

TypeSafe documents 64K tokens for the complete Jev 1.13 request and 32K for state plus the longest question. With one Choice, the latter is the relevant documented constraint. The [Gateway catalog](https://ai-gateway.vercel.sh/v1/models) also advertises a 32,000-token context window for `typesafe-ai/jev`. See [Models](https://docs.typesafe.ai/models). Byte limits are separate from token limits.

Each context has a separate request hash, cache entry and attempt journal. Only an explicit size rejection changes the context; transient errors retry the same request. The default has at most four provider calls across all context stages, waiting 10, 20 and 30 seconds before attempts two, three and four. A resumed journal counts existing attempts toward this bound. The legacy `metadata-files` experiment retains separate budgets of up to four attempts for each of its two stages. Results preserve attempts, latency, known/unknown costs and the final selected stage. An unknown cost for a rejected attempt keeps the PR's total cost unknown even if the fallback succeeds.

## Evaluation for a light-review workflow

Prioritize precision among predicted low PRs, the number of expected medium/high PRs incorrectly labeled low, and the fraction of PRs receiving a usable result. Keep overall macro-F1, high recall and false-high counts as well. Accuracy alone can favor a model that misses the few high examples.

The classifier returns a risk label, class probabilities and provider confidence. Confidence describes the distribution, not the severity or a guaranteed incident rate. A future review-routing policy should set thresholds using independent evaluation data; this offline tool does not implement author permissions, branch protection, merging or a GitHub App.

The v2 experiment reuses exact CLI/Snuba snapshots and audits the existing assistant judgments against the clarified boundaries before generating new predictions. It preserves the prior dataset and records that this is an alignment audit after earlier experiments, not a new blinded or human-labeled benchmark. The reserved holdout stays untouched.

Reports include two separate `low_review` threshold curves: `probability_low` and `provider_confidence`. Only valid predictions labeled low qualify. Fixed inclusive thresholds of 0, 0.5, 0.7, 0.8, 0.9, 0.95 and 0.99 show the tradeoff between the fraction of all PRs eligible for lighter review and the precision against reviewed references. Reported errors distinguish medium-to-low from high-to-low, and precision includes a 95% Wilson interval. Missing confidence is unknown; unreviewed cases can contribute coverage but cannot count as correct. A high-confidence mistake remains a mistake. These curves do not pick a threshold, change labels or enable merging.

Eligibility also requires matching a known expected base/head snapshot. Predictions with no verifiable expected revision count as `snapshot_unverified` in low-review coverage and cannot qualify for lighter review, even when their label and probabilities are otherwise valid.

The `low-review-v1` diagnostic cohort adds 20 CLI and 20 Snuba PRs, selected by deterministic hashes within five predeclared path/title categories before reading source or assigning labels. It excludes every original reviewed and representative ID and stays chronologically before each repository's reserved holdout. Assistant references are frozen before inference under the unchanged rubric 2. Because the sample deliberately targets review boundaries, its class mix and low fraction do not estimate repository-wide rates. It remains development evidence from assistant judgments, separate from the earlier 40 cases and from human-reviewed holdout acceptance.

## Sources and scope of their advice

The official [use-case map](https://docs.typesafe.ai/concepts/use-case-map) includes semantic code linting. The documentation and cookbooks reviewed do not prescribe a PR-risk rubric or establish that complete files outperform diffs; this classifier's boundaries and fallback policy are adaptations to this project's use case.

[Jev 1.13 limitations](https://docs.typesafe.ai/model-jaggedness/jev-1.13) recommend literal conditions, explicit boundaries, relevant context and testing adversarial content. They also note that irrelevant context and indirect reasoning can reduce accuracy. We keep counts in Python, treat PR text as untrusted evidence and measure the effect of additional source context.

[Confidence](https://docs.typesafe.ai/confidence) distinguishes uncertainty from the chosen class and recommends determining action thresholds from the consequences and observed performance of the particular workflow. No universal merge-confidence threshold is supplied.

The community `jev-review` repository distinguishes review priority from severity and selects concrete evidence and a failure mechanism before estimating impact. Its bug-finding workflow is useful context, but “no supported issue found” must not become our definition of low PR risk.
