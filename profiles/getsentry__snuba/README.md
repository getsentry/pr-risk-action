# getsentry/snuba Profile

This profile calibrates PR risk scoring for `getsentry/snuba`.

- `pr-history.jsonl.gz`: 4,000 recent public PR rows fetched from the GitHub API with reviews skipped.
- Date range: 2023-03-22 to 2026-06-23.
- Model: shared selected-static logistic model trained on the expanded multi-repo dataset.

This is an experimental profile for Action rollout. It should be refreshed with a larger historical slice before treating the labels as stable.
