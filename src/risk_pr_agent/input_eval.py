"""Portable, source-complete development cases for input ablation experiments."""

from __future__ import annotations

import copy
import hashlib
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from .dataset import example_id
from .evaluation import RISK_LABELS, _assisted_label, _index, _revision
from .github import pr_metadata, write_jsonl


def prepare_input_eval(snapshots, references, raw_rows, repos, out_dir):
    """Export immutable dev inputs and expected assistant judgments separately.

    cases.jsonl is self-contained for inspection or another evaluation runner.
    inputs-dev.jsonl is the model-facing copy; references.jsonl never enters it.
    """
    wanted = set(repos)
    if not wanted:
        raise ValueError("at least one repository is required")
    selected = [row for row in snapshots if row.get("repo") in wanted and row.get("split") == "dev"]
    if {row["repo"] for row in selected} != wanted:
        raise ValueError("each requested repository must have development snapshots")
    indexed = _index(selected, "snapshot")
    reviewed = _index(references, "reference")
    raw_by_id = {}
    for row in raw_rows:
        identifier = example_id(row)
        if identifier not in indexed:
            continue
        previous = raw_by_id.get(identifier)
        # A synthetic Git row must never replace a captured PR description.
        priority = lambda value: (pr_metadata(value)["description_available"], value.get("fetched_at") or "")
        if previous is None or priority(row) > priority(previous):
            raw_by_id[identifier] = row
    inputs, labels, cases, examples = [], [], [], []
    file_keys = ("path", "previous_path", "status", "patch", "binary", "before", "after", "additions", "deletions")
    for identifier, original in sorted(indexed.items()):
        reference = reviewed.get(identifier, {})
        if reference.get("source") != "agent" or reference.get("review_status") != "proposed" or reference.get("split") != "dev":
            raise ValueError(f"missing assistant development review: {identifier}")
        if _revision(original) is None or _revision(reference) != _revision(original):
            raise ValueError(f"review revision differs from the snapshot: {identifier}")
        label = reference.get("proposed_risk_label")
        if label is not None and (label not in RISK_LABELS or _assisted_label(reference) is None):
            raise ValueError(f"invalid assistant reference: {identifier}")
        input_row = {key: copy.deepcopy(original[key]) for key in
                     ("schema_version", "snapshot_version", "example_id", "repo", "number", "split", "snapshot", "status", "missing", "strata") if key in original}
        input_row["files"] = [{key: copy.deepcopy(file[key]) for key in file_keys if key in file} for file in original.get("files", [])]
        for source, target in zip(original.get("files", []), input_row["files"]):
            metadata = source.get("content_metadata")
            if isinstance(metadata, dict):
                target["content_metadata"] = {
                    side: {key: copy.deepcopy(entry[key]) for key in ("kind", "size_bytes", "sha256") if key in entry}
                    for side in ("before", "after") if isinstance(entry := metadata.get(side), dict)
                }
        context = original.get("repository_context") or {}
        input_row["repository_context"] = {
            "tree": copy.deepcopy(context.get("tree", [])),
            "files": [{key: copy.deepcopy(file[key]) for key in ("path", "kind", "content") if key in file}
                      for file in context.get("files", [])],
            "omitted": copy.deepcopy(context.get("omitted", [])),
            "tree_omitted": context.get("tree_omitted", 0),
        }
        input_row["pr_metadata"] = pr_metadata(raw_by_id.get(identifier, {}))
        expected = {
            "risk_label": label, "rationale": reference.get("rationale"),
            "source": "agent", "reviewer": reference.get("reviewer"),
            "reviewed_at": reference.get("reviewed_at"), "rubric_version": reference.get("rubric_version"),
            "reviewer_confidence": reference.get("confidence"),
            "evidence": copy.deepcopy(reference.get("evidence", [])),
            "limitations": copy.deepcopy(reference.get("missing_context", [])),
        }
        cases.append({"schema_version": 1, "example_id": identifier, "input": input_row, "expected": expected})
        inputs.append(input_row)
        labels.append(copy.deepcopy(reference))
        examples.append({key: copy.deepcopy(input_row[key]) for key in ("example_id", "repo", "number", "split", "snapshot", "strata") if key in input_row})
    directory = Path(out_dir)
    if (directory / "manifest.json").exists():
        raise ValueError("evaluation dataset is frozen; choose a new output directory")
    directory.mkdir(parents=True, exist_ok=True)
    outputs = {"cases.jsonl": cases, "inputs-dev.jsonl": inputs, "references.jsonl": labels}
    for name, rows in outputs.items():
        write_jsonl(str(directory / name), rows)
    manifest = {
        "schema_version": 1, "dataset_kind": "input_ablation", "split": "dev",
        "created_at": datetime.now(timezone.utc).isoformat(), "reference_source": "agent",
        "repositories": sorted(wanted), "cases": len(cases),
        "risk_distribution": dict(Counter(row["expected"]["risk_label"] or "unreviewable" for row in cases)),
        "description_availability": dict(Counter("unavailable" if not row["pr_metadata"]["description_available"] else
                                                "nonempty" if row["pr_metadata"]["description"] else "observed_empty" for row in inputs)),
        "description_caveat": "Captured descriptions may have been edited after merge; they are not opening-time snapshots.",
        "cases_path": str((directory / "cases.jsonl").resolve()),
        "references_path": str((directory / "references.jsonl").resolve()),
        "inputs": {"dev": str((directory / "inputs-dev.jsonl").resolve())},
        "hashes": {name: hashlib.sha256((directory / name).read_bytes()).hexdigest() for name in outputs},
        "reviewed": examples, "representative": [],
    }
    (directory / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return manifest
