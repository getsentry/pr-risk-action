# PR Risk Action

Classify a pull request as `low`, `medium` or `high` with Typesafe AI Jev through Vercel AI Gateway. Outside SDK mode (see [SDK repositories](#sdk-repositories)), each request includes the PR title and description, changed paths/status, additions/deletions per file and in total, and Git diffs within the context budget. Inference does not need historical PRs or a trained model.

This is an advisory evaluation candidate. It does not authorize merging, replace existing checks, or change branch protection, and it applies labels only when `publish-label` is `true` (see [Publishing the risk label](#publishing-the-risk-label)). Probabilities describe the risk classes, not incident likelihood.

## Usage

Add `AI_GATEWAY_API_KEY` as an Actions secret in the caller repository. Pin the Action to a reviewed commit containing the Jev implementation; `v0` still refers to the previous logistic/rule implementation.

```yaml
name: PR Risk (Jev)

on:
  pull_request_target:
    types: [opened, synchronize, reopened, ready_for_review, edited]

permissions:
  contents: read
  pull-requests: read

concurrency:
  group: pr-risk-${{ github.event.pull_request.number }}
  cancel-in-progress: true

jobs:
  risk:
    runs-on: ubuntu-latest
    timeout-minutes: 10
    if: github.event.pull_request.draft == false
    steps:
      - name: Classify PR risk
        uses: getsentry/pr-risk-action@<reviewed-jev-commit-sha>
        env:
          AI_GATEWAY_API_KEY: ${{ secrets.AI_GATEWAY_API_KEY }}
        with:
          repo: ${{ github.repository }}
          pr-number: ${{ github.event.pull_request.number }}
          expected-head-sha: ${{ github.event.pull_request.head.sha }}
          result-path: risk-pr-result.json

      - name: Upload risk result
        if: always()
        uses: actions/upload-artifact@v7
        with:
          name: pr-risk-result
          path: risk-pr-result.json
          retention-days: 30
```

No target checkout or target dependency installation is needed. The Action loads its trusted Python source through `PYTHONPATH`, installs the trusted Python dependencies (`tiktoken==0.14.0`) and Node worker dependencies (AI SDK `7.0.106`), preloads the token estimator without the Gateway key, downloads Git objects into an isolated bare repository and reads the exact base/head revisions. PR files are never executed. Git downloads and the scorer's Git reads receive neither the Gateway key nor the GitHub token, and the Jev worker does not receive the GitHub token. The nested `actions/setup-python` and `actions/setup-node` are pinned to commit SHAs. This initial integration supports public GitHub repositories, including PRs from forks.

The Action verifies that the PR has not moved before publishing a classification. A moved PR is reported as unclassified. `expected-head-sha` binds automatic runs to their triggering revision; leave it empty for a manual run against the current PR.

Inputs: `repo`, `pr-number`, `expected-head-sha`, `result-path` (default `risk-pr-result.json`) and `publish-label` (default `false`).

### Publishing the risk label

With `publish-label: true` the Action owns the PR's `risk: low`, `risk: medium` and `risk: high` labels, and the caller job needs `pull-requests: write`. With the default (`false`) the Action makes no write call to GitHub and the read-only permissions shown above are enough.

Use this caller workflow when publishing labels:

```yaml
name: PR Risk (Jev)

on:
  pull_request_target:
    types: [opened, synchronize, reopened, ready_for_review, edited, converted_to_draft]

permissions: {}

concurrency:
  group: pr-risk-${{ github.event.pull_request.number }}
  cancel-in-progress: false   # every started run reaches its label clear

jobs:
  risk:
    runs-on: ubuntu-latest
    timeout-minutes: 10
    permissions:
      contents: read
      pull-requests: write    # this job only; no issues, contents or actions write
    steps:
      - id: risk
        uses: getsentry/pr-risk-action@<full commit SHA>
        env:
          AI_GATEWAY_API_KEY: ${{ secrets.AI_GATEWAY_API_KEY }}
        with:
          repo: ${{ github.repository }}
          pr-number: ${{ github.event.pull_request.number }}
          expected-head-sha: ${{ github.event.pull_request.head.sha }}
          publish-label: true

      - name: Upload risk result
        if: always()
        uses: actions/upload-artifact@<full commit SHA>
        with:
          name: pr-risk-result
          path: risk-pr-result.json
          retention-days: 30
```

- `permissions: {}` at the top and the two permissions on the one job. Do not set the GitHub token or the Gateway key at workflow or job level; keep the key on the one step.
- `cancel-in-progress: false`, so a run that has started always reaches its label clear. A run overtaken by a push ends `pr_changed`, and the newest pending run scores.
- Keep `edited`: it is the only event fired when the author changes the base branch.
- No `if: github.event.pull_request.draft == false`: drafts are scored, and `converted_to_draft` starts a run, so a draft never keeps a label from an earlier revision.
- Pin the Action to a full commit SHA. Do not add `actions/checkout`, and never check out the PR head in this job.
- Pre-create the three labels so the job never needs to create one, and remove the caller's own label script so only one writer exists.
- Keep `workflow_dispatch` out of this workflow if the result will gate review; it runs without `expected-head-sha`.

The Action's first step, before Python, Node or any dependency is set up, removes all three risk labels using the runner's own `python3` and the standard library. If the inputs are invalid or it cannot remove them, it adds one line naming the status to the job summary, the job fails and nothing is scored. The scoring step clears them again before scoring. The Action then adds the final `risk-label` (never the model label) only when the result is `ok`, the repository has an SDK fact sheet (the result has `model_label`), and the PR is still open at the scored base and head; it removes the other two risk labels just before adding and checks the PR again after adding. A repository without a fact sheet gets no label: the result records `label_skipped: "no_fact_sheet"` and the summary says so. A run that ends in any other status leaves the PR without a risk label: `label_error` when a label request fails, `pr_changed` when the PR moved or closed. If removing a label the run just added fails three times, the status is `label_error`, the result records `label_cleanup_failed: true`, and the summary says a risk label may still be on the PR until the next run clears it. See [Label publishing](jev-risk-classification.md#label-publishing).

**Replacing a label script in the caller workflow.** Delete the caller's own label step, switch to the workflow above and set `publish-label: true`. The Action clears the old label at the start of every run.

### The label is advisory, not an authorization

A risk label is a hint for reviewers. Triage-role users and any workflow with `issues: write` or `pull-requests: write` can set it, and it can outlive the revision it was computed for (see the limits below). Never let a label approve, merge or skip review of a PR.

What a skip-review or auto-merge consumer must check. Every check uses the step outputs or the JSON result of the trusted run, compared with the PR's current state from the GitHub API:

1. Never read the label.
2. Take the outputs, or the uploaded `pr-risk-result` artifact, from the trusted workflow run itself: event `pull_request_target`, this workflow file, on the default branch, found by its run ID. Step outputs (`steps.risk.outputs.*`) are readable only inside that workflow, so a consumer outside it, such as a bot or another workflow, must use the `pr-risk-result` artifact of that run. Do not look up artifacts by name alone: a fork `pull_request` run can upload an artifact with the same name. A successful job only means status `ok`, not `low`.
3. Require `status == 'ok'`, `risk-label == 'low'` and a non-empty `model-label` (JSON: `status`, `risk_label`, `model_label`). With `publish-label: true`, also require `label-published == 'true'` (JSON: `label_published`).
4. Require `head-sha` and `base-sha` (JSON: `snapshot.source_head_sha`, `snapshot.source_base_sha`) to equal the PR's current head and base, the PR's base ref to be the default branch, and the PR to be open and not a draft.
5. Merge or approve with `head-sha` pinned (the merge API's `sha` parameter), so a push between the check and the action fails.
6. Decide explicitly whether the author may be the only human involved. The author writes everything the classifier sees, so a `low` should reduce review effort, not replace the second reviewer.

A result is gated by the SDK rules when `model-label` is non-empty. `label-published == 'true'` additionally means the label on the PR was written by this run for the scored revision. `status` and `risk-label` alone say neither: for a repository without a fact sheet, `risk-label` may read `low` with no rules applied, and `model-label` is then empty.

Known limits:

- `pull_request_target` takes the workflow from the PR's base branch. Retargeting a PR to a base branch that does not have this workflow stops the check from running, so a label can stay on later, unscored commits until the PR is retargeted back.
- `pull-requests: write` also permits approving and commenting on pull requests. Repositories that will act on `low` should turn off "Allow GitHub Actions to create and approve pull requests", or split scoring and labelling into separate jobs so the scoring job holds only `pull-requests: read`.
- The label is published only for repositories with an SDK fact sheet under `sdk/repos/`.
- If a run is killed between steps, more than one risk label can be on a PR for a short time. Treat more than one risk label as no label.

**Cost.** Any GitHub user can trigger runs by opening or editing a PR, and each run can make up to four model calls. Use a dedicated Gateway key with a spend cap, and do not make this a required check.

## Result

The Action writes a job summary and a JSON result (`risk-pr-result.json` by default), and exposes these outputs: `risk-label` (the final label), `status`, `summary`, `label-published`, `model-label` (the model's label before the SDK rules; empty unless the result is an `ok` SDK-mode result), and `head-sha` and `base-sha` (the scored commits; empty unless the status is `ok`). The three new values are validated before they are written: a label from the three classes and 40-hex-character SHAs. `label-published` is `true` only when the Action added the risk label to the unchanged PR, and `false` otherwise, including when `publish-label` is off. With `publish-label: true` the JSON also carries `label_published`, and `label_skipped: "no_fact_sheet"` when an `ok` result got no label because the repository has no SDK fact sheet. The JSON records the exact revision, class probabilities, available provider confidence, request hash, versions, usage, attempts, latency and known or estimated cost.

Binary and non-UTF-8 changes are supported, including PRs containing only binaries. Jev receives paths, change status, Git headers, and the size and SHA-256 of each available side. Binary bytes and encoded Git payloads are excluded; readable text sides of text/binary conversions share the code context budget. The request and JSON result identify the binary sides whose contents were not inspected. Binary presence alone does not impose a risk class.

The default context keeps the full title, description, file inventory and counts, then fits a deterministic prefix of code diffs. It uses a **30,000-token estimate plus 2,000-token reserve** for Jev's 32,000-token window. The estimator is `cl100k_base`, not Jev's unpublished tokenizer; actual provider usage is recorded separately. Large file inventories use shared columns when needed, preserving all metadata. Truncated code is marked in the model input, job summary and JSON, with per-file omissions. The configurable 1 MiB serialized-request guard remains an additional byte limit.

An explicit provider context rejection reduces the code prefix by half, then removes code if necessary. Transient errors retry the same request. There are **four provider attempts total**, with waits of **10, 20 and 30 seconds** before attempts two, three and four, including any context fallback. Each distinct request has its own hash and journal; all attempts contribute to accounting.

Missing Git objects or required metadata, a broken SDK fact sheet or rubric (`invalid_fact_sheet`), an Action checkout without its `sdk/` data (`missing_sdk_data`), invalid binary metadata, metadata that cannot fit even after compaction, or exhausted provider attempts produce an explicit status with no risk label. Unclassified results fail the advisory job while retaining the JSON and summary. Unknown usage or cost stays unknown. Truncation does not impose a risk class, and quality on partially visible changes still needs evaluation.

## SDK repositories

Repositories with a fact sheet under `sdk/repos/` in this Action are classified in SDK mode, which uses one shared rubric for Sentry SDKs, does not send the PR title or description, and records why the label was chosen. There is no input to set: adding or removing the fact sheet is what turns it on. Callers pick it up by moving to a newer pinned commit. See [SDK repositories](jev-risk-classification.md#sdk-repositories) for the rubric, rules, result fields and how to add a repository.

In SDK mode, read `risk-label` (or `risk_label` in the JSON), not any other label field. A fact sheet or rubric file that cannot be loaded gives the status `invalid_fact_sheet` and no label. A result whose status is not `ok` carries no label, model label, rules or mechanisms. Treat a `low` as gated by the SDK rules only when `model_label` is present in the result.

SDK mode applies to the default `metadata-diff` input profile, which is what the Action uses. Other experimental input profiles and legacy variants ignore fact sheets.

The `summary` output holds the same Markdown as the job summary. To post it as a PR comment, pass it through an environment variable and never put `${{ }}` inside a `run:` script, because the summary contains text derived from an untrusted pull request. This needs `pull-requests: write` in the caller workflow, which the Action itself needs only with `publish-label: true`.

```yaml
      - name: Comment risk summary
        env:
          GH_TOKEN: ${{ github.token }}
          PR_NUMBER: ${{ github.event.pull_request.number }}
          SUMMARY: ${{ steps.risk.outputs.summary }}
        run: gh pr comment "$PR_NUMBER" --repo "$GITHUB_REPOSITORY" --body "$SUMMARY"
```

Give the classify step `id: risk` for this to work.

## Development and evaluation

```bash
python -m pip install -e .
npm ci --ignore-scripts --prefix worker
python -m unittest discover -s tests
npm run check --prefix worker
npm test --prefix worker
```

Python owns exact snapshots, datasets and evaluation. The Node worker owns one Jev provider call per attempt. See [offline dataset and evaluation commands](evaluation.md) and [risk criteria and context behavior](jev-risk-classification.md).

Legacy helpers and bundled profiles remain only for reproducing prior benchmarks; public inference uses Jev. The CLI retains its explicit candidate/held-out acceptance distinction, and the Action runs in candidate mode. Selecting an input configuration and enabling this advisory check do not count as a passed holdout evaluation.
