# getsentry/sentry-mcp Profile

This profile calibrates PR risk scoring for `getsentry/sentry-mcp`.

- `pr-history.jsonl.gz`: 817 PR rows.
- Date range: 2025-04-03 to 2026-06-23.
- Sources: 590 GitHub API rows and 227 local-git fallback rows from `/Users/bete/code/sentry-mcp`.
- Model: shared selected-static logistic model trained on the expanded multi-repo dataset.

GitHub search reports about 850 total PRs for this repository, so 2,000 PRs are not available. The remaining gap is mostly PRs that were not present in the existing API slice and are not recoverable from main-branch git history.
