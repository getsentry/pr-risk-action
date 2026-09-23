"""Reproducible Jev cohorts and commit-pinned, outcome-free model inputs.

Sampling uses paths and churn only; those strata are never risk labels. All Git
reads address immutable commits, never the worktree or the current branch tip.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from .features import (
    CODE_EXTENSIONS, CONFIG_EXTENSIONS, DOC_EXTENSIONS, LOCKFILES, REVERT_SHA_RE,
    extract_referenced_pr_numbers, is_followup_fix_pr, is_revert_pr, is_test_path,
)
from .github import RepoRef, normalize_pr, parse_github_time, pr_metadata, read_jsonl, utc_now_iso, write_jsonl


DATASET_VERSION = 2
SNAPSHOT_VERSION = 6
RUBRIC_VERSION = "jev-risk-v1"
DEFAULT_REPOS = ("getsentry/sentry", "getsentry/cli", "getsentry/sentry-mcp", "getsentry/snuba")
STRATA = ("docs", "tests", "mechanical", "auth", "migration", "config", "large", "functional")
MANIFESTS = {"package.json", "pyproject.toml", "cargo.toml", "go.mod", "composer.json", "gemfile", "requirements.txt"}


def example_id(row: Mapping[str, Any]) -> str:
    return f"{row['repo']}#{int(row['number'])}"


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def _canonical_rows(rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Prefer the newest fetch for duplicate PRs, independently of input order."""
    unique: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        if not row.get("repo") or row.get("number") is None:
            continue
        key = example_id(row)
        old = unique.get(key)
        if old is None or (row.get("fetched_at") or "", _digest(row)) > (old.get("fetched_at") or "", _digest(old)):
            unique[key] = row
    return list(unique.values())


def change_strata(row: Mapping[str, Any]) -> List[str]:
    """Content-independent sampling categories, not a risk scoring heuristic."""
    paths = [str(item.get("filename") or item.get("path") or "") for item in row.get("files", [])]
    paths = [path for path in paths if path]
    result = []
    if paths and all(PurePosixPath(path).suffix.lower() in DOC_EXTENSIONS or "docs" in PurePosixPath(path).parts for path in paths):
        result.append("docs")
    if paths and all(is_test_path(path) for path in paths):
        result.append("tests")
    if paths and all(PurePosixPath(path).name.lower() in LOCKFILES or any(part in {"generated", "vendor"} for part in PurePosixPath(path).parts) for path in paths):
        result.append("mechanical")
    for label, pattern in (("auth", r"(?:^|[/_.-])(?:auth|oauth|authentication|authorization|permission|permissions)(?:[/_.-]|$)"), ("migration", r"(?:^|/)(?:migrations?|migrate)(?:/|$)")):
        if any(re.search(pattern, path.lower()) for path in paths):
            result.append(label)
    if any(PurePosixPath(path).suffix.lower() in CONFIG_EXTENSIONS or path.startswith(".github/") for path in paths):
        result.append("config")
    metrics = row.get("metrics") or {}
    churn = int(metrics.get("additions") or 0) + int(metrics.get("deletions") or 0)
    if not churn:
        churn = sum(int(item.get("changes") or 0) for item in row.get("files", []))
    if churn >= 1000 or len(paths) >= 30:
        result.append("large")
    if not result:
        result.append("functional")
    return [stratum for stratum in STRATA if stratum in result]


