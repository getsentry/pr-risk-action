# PR Risk Action

Classify a pull request as `low`, `medium` or `high` with Typesafe AI Jev through Vercel AI Gateway. Each request includes the PR title and description, changed paths/status, additions/deletions per file and in total, and the complete Git diff. Inference does not need historical PRs or a trained model.

This is an advisory evaluation candidate. It does not authorize merging, replace existing checks, apply labels, or change branch protection. Probabilities describe the risk classes, not incident likelihood.

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

No target checkout or target dependency installation is needed. The Action loads its trusted Python source through `PYTHONPATH`, installs the Node worker dependencies (AI SDK `7.0.106`), downloads Git objects into an isolated bare repository and reads the exact base/head revisions. PR files are never executed. Git downloads do not receive the Gateway key or GitHub token. This initial integration supports public GitHub repositories, including PRs from forks.

The Action verifies that the PR has not moved before publishing a classification. A moved PR is reported as unclassified. `expected-head-sha` binds automatic runs to their triggering revision; leave it empty for a manual run against the current PR.

## Result

The Action writes a job summary and a JSON result (`risk-pr-result.json` by default), and exposes `risk-label` and `status` outputs. The JSON records the exact revision, class probabilities, available provider confidence, request hash, versions, usage, attempts, latency and known or estimated cost.

Missing evidence, unsupported binary diffs, the local size guard or provider context rejection produce an explicit status with no risk label. Unclassified results fail the advisory job while retaining the JSON and summary. Essential diffs are never truncated. The default guard is 1 MiB of serialized request, not the model token window. Automatic retries are bounded and individually accounted. Unknown usage or cost stays unknown.

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
