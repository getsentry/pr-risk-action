# PR Risk Action

GitHub Action for scoring pull requests as `low`, `medium`, or `high` risk with the current deterministic/logistic PR risk MVP.

This repository intentionally holds the Action runtime so consumer repositories do not need to vendor the scorer. The first bundled profiles are for `getsentry/cli`, `getsentry/sentry`, `getsentry/sentry-mcp`, `getsentry/snuba`, `getsentry/junior`, `getsentry/dotagents`, and `getsentry/sentry-dotnet`.

## Usage

```yaml
name: PR Risk Experiment

on:
  pull_request_target:
    types: [opened, synchronize, reopened, ready_for_review]

permissions:
  contents: read
  pull-requests: write
  issues: write

jobs:
  risk:
    name: Score PR risk
    runs-on: ubuntu-latest
    if: github.event.pull_request.draft == false

    steps:
      - name: Score PR risk
        uses: getsentry/pr-risk-action@main
        with:
          repo: ${{ github.repository }}
          pr-number: ${{ github.event.pull_request.number }}
```

The Action writes a Markdown summary to `GITHUB_STEP_SUMMARY`, writes `risk-pr-result.json` by default, and exposes outputs such as `risk-label`, `logistic-risk-label`, and `rule-risk-label`.

By default it also keeps exactly one PR label in sync:

- `risk: low`
- `risk: medium`
- `risk: high`

The labels are created on demand. This requires `issues: write` in the caller workflow because pull request labels use GitHub's Issues API.

## Profiles

By default, the Action maps `owner/repo` to `profiles/owner__repo`.

The bundled repo profiles contain:

- `pr-history.jsonl.gz`: public historical PR rows used for repo-relative calibration.
- `model.json`: selected logistic model weights.

Available profiles:

- `getsentry__cli`
- `getsentry__sentry`
- `getsentry__sentry-mcp`
- `getsentry__snuba`
- `getsentry__junior`
- `getsentry__dotagents`
- `getsentry__sentry-dotnet`

Other repositories can either add a bundled profile here or pass explicit `history` and `model` paths.

## Safety

The Action does not check out or execute pull request code. It reads PR metadata and diffs through the GitHub API, then runs the scorer code from this Action repository. Keep that property if using `pull_request_target`.

This is an experimental advisory signal only. It should not block merges yet.