def select_dataset(
    rows: Sequence[Dict[str, Any]], seed: str = "jev-v1",
    reviewed_per_repo: int = 30, representative_per_repo: int = 250,
) -> Dict[str, Any]:
    if reviewed_per_repo < 0 or representative_per_repo < 0:
        raise ValueError("Dataset cohort counts cannot be negative")
    grouped: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in _canonical_rows(rows):
        if row["repo"] in DEFAULT_REPOS and parse_github_time(row.get("merged_at")):
            grouped[row["repo"]].append(row)
    result: Dict[str, Any] = {"reviewed": [], "representative": [], "shortfalls": []}
    for repo in DEFAULT_REPOS:
        candidates = grouped[repo]
        # Take the outcome-independent random cohort first: deliberate review
        # enrichment must not bias the representative sample by exclusion.
        ordered = sorted(candidates, key=lambda row: _digest([seed, "representative", example_id(row)]))
        representative = ordered[:representative_per_repo]
        reserved = {example_id(row) for row in representative}
        remaining = [row for row in candidates if example_id(row) not in reserved]
        buckets = {stratum: sorted((row for row in remaining if stratum in change_strata(row)), key=lambda row: _digest([seed, "reviewed", example_id(row)])) for stratum in STRATA}
        reviewed: List[Dict[str, Any]] = []
        selected = set()
        while len(reviewed) < reviewed_per_repo:
            progress = False
            for stratum in STRATA:
                while buckets[stratum] and example_id(buckets[stratum][0]) in selected:
                    buckets[stratum].pop(0)
                if buckets[stratum] and len(reviewed) < reviewed_per_repo:
                    row = buckets[stratum].pop(0)
                    reviewed.append(row)
                    selected.add(example_id(row))
                    progress = True
            if not progress:
                break
        reviewed.sort(key=lambda row: (parse_github_time(row["merged_at"]), example_id(row)))
        dev_count = len(reviewed) * 2 // 3
        # Never place a tied merge timestamp on both sides of the time boundary.
        while 0 < dev_count < len(reviewed) and parse_github_time(reviewed[dev_count - 1]["merged_at"]) == parse_github_time(reviewed[dev_count]["merged_at"]):
            dev_count -= 1
        for index, row in enumerate(reviewed):
            result["reviewed"].append(_selection(row, "dev" if index < dev_count else "holdout"))
        result["representative"].extend(_selection(row, "representative") for row in representative)
        for cohort, actual, requested in (("reviewed", len(reviewed), reviewed_per_repo), ("representative", len(representative), representative_per_repo)):
            if actual < requested:
                result["shortfalls"].append({"repo": repo, "cohort": cohort, "requested": requested, "actual": actual})
    return result


def _selection(row: Mapping[str, Any], split: str) -> Dict[str, Any]:
    return {"example_id": example_id(row), "repo": row["repo"], "number": int(row["number"]), "merged_at": row["merged_at"], "split": split, "strata": change_strata(row)}


def derive_outcomes(rows: Sequence[Dict[str, Any]], observation_cutoff: str) -> List[Dict[str, Any]]:
    """Observed post-merge proxies; absent evidence is not a human low label.

    Only merged, explicitly referenced follow-ups between target merge and the
    cutoff count. Fix references additionally require an overlapping file and
    a merge within 30 days of the target merge.
    """
    cutoff = parse_github_time(observation_cutoff)
    if cutoff is None:
        raise ValueError("An explicit observation cutoff is required")
    grouped: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in _canonical_rows(rows):
        grouped[row["repo"]].append(row)
    results = []
    for repo, group in sorted(grouped.items()):
        by_number = {int(row["number"]): row for row in group}
        sha_to_number = {str(row["merge_commit_sha"]).lower(): int(row["number"]) for row in group if row.get("merge_commit_sha")}
        events: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
        for candidate in group:
            merged = parse_github_time(candidate.get("merged_at"))
            if merged is None or merged > cutoff:
                continue
            revert = is_revert_pr(candidate)
            if not revert and not is_followup_fix_pr(candidate):
                continue
            targets = extract_referenced_pr_numbers(candidate, by_number)
            if revert:
                text = f"{candidate.get('title') or ''}\n{candidate.get('body') or ''}"
                for match in REVERT_SHA_RE.finditer(text):
                    matches = [number for sha, number in sha_to_number.items() if sha.startswith(match.group(1).lower())]
                    if len(matches) == 1:
                        targets.add(matches[0])
            for number in sorted(targets):
                target = by_number[number]
                target_merged = parse_github_time(target.get("merged_at"))
                if number == int(candidate["number"]) or target_merged is None or not target_merged < merged:
                    continue
                if not revert:
                    if merged - target_merged > timedelta(days=30):
                        continue
                    paths = {item.get("filename") for item in candidate.get("files", []) if item.get("filename")}
                    target_paths = {item.get("filename") for item in target.get("files", []) if item.get("filename")}
                    if not paths.intersection(target_paths):
                        continue
                events[number].append({"kind": "revert" if revert else "fix", "number": int(candidate["number"]), "merged_at": candidate["merged_at"], "evidence": "explicit_reference"})
        for row in sorted(group, key=lambda item: int(item["number"])):
            merged = parse_github_time(row.get("merged_at"))
            valid = merged is not None and merged <= cutoff
            days = (cutoff - merged).total_seconds() / 86400 if valid else None
            mature = bool(valid and cutoff - merged >= timedelta(days=30))
            matched = sorted(events[int(row["number"])], key=lambda event: (event["merged_at"], event["number"]))
            results.append({"schema_version": 2, "example_id": example_id(row), "repo": repo, "number": int(row["number"]), "merged_at": row.get("merged_at"), "observation_cutoff": observation_cutoff, "observation_days": days, "maturity_days": 30, "valid": valid, "mature": mature, "strong_outcome": any(event["kind"] == "revert" for event in matched) if mature else None, "medium_outcome": bool(matched) if mature else None, "medium_outcome_strict": bool(matched) if mature else None, "events": matched, "label_semantics": "observed_proxy_not_human_risk"})
    return results


