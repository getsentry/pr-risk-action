# getsentry/sentry-dotnet Profile

This profile calibrates PR risk scoring for `getsentry/sentry-dotnet`.

- `pr-history.jsonl.gz`: 3,286 public PR rows fetched from the GitHub API with reviews skipped.
- Date range: 2018-05-22 to 2026-07-01.
- Model: shared selected-static logistic model trained on the expanded multi-repo dataset.

This profile includes the full public pull request history returned by GitHub's paginated pulls API at generation time.
