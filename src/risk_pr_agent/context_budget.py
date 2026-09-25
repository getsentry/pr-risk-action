"""Bound code evidence using an explicit proxy, not Jev's unpublished tokenizer.

The provider still enforces its own context limit. A cl100k_base estimate and a
reserve reduce overflows; neither is an exact count of Jev's input tokens.
"""
from __future__ import annotations

import copy
import json
import math
from functools import lru_cache

import tiktoken

MODEL_LIMIT = 32000
RESERVE = 2000
ESTIMATOR = "cl100k_base"
CODE_FIELDS = ("patch", "before", "after")


@lru_cache(maxsize=1)
def _encoding():
    return tiktoken.get_encoding(ESTIMATOR)


def _serialize(request):
    return json.dumps(request, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _measure(request, max_bytes, max_input_tokens):
    serialized = _serialize(request)
    size = len(serialized.encode("utf-8"))
    # Avoid tokenizing a many-megabyte diff that already fails the byte limit.
    if size > max_bytes:
        return False, None
    count = len(_encoding().encode_ordinary(serialized))
    return count <= max_input_tokens, count


def _compact_metadata(state):
    """Rewrite metadata in place as lossless rows, retaining zero-based file indices."""
    files = state["files"]
    if not files:
        return
    shared = set(files[0]).intersection(*(set(file) for file in files[1:]))
    columns = sorted(shared, key=lambda key: (key != "path", key))
    state["file_columns"] = columns
    state["file_layout"] = (
        "Each files row follows file_columns. file_details keys and code.file_index use zero-based file indices."
    )
    state["files"] = [[file[key] for key in columns] for file in files]
    details = {str(index): {key: value for key, value in file.items() if key not in shared}
               for index, file in enumerate(files)}
    details = {index: value for index, value in details.items() if value}
    if details:
        state["file_details"] = details


def _with_code_prefix(floor, fields, characters, compact):
    """Copy the metadata floor and spend one character budget across ordered code fields."""
    candidate = copy.deepcopy(floor)
    state = candidate["state"]
    code = {}
    for index, key, original in fields:
        if characters <= 0:
            break
        included = original[:characters]
        characters -= len(included)
        if not included:
            continue
        target = code.setdefault(index, {"file_index": index}) if compact else state["files"][index]
        target[key] = included
    if code:
        state["code"] = list(code.values())
    return candidate


def fit_diff_context(request, max_bytes, *, diff_fraction=1.0, max_input_tokens=None):
    """Fit ordered code prefixes while preserving every non-code input field.

    Mutate only on success. The normal request is unchanged when it fits. Large
    file inventories may use shared column names to retain all their metadata.
    A smaller diff_fraction shortens the prefix even when the original fits,
    allowing bounded recovery after an explicit provider context rejection.
    """
    if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes <= 0:
        raise ValueError("max_bytes must be a positive integer")
    if isinstance(diff_fraction, bool) or not math.isfinite(diff_fraction) or not 0 <= diff_fraction <= 1:
        raise ValueError("diff_fraction must be between 0 and 1")
    if max_input_tokens is None:
        max_input_tokens = MODEL_LIMIT - RESERVE
    if (type(max_input_tokens) is not int or not 1 <= max_input_tokens <= MODEL_LIMIT - RESERVE):
        raise ValueError("max_input_tokens must be between 1 and the model limit minus its reserve")
    budget = {"model_limit": MODEL_LIMIT, "reserve": RESERVE, "estimator": ESTIMATOR}
    budget["max_input_tokens"] = max_input_tokens
    result = {"context_budget": budget, "diff_truncated": False, "omitted": []}
    files = request.get("state", {}).get("files", [])
    fields = [(index, key, file[key]) for index, file in enumerate(files)
              for key in CODE_FIELDS if isinstance(file.get(key), str)]
    fits, tokens = _measure(request, max_bytes, max_input_tokens)
    if fits and (diff_fraction == 1 or not any(content for _, _, content in fields)):
        return {**result, "status": "ready", "estimated_input_tokens": tokens}

    floor = copy.deepcopy(request)
    state = floor["state"]
    for file in state.get("files", []):
        for key in CODE_FIELDS:
            if isinstance(file.get(key), str):
                del file[key]
    state["code_context"] = {"truncated": True, "snippets_may_be_incomplete": True}
    floor_fits, floor_tokens = _measure(floor, max_bytes, max_input_tokens)
    compact = False
    if not floor_fits and files:
        _compact_metadata(state)
        compact = True
        floor_fits, floor_tokens = _measure(floor, max_bytes, max_input_tokens)
    if not floor_fits:
        return {**result, "status": "metadata_exceeds_budget", "estimated_input_tokens": floor_tokens}

    # Prefix length is bounded by the byte budget before any repeated tokenization.
    # Since a Unicode character needs at least one UTF-8 byte, larger prefixes
    # cannot fit, even before JSON escaping and the protected metadata.
    total_characters = sum(len(content) for _, _, content in fields)
    low, high = 0, min(total_characters, max_bytes)
    best, best_tokens = floor, floor_tokens
    while low < high:
        middle = (low + high + 1) // 2
        candidate = _with_code_prefix(floor, fields, middle, compact)
        candidate_fits, candidate_tokens = _measure(candidate, max_bytes, max_input_tokens)
        if candidate_fits:
            low, best, best_tokens = middle, candidate, candidate_tokens
        else:
            high = middle - 1
    included_characters = math.floor(low * diff_fraction)
    if included_characters != low:
        best = _with_code_prefix(floor, fields, included_characters, compact)
        fits, best_tokens = _measure(best, max_bytes, max_input_tokens)
        # Token counts need not be monotonic for adjacent string prefixes.
        while not fits and included_characters:
            included_characters -= 1
            best = _with_code_prefix(floor, fields, included_characters, compact)
            fits, best_tokens = _measure(best, max_bytes, max_input_tokens)

    remaining = included_characters
    omitted = []
    for index, key, content in fields:
        included = content[:remaining]
        remaining -= len(included)
        if len(included) != len(content):
            omission = {"kind": "diff" if key == "patch" else "file_content", "path": files[index]["path"],
                        "original_bytes": len(content.encode("utf-8")),
                        "included_bytes": len(included.encode("utf-8")),
                        "reason": "context_budget" if diff_fraction == 1 else "provider_context"}
            if key != "patch":
                omission["side"] = key
            omitted.append(omission)
    if not omitted:
        del best["state"]["code_context"]
        _, best_tokens = _measure(best, max_bytes, max_input_tokens)
    request.clear()
    request.update(best)
    return {**result, "status": "ready", "estimated_input_tokens": best_tokens,
            "diff_truncated": bool(omitted), "omitted": omitted, "metadata_compacted": compact}