def _git(repo: str, *args: str, input_data: Optional[bytes] = None) -> bytes:
    return subprocess.run(["git", "-C", str(repo), *args], input=input_data, check=True, capture_output=True).stdout


def _commit(repo: str, sha: Any) -> str:
    if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-fA-F]{7,64}", sha):
        raise ValueError("missing_or_invalid_commit_sha")
    return _git(repo, "rev-parse", "--verify", f"{sha}^{{commit}}").decode().strip()


def _blobs(repo: str, specs: Sequence[str]) -> Dict[str, Optional[bytes]]:
    if not specs:
        return {}
    raw = _git(repo, "cat-file", "--batch", input_data=("\n".join(specs) + "\n").encode())
    position = 0
    result = {}
    for spec in specs:
        end = raw.index(b"\n", position)
        header = raw[position:end]
        position = end + 1
        if header.endswith(b" missing"):
            result[spec] = None
            continue
        fields = header.split()
        size = int(fields[-1])
        result[spec] = raw[position:position + size] if fields[-2] == b"blob" else None
        position += size + 1
    return result


def _text(blob: Optional[bytes]) -> Optional[str]:
    if blob is None or b"\x00" in blob:
        return None
    try:
        return blob.decode("utf-8")
    except UnicodeDecodeError:
        return None


def binary_patch_summary(patch: str) -> str:
    """Keep Git headers without binary payloads or unreadable text hunks."""
    headers = ("diff --git ", "index ", "old mode ", "new mode ", "deleted file mode ",
               "new file mode ", "similarity index ", "dissimilarity index ",
               "rename from ", "rename to ", "copy from ", "copy to ", "--- ", "+++ ")
    lines = []
    for line in patch.splitlines(keepends=True):
        if not line.startswith(headers):
            break
        lines.append(line)
    return "".join(lines)


@lru_cache(maxsize=65536)
def _is_test_source_path(path: str) -> bool:
    parsed = PurePosixPath(path)
    if parsed.suffix.lower() not in CODE_EXTENSIONS - {".css", ".h", ".hpp"} | {".mjs", ".cjs", ".mts", ".cts"}:
        return False
    if re.search(r"(?:^test_|[_.](?:test|spec)$)", parsed.stem):
        return True
    # Python and Go discover tests by filename, not just by their directory.
    if parsed.suffix.lower() in {".py", ".go"}:
        return False
    support = {"__init__", "conftest", "assertions", "base", "setup", "teardown", "util", "utils",
               "helper", "helpers", "fixture", "fixtures", "factories", "testdata", "testutils",
               "support", "assets", "resources", "data", "mocks", "snapshots", "__fixtures__",
               "__mocks__", "__snapshots__"}
    parts = parsed.parent.parts
    test_root = next((index for index, part in enumerate(parts) if part in {"test", "tests", "__tests__"}), None)
    return (test_root is not None and parsed.stem not in support
            and not support.intersection(parts[test_root + 1:]))


@lru_cache(maxsize=65536)
def _test_path_descriptor(path: str) -> tuple:
    parsed = PurePosixPath(path)
    stem = re.sub(r"(?:^test_|[_.](?:test|spec)$)", "", parsed.stem)
    parts = tuple(part for part in parsed.parent.parts if part not in {"test", "tests", "__tests__"})
    if parts[:1] == ("src",):
        parts = parts[1:]
    return stem, parts


