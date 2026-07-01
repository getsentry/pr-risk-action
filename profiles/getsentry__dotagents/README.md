# getsentry/dotagents Profile

This profile calibrates PR risk scoring for `getsentry/dotagents`.

- `pr-history.jsonl.gz`: 99 public PR rows fetched from the GitHub API with reviews skipped.
- Date range: 2026-02-10 to 2026-06-30.
- Model: shared selected-static logistic model trained on the expanded multi-repo dataset.

This is an experimental profile for Action rollout. The repository is young, so the profile should be refreshed as more PR history accumulates.
