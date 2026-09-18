"""Public CLI: exact snapshots, one Jev decision, and isolated evaluation.

Legacy scoring helpers stay available only to reproduce frozen benchmarks.
Unvalidated models require explicit candidate mode; a held-out gate controls release.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

from . import cli as collection
from .features import load_raw_dataset
from .github import GitHubClient, RepoRef, utc_now_iso, write_jsonl
from .scoring import write_json


def _read(path: Optional[str]):
    return load_raw_dataset(str(path)) if path else []


def _maps(values: Sequence[str]) -> Dict[str, str]:
    result = {}
    for value in values:
        if "=" not in value:
            raise ValueError("--git-repo must be owner/repo=/path")
        repo, path = value.split("=", 1)
        RepoRef.parse(repo)
        result[repo] = path
    return result


def _load_env() -> None:
    """Read only the optional local Gateway key; never overwrite the environment."""
    path = Path(".env")
    if os.environ.get("AI_GATEWAY_API_KEY") or not path.is_file():
        return
    for line in path.read_text().splitlines():
        key, sep, value = line.strip().partition("=")
        if sep and key == "AI_GATEWAY_API_KEY":
            os.environ[key] = value.strip().strip("\"'")


def _inference_arguments(parser: argparse.ArgumentParser) -> None:
    from .jev import DEFAULT_INPUT_PROFILE, INPUT_PROFILES
    parser.add_argument("--variant", choices=("A", "B", "C"), help="Explicit legacy context for reproducing A/B/C experiments")
    parser.add_argument("--input-profile", choices=INPUT_PROFILES,
                        help=f"Input fields (default: {DEFAULT_INPUT_PROFILE}); other profiles are optional experiments")
    parser.add_argument("--max-bytes", type=int, help="Local serialized-request guard: 1 MiB for metadata-diff/files, 64 KiB for other profiles; not the model token limit")
    parser.add_argument("--candidate", action="store_true", help="Explicitly run an unvalidated candidate")
    parser.add_argument("--acceptance", help="Accepted held-out evaluation receipt")
    parser.add_argument("--dry-run", action="store_true", help="Prepare requests without inference or credentials")


def _authorize(args) -> None:
    """Allow candidate/dry-run, otherwise require the exact held-out configuration."""
    from .jev import _configuration
    configuration = _configuration(args.variant, args.max_bytes, args.input_profile)
    args.variant, args.max_bytes = configuration["variant"], configuration["max_bytes"]
    args.input_profile = configuration.get("input_profile")
    if args.candidate or args.dry_run:
        return
    if not args.acceptance:
        raise ValueError("Jev has not passed the held-out gate. Use --candidate for offline evaluation.")
    receipt = json.loads(Path(args.acceptance).read_text())
    receipt = receipt.get("report", receipt)
    gate = receipt.get("acceptance", {})
    if not gate.get("accepted"):
        raise ValueError("Acceptance receipt does not pass the held-out gate")
    from .jev import MODEL, VERSIONS
    config = receipt.get("configuration") or {}
    current = {"model": MODEL, "variant": args.variant, "max_bytes": args.max_bytes, "versions": VERSIONS}
    if args.input_profile:
        current["input_profile"] = args.input_profile
    if config != current or receipt.get("split") != "holdout":
        raise ValueError("Requested context differs from the accepted configuration")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="risk-pr", description="Jev PR risk datasets and evaluation")
    sub = parser.add_subparsers(dest="command", required=True)
    backfill = sub.add_parser("backfill", help="Collect raw GitHub PRs")
    build = sub.add_parser("build", help="Collect PRs, reconstruct snapshots, and run Jev")
    for command in (backfill, build):
        command.add_argument("--repo", action="append", required=True)
        command.add_argument("--out", default="data/raw" if command is backfill else "data/jev-build")
        command.add_argument("--since")
        command.add_argument("--until")
        command.add_argument("--months", type=int, default=12)
        command.add_argument("--max-prs", type=int)
        command.add_argument("--skip-reviews", action="store_true")
        command.add_argument("--resume", action="store_true")
        command.add_argument("--refresh", action="store_true")
        command.add_argument("--sleep", type=float, default=0)
    backfill.set_defaults(func=collection.cmd_backfill)
    build.add_argument("--git-repo", action="append", required=True)
    _inference_arguments(build)
    build.set_defaults(func=cmd_build)

    git = sub.add_parser("git-backfill", help="Collect merged PR metadata from Git")
    git.add_argument("--repo", action="append", required=True)
    git.add_argument("--git-repo", action="append", required=True)
    git.add_argument("--git-ref", default="origin/master")
    git.add_argument("--since")
    git.add_argument("--until")
    git.add_argument("--months", type=int, default=24)
    git.add_argument("--max-prs", type=int)
    git.add_argument("--out", default="data/raw-git")
    git.set_defaults(func=collection.cmd_git_backfill)

    features = sub.add_parser("features", help="Extract legacy features for benchmark comparison only")
    features.add_argument("--input", required=True)
    features.add_argument("--out", default="data/processed")
    features.set_defaults(func=collection.cmd_features)
    survey = sub.add_parser("survey", help="Estimate raw collection size")
    survey.add_argument("--repo", required=True)
    survey.add_argument("--since")
    survey.add_argument("--until")
    survey.add_argument("--months", type=int, default=12)
    survey.add_argument("--git-repo")
    survey.add_argument("--git-ref", default="origin/master")
    survey.set_defaults(func=collection.cmd_survey)

    prepare = sub.add_parser("prepare-dataset", help="Freeze sampled snapshots and blind review packets")
    prepare.add_argument("--input", action="append", required=True)
    prepare.add_argument("--git-repo", action="append", required=True)
    prepare.add_argument("--out", default="data/jev-v1")
    prepare.add_argument("--seed", default="jev-v1")
    prepare.add_argument("--reviewed-per-repo", type=int, default=30)
    prepare.add_argument("--representative-per-repo", type=int, default=250)
    prepare.add_argument("--observation-cutoff")
    prepare.add_argument("--refresh-metadata", action="store_true", help="Verify selected Git-derived PR spans against GitHub; resume cached metadata")
    prepare.add_argument("--snapshot-metadata", help="Previously saved JSON mapping from example ID to authoritative PR metadata")
    prepare.set_defaults(func=cmd_prepare)

    input_eval = sub.add_parser("prepare-input-eval", help="Export source-complete dev cases with separate assistant risk references")
    input_eval.add_argument("--snapshots", required=True)
    input_eval.add_argument("--references", required=True)
    input_eval.add_argument("--raw", action="append", required=True)
    input_eval.add_argument("--repo", action="append", required=True)
    input_eval.add_argument("--out", required=True)
    input_eval.set_defaults(func=cmd_prepare_input_eval)

    freeze = sub.add_parser("freeze-baselines", help="Freeze legacy predictions for the selected snapshots")
    freeze.add_argument("--dataset", required=True)
    freeze.add_argument("--input", action="append", required=True)
    freeze.add_argument("--model", required=True)
    freeze.set_defaults(func=cmd_freeze)

    score = sub.add_parser("score", help="Score exact snapshots with Jev")
    source = score.add_mutually_exclusive_group(required=True)
    source.add_argument("--input")
    source.add_argument("--dataset")
    score.add_argument("--split", choices=("dev", "representative", "holdout"), default="dev")
    score.add_argument("--selection", help="Locked development selection, required for holdout")
    score.add_argument("--out", required=True)
    _inference_arguments(score)
    score.set_defaults(func=cmd_score)

    single = sub.add_parser("score-pr", help="Score one PR from its exact Git revisions")
    single.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY"), required=not os.environ.get("GITHUB_REPOSITORY"))
    single.add_argument("--pr", type=int, required=True)
    single.add_argument("--git-repo", required=True)
    single.add_argument("--out", default="risk-pr-result.json")
    single.add_argument("--summary-file")
    single.add_argument("--cache-dir", default=".cache/jev/single")
    _inference_arguments(single)
    single.set_defaults(func=cmd_score_pr)

    evaluate = sub.add_parser("evaluate", help="Evaluate development or representative predictions")
    evaluate.add_argument("--dataset", required=True)
    evaluate.add_argument("--predictions", required=True)
    evaluate.add_argument("--split", choices=("dev", "representative"), default="dev")
    evaluate.add_argument("--assisted-labels", help="Agent proposal JSONL for a dev-only comparison; cannot select or promote a candidate")
    evaluate.add_argument("--out", required=True)
    evaluate.set_defaults(func=cmd_evaluate)
    select = sub.add_parser("select-candidate", help="Lock the configuration selected on development labels")
    select.add_argument("--report", action="append", required=True)
    select.add_argument("--out", required=True)
    select.set_defaults(func=cmd_select)
    held = sub.add_parser("evaluate-holdout", help="Evaluate the locked candidate once on the reserved cohort")
    held.add_argument("--dataset", required=True)
    held.add_argument("--predictions", required=True)
    held.add_argument("--selection", required=True)
    held.add_argument("--out", required=True)
    held.set_defaults(func=cmd_holdout)
    return parser


def _manifest(dataset: str) -> Dict[str, Any]:
    return json.loads((Path(dataset) / "manifest.json").read_text())


def cmd_prepare(args) -> None:
    from .dataset import prepare_dataset, refresh_snapshot_metadata, select_dataset
    metadata = json.loads(Path(args.snapshot_metadata).read_text()) if args.snapshot_metadata else None
    if args.refresh_metadata:
        rows = [row for path in args.input for row in _read(path)]
        selected = select_dataset(rows, seed=args.seed, reviewed_per_repo=args.reviewed_per_repo,
                                  representative_per_repo=args.representative_per_repo)
        metadata = refresh_snapshot_metadata(rows, selected, args.out, GitHubClient())
    result = prepare_dataset(args.input, _maps(args.git_repo), args.out, seed=args.seed,
                             reviewed_per_repo=args.reviewed_per_repo,
                             representative_per_repo=args.representative_per_repo,
                             observation_cutoff=args.observation_cutoff, snapshot_metadata=metadata)
    print(json.dumps({"out": args.out, "snapshot_counts": result["snapshot_counts"],
                     "shortfalls": result["shortfalls"]}, indent=2))


def cmd_prepare_input_eval(args) -> None:
    from .input_eval import prepare_input_eval
    manifest = prepare_input_eval(_read(args.snapshots), _read(args.references),
                                  [row for path in args.raw for row in _read(path)], args.repo, args.out)
    print(json.dumps({key: manifest[key] for key in ("cases", "risk_distribution", "description_availability", "cases_path")}, indent=2))


def cmd_freeze(args) -> None:
    from .baselines import freeze_baselines
    manifest = _manifest(args.dataset)
    snapshots = [row for path in manifest["inputs"].values() for row in _read(path)]
    raw = [row for path in args.input for row in _read(path)]
    print(json.dumps(freeze_baselines(snapshots, raw, Path(args.model), Path(args.dataset) / "baselines"), indent=2))


def _score(rows, args, out_dir):
    from .jev import build_request, score_snapshots
    _authorize(args)
    if args.dry_run:
        results = []
        for row in rows:
            prepared = build_request(row, args.variant, args.max_bytes, input_profile=args.input_profile)
            results.append({"example_id": row["example_id"], "status": prepared["status"],
                            "request_hash": prepared.get("request_hash"), "risk_label": None,
                            "variant": args.variant, "omitted": prepared.get("omitted", [])})
            if args.input_profile:
                results[-1].update(input_profile=args.input_profile, configuration=prepared["configuration"],
                                   request_bytes=prepared.get("request_bytes"), request=prepared.get("request"))
                if "context_stage" in prepared:
                    results[-1]["context_stage"] = prepared["context_stage"]
        Path(out_dir).mkdir(parents=True, exist_ok=True)
        write_jsonl(str(Path(out_dir) / "prepared.jsonl"), results)
        return results
    _load_env()
    return score_snapshots(rows, out_dir, variant=args.variant, max_bytes=args.max_bytes,
                           candidate=args.candidate, input_profile=args.input_profile)


def cmd_score(args) -> None:
    rows = _read(_manifest(args.dataset)["inputs"][args.split] if args.dataset else args.input)
    if args.split == "holdout" or any(row.get("split") == "holdout" for row in rows):
        if not args.selection:
            raise ValueError("Holdout inference requires a locked --selection from development evaluation")
        selection = json.loads(Path(args.selection).read_text())
        from .evaluation import validate_selection
        config = validate_selection(selection, (row["example_id"] for row in rows))
        from .jev import MODEL, VERSIONS
        current = {"model": MODEL, "variant": config.get("variant"),
                   "max_bytes": config.get("max_bytes"), "versions": VERSIONS}
        if config.get("input_profile"):
            from .jev import INPUT_PROFILES
            if config["input_profile"] not in INPUT_PROFILES:
                raise ValueError("Unknown input profile in locked development configuration")
            current["input_profile"] = config["input_profile"]
        if config != current or config.get("variant") not in ("A", "B", "C") or not isinstance(config.get("max_bytes"), int) or config["max_bytes"] <= 0:
            raise ValueError("Installed engine differs from the locked development configuration")
        args.variant, args.max_bytes = config["variant"], config["max_bytes"]
        args.input_profile = config.get("input_profile")
    results = _score(rows, args, args.out)
    print(json.dumps({"examples": len(results), "out": args.out, "candidate": bool(args.candidate)}, indent=2))


def cmd_build(args) -> None:
    from .dataset import prepare_snapshot
    _authorize(args)
    repos = _maps(args.git_repo)
    client = GitHubClient(sleep_seconds=args.sleep)
    rows = []
    for slug in args.repo:
        if slug not in repos:
            raise ValueError(f"Missing Git checkout for {slug}")
        path = collection.backfill_repo(client, RepoRef.parse(slug), Path(args.out) / "raw",
                                        collection.parse_since(args.since, args.months),
                                        collection.parse_until(args.until), args)
        for raw in _read(str(path)):
            snapshot = prepare_snapshot(raw, repos[slug])
            rows.append(snapshot)
    Path(args.out).mkdir(parents=True, exist_ok=True)
    write_jsonl(str(Path(args.out) / "snapshots.jsonl"), rows)
    results = _score(rows, args, Path(args.out) / "run")
    from .evaluation import evaluate_run
    write_json(Path(args.out) / "evaluation.json", evaluate_run(results, examples=rows, split="representative"))
    print(f"Prepared and processed {len(rows)} PRs in {args.out}")


def risk_summary(result: Dict[str, Any]) -> str:
    lines = ["# PR risk", "", f"Status: `{result.get('status')}`",
             f"Risk: `{result.get('risk_label') or 'not classified'}`",
             f"Context: `{result.get('input_profile') or result.get('variant', 'unknown')}`"]
    if result.get("probabilities"):
        lines.append("Class probabilities: " + ", ".join(f"{key}={value:.2f}" for key, value in result["probabilities"].items()))
    lines += ["", "Class probabilities describe the risk rubric, not incident likelihood."]
    return "\n".join(lines) + "\n"


def cmd_score_pr(args) -> None:
    from .dataset import prepare_snapshot
    _authorize(args)
    raw = collection.fetch_pr_row(GitHubClient(), RepoRef.parse(args.repo), args.pr, utc_now_iso(), skip_reviews=True)
    snapshot = prepare_snapshot(raw, args.git_repo)
    result = _score([snapshot], args, args.cache_dir)[0]
    result["candidate"] = bool(args.candidate)
    write_json(Path(args.out), result)
    summary = risk_summary(result)
    summary_path = args.summary_file or os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        Path(summary_path).write_text(summary)
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as handle:
            handle.write(f"risk_label={result.get('risk_label') or ''}\nstatus={result.get('status')}\n")
    print(summary)


def _evaluation_inputs(dataset: str):
    manifest = _manifest(dataset)
    examples = manifest["reviewed"] + manifest["representative"]
    baseline = Path(dataset) / "baselines" / "predictions.jsonl"
    return {"labels": _read(manifest["labels_path"]), "outcomes": _read(manifest["outcomes_path"]),
            "baselines": _read(str(baseline)) if baseline.exists() else [], "examples": examples}


def cmd_evaluate(args) -> None:
    from .evaluation import evaluate_assisted_run, evaluate_run
    if args.assisted_labels and args.split != "dev":
        raise ValueError("assisted evaluation is restricted to the dev split")
    manifest = _manifest(args.dataset)
    references = args.assisted_labels
    if manifest.get("dataset_kind") == "input_ablation":
        references = references or manifest.get("references_path")
        if not references:
            raise ValueError("input evaluation dataset is missing its assistant references")
    if references:
        if args.split != "dev":
            raise ValueError("assisted evaluation is restricted to the dev split")
        baseline = Path(args.dataset) / "baselines" / "predictions.jsonl"
        report = evaluate_assisted_run(
            _read(args.predictions), _read(references),
            baselines=_read(str(baseline)) if baseline.exists() else [],
            examples=manifest["reviewed"], split=args.split,
        )
    else:
        report = evaluate_run(_read(args.predictions), split=args.split, **_evaluation_inputs(args.dataset))
    write_json(Path(args.out), report)
    print(f"Evaluation written to {args.out}")


def cmd_select(args) -> None:
    from .evaluation import select_candidate
    selection = select_candidate([json.loads(Path(path).read_text()) for path in args.report])
    if selection.get("status") != "locked":
        raise ValueError(selection["reason"])
    destination = Path(args.out)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("x") as handle:
        json.dump(selection, handle, indent=2)
        handle.write("\n")
    print(f"Locked candidate written to {destination}")


def cmd_holdout(args) -> None:
    from .evaluation import evaluate_holdout_once
    report = evaluate_holdout_once(Path(args.dataset) / "evaluation", _read(args.predictions),
                                   selection=json.loads(Path(args.selection).read_text()),
                                   **_evaluation_inputs(args.dataset))
    write_json(Path(args.out), report)
    print(f"Held-out result written to {args.out}")


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        args.func(args)
    except (ValueError, OSError, RuntimeError) as exc:
        print(f"risk-pr: {exc}", file=sys.stderr)
        return 1
    return 0