def related_test_score(test: str, paths: Sequence[str]) -> tuple:
    """Prefer shared directories, then names; (0, 0) means ineligible/unrelated."""
    if not _is_test_source_path(test):
        return (0, 0, test)
    stem, test_parts = _test_path_descriptor(test)
    best = (0, 0)
    for path in paths:
        changed_stem, changed_parts = _test_path_descriptor(path)
        same_name = int(stem == changed_stem)
        # Sibling tests share a prefix; separate test trees can omit the source
        # package (src/snuba/query -> tests/query), so also match the suffix.
        common = 0
        for left_parts, right_parts in ((test_parts, changed_parts), (test_parts[::-1], changed_parts[::-1])):
            matched = 0
            for left, right in zip(left_parts, right_parts):
                if left != right:
                    break
                matched += 1
            common = max(common, matched)
        best = max(best, (common, same_name))
    return (-best[0], -best[1], test)


def _context(repo: str, head: str, changed: Sequence[str]) -> Dict[str, Any]:
    # Enumerate metadata only; read a small selected set of blobs.
    entries = _git(repo, "ls-tree", "-r", "-z", "--name-only", head).split(b"\x00")
    tree = sorted(path.decode("utf-8", "surrogateescape") for path in entries if path)
    ancestors = {str(parent) for path in changed for parent in PurePosixPath(path).parents}
    relevant = [path for path in tree if str(PurePosixPath(path).parent) in ancestors]
    tree_limit = 160
    tree_view = relevant[:tree_limit]
    documents = []
    for path in relevant:
        name = PurePosixPath(path).name.lower()
        if name.startswith("readme"):
            documents.append((path, "readme"))
        elif name in MANIFESTS:
            documents.append((path, "manifest"))
    documents.sort(key=lambda item: (-len(PurePosixPath(item[0]).parts), item[0]))
    changed_set = set(changed)
    scored_tests = sorted(related_test_score(path, changed) for path in tree if path not in changed_set and _is_test_source_path(path))
    tests = [score[2] for score in scored_tests if score[:2] != (0, 0)][:4]
    chosen = documents[:12] + [(path, "test") for path in tests]
    omitted = [{"path": path, "reason": "context_file_limit"} for path, _ in documents[12:]]
    safe = [(path, kind) for path, kind in chosen if "\n" not in path and "\r" not in path]
    blobs = _blobs(repo, [f"{head}:{path}" for path, _ in safe])
    files = []
    for path, kind in safe:
        blob = blobs[f"{head}:{path}"]
        content = _text(blob)
        if blob is not None and len(blob) <= 65536 and content is not None:
            files.append({"path": path, "content": content, "kind": kind})
        else:
            omitted.append({"path": path, "reason": "large_or_nontext_context"})
    return {"tree": tree_view, "tree_omitted": max(0, len(relevant) - tree_limit), "files": files, "omitted": omitted}


