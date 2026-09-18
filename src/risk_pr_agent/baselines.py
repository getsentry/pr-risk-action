"""Freeze the old engines against exact snapshots for offline comparison only."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any, Dict, Sequence

from .features import build_feature_rows
from .github import write_jsonl
from .modeling import apply_serialized_logistic_model
from .scoring import write_json


def _patch_line_counts(patch: str) -> tuple[int, int]:
    """Count unified-diff content, including literal ++/-- source lines."""
    added = deleted = 0
    in_hunk = False
    for line in patch.splitlines():
        if line.startswith("@@ "):
            in_hunk = True
        elif line.startswith("diff --git "):
            in_hunk = False
        elif in_hunk:
            added += int(line.startswith("+"))
            deleted += int(line.startswith("-"))
    return added, deleted


def freeze_baselines(snapshots: Sequence[Dict[str, Any]], raw_rows: Sequence[Dict[str, Any]],
                     model_path: Path, out_dir: Path) -> Dict[str, Any]:
    """Save immutable predictions; legacy features are never sent to Jev."""
    from risk_tagger.labeling import assign_labels

    out_dir = Path(out_dir)
    manifest_path = out_dir / "manifest.json"
    if manifest_path.exists() or (out_dir / "predictions.jsonl").exists():
        raise ValueError("Baseline already frozen; use its existing artifacts")
    selected = {row["example_id"]: row for row in snapshots if row.get("status") == "ready"}
    history = {}
    for original in raw_rows:
        row = copy.deepcopy(original)
        key = f"{row['repo']}#{row['number']}"
        snapshot = selected.get(key)
        if snapshot:
            files = []
            for file in snapshot["files"]:
                patch = file.get("patch") or ""
                # Snapshots produced by dataset.py contain exact counts. The
                # hunk-aware fallback supports earlier snapshot fixtures safely.
                added, deleted = _patch_line_counts(patch)
                if isinstance(file.get("additions"), int) and isinstance(file.get("deletions"), int):
                    added, deleted = file["additions"], file["deletions"]
                files.append({"filename": file["path"], "previous_filename": file.get("previous_path"),
                              "status": file["status"], "patch": patch, "additions": added,
                              "deletions": deleted, "changes": added + deleted})
            row["files"] = files
            row["metrics"] = {**row.get("metrics", {}), "changed_files": len(files),
                              "additions": sum(f["additions"] for f in files),
                              "deletions": sum(f["deletions"] for f in files)}
        history[key] = row
    features = []
    for repo in sorted({row["repo"] for row in history.values()}):
        features.extend(build_feature_rows([row for row in history.values() if row["repo"] == repo]))
    model_bytes = Path(model_path).read_bytes()
    model = json.loads(model_bytes)
    modeled = apply_serialized_logistic_model(features, model)
    # Preserve the retired live run-open/relabel behavior, which ranked the
    # current PR against the full supplied repository population.
    tagged = assign_labels(features, percentile_mode="global")
    predictions = []
    for name, rows in (("score_pr", modeled), ("risk_tagger", tagged)):
        for row in rows:
            key = f"{row['repo']}#{row['number']}"
            if key not in selected:
                continue
            prediction = row.get("prediction", {})
            label = (prediction.get("final_risk_label") or prediction.get("logistic_risk_label")) if name == "score_pr" else row["risk"]["label"]
            predictions.append({"schema_version": 2, "example_id": key, "repo": row["repo"],
                                "number": row["number"], "baseline": name, "risk_label": label,
                                "snapshot": selected[key]["snapshot"], "status": "ok"})
    out_dir.mkdir(parents=True, exist_ok=True)
    write_jsonl(str(out_dir / "predictions.jsonl"), predictions)
    source_hashes = {}
    for folder in (Path(__file__).parent, Path(__file__).resolve().parents[2] / "risk_tagger"):
        for path in sorted(folder.glob("*.py")):
            source_hashes[str(path.relative_to(Path(__file__).resolve().parents[2]))] = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest = {"schema_version": 2, "history_rows": len(history), "predictions": len(predictions),
                "ready_snapshots": len(selected), "model_sha256": hashlib.sha256(model_bytes).hexdigest(),
                "percentile_modes": {"score_pr": model.get("percentile_mode", "as_of"), "risk_tagger": "global"},
                "population": "complete supplied history per repository, with selected snapshot diffs substituted",
                "sources": source_hashes, "purpose": "frozen offline comparison; not an inference backend"}
    write_json(manifest_path, manifest)
    return manifest
