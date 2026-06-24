# getsentry/sentry Profile

This profile calibrates PR risk scoring for `getsentry/sentry`.

- `pr-history.jsonl.gz`: 10,000 recent PR rows.
- Date range: 2026-01-21 to 2026-06-24.
- Sources: 7,252 GitHub API rows and 2,748 local-git fallback rows from `/Users/bete/code/sentry`.
- Model: shared selected-static logistic model trained on the expanded multi-repo dataset.

Reviews are skipped. Local-git fallback rows preserve diff/file history for calibration, but use merge-commit metadata rather than full GitHub PR metadata.