def prepare_snapshot(row: Dict[str, Any], git_repo: Optional[str]) -> Dict[str, Any]:
    """Reconstruct a provably complete merge delta or original PR head delta.

    A first-parent Git catalogue cannot distinguish a squash from the final
    commit of a rebased PR. It needs authoritative PR metadata before its
    reconstructed patch is suitable for classification.
    """
    merged = bool(row.get("merged_at"))
    result: Dict[str, Any] = {"schema_version": 2, "snapshot_version": SNAPSHOT_VERSION, "example_id": example_id(row), "repo": row["repo"], "number": int(row["number"]), "snapshot": {"kind": "merge" if merged else "pr_head", "base_sha": None, "head_sha": None, "source_base_sha": (row.get("base") or {}).get("sha"), "source_head_sha": (row.get("head") or {}).get("sha"), "source_merge_sha": row.get("merge_commit_sha"), "observed_at": row.get("merged_at") if merged else row.get("fetched_at")}, "files": [], "repository_context": {"tree": [], "files": []}, "status": "incomplete", "missing": []}
    result["pr_metadata"] = pr_metadata(row)
    if row.get("data_source") == "local_git":
        result["missing"].append("unverified_pr_span")
        return result
    if not git_repo:
        result["missing"].append("local_git_repository_unavailable")
        return result
    try:
        if merged:
            head = _commit(git_repo, row.get("merge_commit_sha"))
            parents = _git(git_repo, "rev-list", "--parents", "-n", "1", head).decode().split()[1:]
            commits = int((row.get("metrics") or {}).get("commits") or 0)
            if len(parents) == 1 and commits != 1:
                # The final commit of a rebase is not the PR delta. Using the
                # original immutable PR refs also safely covers squash merges
                # and older REST captures that did not record commit counts.
                if not (row.get("base") or {}).get("sha") or not (row.get("head") or {}).get("sha"):
                    result["missing"].append("ambiguous_merge_span")
                    return result
                head = _commit(git_repo, row["head"]["sha"])
                base_ref = _commit(git_repo, row["base"]["sha"])
                base = _git(git_repo, "merge-base", base_ref, head).decode().strip()
                result["snapshot"]["kind"] = "pr_head"
            elif len(parents) >= 2 or (len(parents) == 1 and commits == 1):
                base = parents[0]
            else:
                result["missing"].append("ambiguous_merge_span")
                return result
        else:
            head = _commit(git_repo, (row.get("head") or {}).get("sha"))
            base_ref = _commit(git_repo, (row.get("base") or {}).get("sha"))
            base = _git(git_repo, "merge-base", base_ref, head).decode().strip()
        result["snapshot"].update(base_sha=base, head_sha=head)
        options = ("--no-ext-diff", "--no-textconv", "--no-color", "--find-renames")
        names = _git(git_repo, "diff", *options, "--name-status", "-z", base, head, "--").split(b"\x00")
        changes = []
        offset = 0
        while offset < len(names) and names[offset]:
            code = names[offset].decode()
            path = names[offset + 1].decode("utf-8", "surrogateescape")
            offset += 2
            previous = None
            if code[0] in "RC":
                previous = path
                path = names[offset].decode("utf-8", "surrogateescape")
                offset += 1
            changes.append((code, path, previous))
        raw_diff = _git(git_repo, "diff", *options, "--binary", "--full-index", "--unified=3", base, head, "--")
        patches = re.split(br"(?m)(?=^diff --git )", raw_diff)
        patches = [patch for patch in patches if patch]
        if len(patches) != len(changes):
            result["missing"].append("diff_file_count_mismatch")
            return result
        specs = []
        for code, path, previous in changes:
            if any("\n" in candidate or "\r" in candidate for candidate in (path, previous or "")):
                result["missing"].append(f"unsupported_path:{path!r}")
                continue
            if code[0] != "A":
                specs.append(f"{base}:{previous or path}")
            if code[0] != "D":
                specs.append(f"{head}:{path}")
        blobs = _blobs(git_repo, specs)
        for (code, path, previous), patch in zip(changes, patches):
            before_blob = blobs.get(f"{base}:{previous or path}") if code[0] != "A" else b""
            after_blob = blobs.get(f"{head}:{path}") if code[0] != "D" else b""
            before, after = _text(before_blob), _text(after_blob)
            binary = bool(re.search(br"(?m)^(?:GIT binary patch|Binary files .* differ)$", patch)) or before is None or after is None
            unavailable = before_blob is None or after_blob is None
            if unavailable:
                result["missing"].append(f"blob_unavailable:{path}")
            additions, deletions = 0, 0
            in_hunk = False
            for line in patch.splitlines():
                if line.startswith(b"@@ "):
                    in_hunk = True
                elif in_hunk:
                    additions += int(line.startswith(b"+"))
                    deletions += int(line.startswith(b"-"))
            patch_text = patch.decode("utf-8", "replace")
            file = {"path": path, "previous_path": previous, "status": {"A": "added", "D": "removed", "R": "renamed", "C": "copied", "T": "type_changed"}.get(code[0], "modified"), "patch": binary_patch_summary(patch_text) if binary else patch_text, "binary": binary, "before": before, "after": after, "additions": additions, "deletions": deletions}
            if binary and not unavailable:
                file["content_metadata"] = {}
                for side, blob, content, present in (
                    ("before", before_blob, before, code[0] != "A"),
                    ("after", after_blob, after, code[0] != "D"),
                ):
                    file["content_metadata"][side] = {
                        "kind": "absent" if not present else "text" if content is not None else "binary",
                        "size_bytes": len(blob) if present else None,
                        "sha256": hashlib.sha256(blob).hexdigest() if present else None,
                    }
            result["files"].append(file)
        # Earlier REST collections predate data_source but retain GitHub IDs.
        authoritative = row.get("data_source") == "github_api" or row.get("id") is not None
        if row.get("metrics_authoritative") or authoritative:
            metrics = row.get("metrics") or {}
            actual = {"changed_files": len(result["files"]), "additions": sum(item["additions"] for item in result["files"]), "deletions": sum(item["deletions"] for item in result["files"])}
            for field, count in actual.items():
                if metrics.get(field) is None or int(metrics[field]) != count:
                    result["missing"].append(f"authoritative_{field}_mismatch")
        if row.get("files_authoritative") or (row.get("id") is not None and "files" in row):
            expected_paths = {item.get("filename") or item.get("path") for item in row.get("files", [])}
            if expected_paths != {item["path"] for item in result["files"]}:
                result["missing"].append("authoritative_paths_mismatch")
        if not changes:
            result["missing"].append("empty_diff")
        result["repository_context"] = _context(git_repo, head, [path for _, path, _ in changes])
        result["status"] = "incomplete" if result["missing"] else "ready"
    except (subprocess.CalledProcessError, ValueError, OSError) as error:
        reason = str(error) if isinstance(error, ValueError) else type(error).__name__
        result["missing"].append(f"git_snapshot_unavailable:{reason}")
    return result


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=True) + "\n", encoding="utf-8")


