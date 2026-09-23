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

## Evidence sent to Jev

The default `metadata-diff` profile sends the PR title/description, changed paths/status (including previous paths for renames), additions/deletions per file and totals, and text diffs within the context budget. `score`, `build`, `score-pr` and the Python request/scoring APIs resolve omitted context options to this same input. Omitted and explicit profile options have the same request and cache identity at the current versions; context 7/rubric 4 intentionally invalidate the earlier evaluation cache. Selecting this input does not count as a passed holdout evaluation.

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
