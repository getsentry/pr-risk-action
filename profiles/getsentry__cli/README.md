# getsentry/cli Profile

This profile calibrates PR risk scoring for `getsentry/cli`.

- `pr-history.jsonl.gz`: 937 PR rows.
- Date range: 2026-01-12 to 2026-06-23.
- Sources: 928 GitHub API rows and 9 local-git fallback rows from `/Users/bete/code/cli`.
- Model: shared selected-static logistic model trained on the expanded multi-repo dataset.

This is effectively all locally recoverable CLI history at this stage. Reviews are skipped for fallback rows.