def refresh_snapshot_metadata(
    rows: Sequence[Dict[str, Any]], selection: Mapping[str, Any],
    out_dir: str, client: Any,
) -> Dict[str, Dict[str, Any]]:
    """Fetch authoritative spans and captured PR text for local-Git rows.

    Per-PR caches are immutable. All independent requests finish and successful
    results are cached before an error is raised, so a retry fetches only misses.
    This explicitly requested helper is the dataset module's only network path.
    """
    by_id = {example_id(row): row for row in _canonical_rows(rows)}
    selected_ids = sorted({item["example_id"] for cohort in ("reviewed", "representative") for item in selection.get(cohort, [])})
    directory = Path(out_dir).resolve()
    cache_dir = directory / "snapshot-metadata"
    cache_dir.mkdir(parents=True, exist_ok=True)
    result: Dict[str, Dict[str, Any]] = {}
    pending = []
    for identity in selected_ids:
        row = by_id[identity]
        if row.get("data_source") != "local_git":
            continue
        path = cache_dir / f"{row['repo'].replace('/', '__')}__{row['number']}.json"
        if path.exists():
            metadata = json.loads(path.read_text(encoding="utf-8"))
            if example_id(metadata) != identity:
                raise ValueError(f"Snapshot metadata identity mismatch for {identity}")
            result[identity] = metadata
        else:
            pending.append((identity, row, path))

    def fetch(item: tuple) -> tuple:
        identity, row, path = item
        response = client.get_pull_request(RepoRef.parse(row["repo"]), int(row["number"]))
        normalized = normalize_pr(RepoRef.parse(row["repo"]), response, utc_now_iso())
        if example_id(normalized) != identity:
            raise ValueError("GitHub returned a different PR identity")
        # Capture the PR text needed by the default input alongside its span.
        # Author, reviews, response headers and client credentials stay out.
        metadata = {key: normalized[key] for key in ("schema_version", "repo", "number", "fetched_at", "state", "merged_at", "merge_commit_sha", "base", "head", "metrics", "title", "body", "updated_at")}
        metadata.update(data_source="github_api", metrics_authoritative=True, files_authoritative=False, files=row.get("files") or [])
        try:
            with path.open("x", encoding="utf-8") as handle:
                handle.write(json.dumps(metadata, indent=2, sort_keys=True) + "\n")
        except FileExistsError:
            # A concurrent preparation won the cache race; preserve its fetch.
            metadata = json.loads(path.read_text(encoding="utf-8"))
            if example_id(metadata) != identity:
                raise ValueError("Concurrent cache contains a different PR")
        return identity, metadata

    failures = []
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {executor.submit(fetch, item): item[0] for item in pending}
        for future in as_completed(futures):
            try:
                identity, metadata = future.result()
                result[identity] = metadata
            except Exception:
                # Do not copy HTTP response bodies or token-bearing URLs into
                # dataset artifacts or the error message.
                failures.append(futures[future])
    result = dict(sorted(result.items()))
    _write_json(directory / "snapshot-metadata.json", result)
    if failures:
        raise RuntimeError(f"Snapshot metadata fetch failed for {', '.join(sorted(failures))}; successful entries are cached, rerun to resume")
    return result


