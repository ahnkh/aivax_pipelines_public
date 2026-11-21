"""Project detected filters and correlated categories into searchable log fields."""

from collections.abc import Mapping
from typing import Any

ACTIONS = frozenset(("accept", "masking", "block"))
FILTERS = frozenset(("regex", "file block", "slm", "secuxper"))


def detection_metadata(document: Mapping[str, Any]) -> dict[str, list[str]]:
    filters: set[str] = set()
    pairs: set[str] = set()
    for entry in document.get("filter_detect") or []:
        name = str(entry.get("filter") or "").strip().lower()
        if name not in FILTERS:
            continue
        root_detected = entry.get("mode") in ACTIONS
        detected = root_detected
        categories: set[str] = set()
        if root_detected and entry.get("category"):
            categories.add(entry["category"].strip())
        for child in [
            *(entry.get("evidence") or []),
            *(entry.get("file_summary") or []),
        ]:
            if child.get("action") in ACTIONS or (
                not child.get("action") and root_detected
            ):
                detected = True
                if child.get("category"):
                    categories.add(child["category"].strip())
        if detected:
            filters.add(name)
            pairs.update(f"{name}|{category}" for category in categories if category)
    return {
        "detected_filters": sorted(filters),
        "detected_filter_categories": sorted(pairs),
    }
