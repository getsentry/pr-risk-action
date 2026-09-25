# PR Risk Action

Classify a pull request as `low`, `medium` or `high` with Typesafe AI Jev through Vercel AI Gateway. Each request includes the PR title and description, changed paths/status, additions/deletions per file and in total, and Git diffs within the context budget. Inference does not need historical PRs or a trained model.

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

No target checkout or target dependency installation is needed. The Action loads its trusted Python source through `PYTHONPATH`, installs the trusted Python dependencies (`tiktoken==0.14.0`) and Node worker dependencies (AI SDK `7.0.106`), preloads the token estimator without the Gateway key, downloads Git objects into an isolated bare repository and reads the exact base/head revisions. PR files are never executed. Git downloads do not receive the Gateway key or GitHub token. This initial integration supports public GitHub repositories, including PRs from forks.

The Action verifies that the PR has not moved before publishing a classification. A moved PR is reported as unclassified. `expected-head-sha` binds automatic runs to their triggering revision; leave it empty for a manual run against the current PR.

## Result

The Action writes a job summary and a JSON result (`risk-pr-result.json` by default), and exposes `risk-label` and `status` outputs. The JSON records the exact revision, class probabilities, available provider confidence, request hash, versions, usage, attempts, latency and known or estimated cost.

Binary and non-UTF-8 changes are supported, including PRs containing only binaries. Jev receives paths, change status, Git headers, and the size and SHA-256 of each available side. Binary bytes and encoded Git payloads are excluded; readable text sides of text/binary conversions share the code context budget. The request and JSON result identify the binary sides whose contents were not inspected. Binary presence alone does not impose a risk class.

The default context keeps the full title, description, file inventory and counts, then fits a deterministic prefix of code diffs. It uses a **30,000-token estimate plus 2,000-token reserve** for Jev's 32,000-token window. The estimator is `cl100k_base`, not Jev's unpublished tokenizer; actual provider usage is recorded separately. Large file inventories use shared columns when needed, preserving all metadata. Truncated code is marked in the model input, job summary and JSON, with per-file omissions. The configurable 1 MiB serialized-request guard remains an additional byte limit.

There are **four provider attempts total**, with waits of **10, 20 and 30 seconds** before attempts two, three and four. After a transient error (including HTTP 503), the standard profile reduces its estimated input budget: **30k → 8k → 4k → 4k**. Requests that already fit retain their code. These are proxy token counts for the whole request, not Jev's exact tokenizer. Title, description, all file paths and line counts remain protected; if that metadata cannot fit a reduced budget, classification stops with `metadata_exceeds_budget` and no label.

An explicit provider context rejection additionally reduces the code prefix by half, then removes code if necessary, within the same four-attempt budget. Each context/budget configuration has its own request hash and journal, even if a small payload is unchanged. Cache/resume preserves all attempts, costs and the overall attempt number without resetting the retry budget. The legacy `metadata-files` evaluation profile retains its existing retry behavior.

Missing Git objects or required metadata, invalid binary metadata, metadata that cannot fit even after compaction, or exhausted provider attempts produce an explicit status with no risk label. Unclassified results fail the advisory job while retaining the JSON and summary. Unknown usage or cost stays unknown. Truncation does not impose a risk class, and quality on partially visible changes still needs evaluation.

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