def _review_markdown(snapshot: Mapping[str, Any]) -> str:
    revision = snapshot["snapshot"]
    lines = [f"# {snapshot['example_id']}", "", f"Snapshot kind: `{revision['kind']}`. Status: `{snapshot['status']}`.", "", f"Base: `{revision.get('base_sha') or 'unavailable'}`", "", f"Head: `{revision.get('head_sha') or 'unavailable'}`", ""]
    if snapshot.get("missing"):
        lines += ["Incomplete input; do not assign a risk label until resolved.", ""]
    for index, item in enumerate(snapshot.get("files", []), 1):
        lines += [f"## File {index}", "", f"Path: {json.dumps(item['path'])}. Status: `{item['status']}`.", ""]
        if item.get("previous_path"):
            lines += [f"Previous path: {json.dumps(item['previous_path'])}.", ""]
        patch = item.get("patch") or ""
        fence = "`" * max(3, max((len(run) + 1 for run in re.findall(r"`+", patch)), default=3))
        lines += [f"{fence}diff", patch.rstrip("\n"), fence, ""]
    return "\n".join(lines)


def prepare_dataset(
    raw_paths: Sequence[str], git_repos: Mapping[str, str], out_dir: str,
    seed: str = "jev-v1", reviewed_per_repo: int = 30,
    representative_per_repo: int = 250, observation_cutoff: Optional[str] = None,
    snapshot_metadata: Optional[Mapping[str, Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Write resumable snapshots, blind review packets and isolated outcomes.

    Existing human labels are never overwritten. The returned manifest contains
    the absolute manifest_path plus all artifact paths and cohort metadata.
    """
    rows = _canonical_rows(row for path in raw_paths for row in read_jsonl(str(path)))
    if observation_cutoff is None:
        fetched = [row.get("fetched_at") for row in rows]
        if not fetched or any(not value for value in fetched):
            raise ValueError("--observation-cutoff is required when any raw row lacks fetched_at")
        observation_cutoff = min(fetched, key=lambda value: parse_github_time(value))
    if parse_github_time(observation_cutoff) is None:
        raise ValueError("Invalid observation cutoff")
    selection = select_dataset(rows, seed, reviewed_per_repo, representative_per_repo)
    directory = Path(out_dir).resolve()
    snapshots_dir = directory / "snapshots"
    reviews_dir = directory / "review"
    snapshots_dir.mkdir(parents=True, exist_ok=True)
    reviews_dir.mkdir(exist_ok=True)
    selected = selection["reviewed"] + selection["representative"]
    signature = _digest({"version": DATASET_VERSION, "seed": seed, "selected": selected})
    manifest_path = directory / "manifest.json"
    if manifest_path.exists():
        previous = json.loads(manifest_path.read_text())
        if previous.get("selection_hash") != signature:
            raise ValueError("Dataset selection changed; choose a new output directory to preserve review labels")
    by_id = {example_id(row): row for row in rows}
    label_templates = []
    inputs_paths = {split: directory / f"inputs-{split}.jsonl" for split in ("dev", "holdout", "representative")}
    handles = {split: path.open("w", encoding="utf-8") for split, path in inputs_paths.items()}
    status_counts: Counter = Counter()
    try:
        for item in selected:
            row = by_id[item["example_id"]]
            captured_text = pr_metadata(row)
            metadata = (snapshot_metadata or {}).get(item["example_id"])
            if metadata is not None:
                refreshed_text = pr_metadata(metadata)
                if refreshed_text["description_available"] and isinstance(refreshed_text["title"], str):
                    captured_text = refreshed_text
                row = {**row, **metadata}
                if example_id(row) != item["example_id"]:
                    raise ValueError("Snapshot metadata must match the selected PR identity")
            snapshot_path = snapshots_dir / f"{row['repo'].replace('/', '__')}__{row['number']}.json"
            cache_key = _digest({"version": SNAPSHOT_VERSION, "repo": row["repo"], "number": row["number"], "data_source": row.get("data_source"), "merge_commit_sha": row.get("merge_commit_sha"), "merged_at": row.get("merged_at"), "base": (row.get("base") or {}).get("sha"), "head": (row.get("head") or {}).get("sha"), "metrics": row.get("metrics"), "metrics_authoritative": row.get("metrics_authoritative"), "files_authoritative": row.get("files_authoritative"), "files": row.get("files") if row.get("files_authoritative") else None})
            cached = json.loads(snapshot_path.read_text()) if snapshot_path.exists() else None
            if cached and cached.get("cache_key") == cache_key and cached.get("status") == "ready":
                snapshot = cached
            else:
                snapshot = prepare_snapshot(row, git_repos.get(row["repo"]))
                snapshot["cache_key"] = cache_key
            # Span-only legacy refreshes must not make synthetic Git text look
            # captured. PR text provenance is independent of the Git cache.
            snapshot["pr_metadata"] = captured_text
            # These are dataset metadata, not model context. Refresh them even
            # when immutable Git content is reused from the snapshot cache.
            snapshot["split"] = item["split"]
            snapshot["strata"] = item["strata"]
            item["snapshot"] = dict(snapshot["snapshot"])
            _write_json(snapshot_path, snapshot)
            status_counts[snapshot["status"]] += 1
            handles[item["split"]].write(json.dumps(snapshot, sort_keys=True) + "\n")
            if item["split"] in {"dev", "holdout"}:
                # Review packets contain only the exact snapshot, never titles,
                # outcomes, old predictions or sampling strata.
                review_snapshot = {key: value for key, value in snapshot.items() if key not in {"strata", "pr_metadata"}}
                _write_json(reviews_dir / snapshot_path.name, review_snapshot)
                (reviews_dir / snapshot_path.with_suffix(".md").name).write_text(_review_markdown(review_snapshot), encoding="utf-8")
                label_templates.append({"schema_version": 2, "example_id": item["example_id"], "repo": item["repo"], "number": item["number"], "split": item["split"], "snapshot": {key: snapshot["snapshot"][key] for key in ("base_sha", "head_sha")}, "rubric_version": RUBRIC_VERSION, "risk_label": None, "rationale": "", "reviewer": None, "reviewed_at": None, "source": "human"})
    finally:
        for handle in handles.values():
            handle.close()
    labels_path = directory / "labels.jsonl"
    if not labels_path.exists():
        write_jsonl(str(labels_path), label_templates)
    else:
        # A previously unavailable commit may become readable after fetching.
        # Bind only untouched templates to that revision; even partial human
        # review notes are sufficient to protect the entire existing row.
        templates_by_id = {item["example_id"]: item for item in label_templates}
        labels = list(read_jsonl(str(labels_path)))
        changed = False
        for label in labels:
            template = templates_by_id.get(label.get("example_id"))
            if template is None or label.get("risk_label") is not None or any(label.get(field) for field in ("reviewer", "rationale", "reviewed_at")):
                continue
            old_snapshot = label.get("snapshot") or {}
            new_snapshot = template["snapshot"]
            if old_snapshot != new_snapshot and new_snapshot.get("base_sha") and new_snapshot.get("head_sha"):
                label["snapshot"] = dict(new_snapshot)
                changed = True
        if changed:
            write_jsonl(str(labels_path), labels)
    selected_ids = {item["example_id"] for item in selected}
    outcomes_path = directory / "outcomes.jsonl"
    write_jsonl(str(outcomes_path), (row for row in derive_outcomes(rows, observation_cutoff) if row["example_id"] in selected_ids))
    review_index = reviews_dir / "index.md"
    index_lines = ["# Blind PR risk review", "", f"Rubric: `{RUBRIC_VERSION}`. Review the complete change, considering regression likelihood, impact, scope and recovery.", "", "Record a low, medium or high risk label, a brief rationale, reviewer and review timestamp in [labels.jsonl](../labels.jsonl). The labels are human judgements; incomplete snapshots must remain unlabelled.", "", "- **low:** Limited regression risk, including editorial or mechanical changes and tightly scoped behavior.", "- **medium:** Functional changes with plausible regressions and limited impact.", "- **high:** Elevated regression risk because of behavior, potential impact, scope or difficulty of recovery.", "", "Do not infer risk solely from file size, sensitive names, missing modified tests or absence of an obvious bug.", ""]
    for item in selection["reviewed"]:
        name = f"{item['repo'].replace('/', '__')}__{item['number']}.md"
        index_lines.append(f"- [{item['example_id']}]({name})")
    review_index.write_text("\n".join(index_lines) + "\n", encoding="utf-8")
    manifest = {"schema_version": 2, "dataset_version": DATASET_VERSION, "rubric_version": RUBRIC_VERSION, "seed": seed, "selection_hash": signature, "observation_cutoff": observation_cutoff, "created_at": datetime.now(timezone.utc).isoformat(), "manifest_path": str(manifest_path), "labels_path": str(labels_path), "outcomes_path": str(outcomes_path), "inputs": {split: str(path) for split, path in inputs_paths.items()}, "review_dir": str(reviews_dir), "review_index_path": str(review_index), "snapshot_counts": dict(status_counts), "label_status": "requires_human_review", "sources": [str(Path(path).resolve()) for path in raw_paths], **selection}
    _write_json(manifest_path, manifest)
    return manifest
