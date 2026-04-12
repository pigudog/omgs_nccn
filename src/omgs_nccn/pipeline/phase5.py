from __future__ import annotations

import json
import re
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any
from typing import Optional

from omgs_nccn.config.paths import REPO_ROOT
from omgs_nccn.config.paths import ov_2025_roots
from omgs_nccn.query.query_assets import build_query_assets


SINGLE_PAGE_RE = re.compile(r"\b(?P<page>(?:OV|LCOC)-\d+)\b")
FAMILY_INDEXED_RE = re.compile(
    r"\b(?P<family>(?:OV|LCOC)-[A-Z])(?:\s*\((?P<index_paren>\d+)\s+of\s+(?P<count_paren>\d+)\)|\s*,\s*(?P<index_comma>\d+)\s+of\s+(?P<count_comma>\d+))(?=\W|$)"
)
PAGE_RANGE_RE = re.compile(
    r"\b(?P<start>(?:OV|LCOC)-?\d+)\s+to\s+(?P<end>(?:OV|LCOC)-?\d+)\b"
)
FAMILY_PLAIN_RE = re.compile(r"\b(?P<family>(?:OV|LCOC)-[A-Z])\b")
CATEGORY_PATTERNS = [
    ("1", re.compile(r"\(\s*category\s*1\s*\)", flags=re.IGNORECASE)),
    ("2A", re.compile(r"\(\s*category\s*2A\s*\)", flags=re.IGNORECASE)),
    ("2B", re.compile(r"\(\s*category\s*2B\s*\)", flags=re.IGNORECASE)),
    ("3", re.compile(r"\(\s*category\s*3\s*\)", flags=re.IGNORECASE)),
]
PREFERENCE_PATTERNS = [
    ("preferred", re.compile(r"\bpreferred\b", flags=re.IGNORECASE)),
    (
        "other_recommended",
        re.compile(r"\bother recommended\b", flags=re.IGNORECASE),
    ),
    (
        "useful_in_certain_circumstances",
        re.compile(r"\buseful in certain circumstances\b", flags=re.IGNORECASE),
    ),
]


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def _dedupe_strings(values: list[Any]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        ordered.append(text)
    return ordered


def _reviewed_footnote_id(page_code: str, label: str) -> str:
    return f"{page_code}:{label}"


def _normalize_page_code(raw: str) -> str:
    raw = raw.strip()
    if re.fullmatch(r"(OV|LCOC)-\d+", raw):
        return raw
    match = re.fullmatch(r"(OV|LCOC)-?(\d+)", raw)
    if not match:
        return raw
    return f"{match.group(1)}-{match.group(2)}"


def _family_indexed_parts(match: re.Match[str]) -> tuple[str, int, int]:
    family = match.group("family")
    index = match.group("index_paren") or match.group("index_comma")
    count = match.group("count_paren") or match.group("count_comma")
    return family, int(index), int(count)


def _extract_condition_prefix(text: str, start_idx: int) -> Optional[str]:
    prefix = text[:start_idx].strip()
    if not prefix:
        return None
    last_line = prefix.splitlines()[-1].strip(" •\t")
    if not last_line:
        return None
    if len(last_line) > 160:
        return None
    return last_line


def _build_explicit_refs_from_rule_graph(rule_graph: dict[str, Any]) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    for node in rule_graph["nodes"]:
        text = node.get("verbatim_text", "")
        page_code = node.get("page_code")
        node_id = node.get("id")

        for match in FAMILY_INDEXED_RE.finditer(text):
            family, index, count = _family_indexed_parts(match)
            refs.append(
                {
                    "source_kind": "rule_graph_node",
                    "source_node_id": node_id,
                    "source_page_code": page_code,
                    "ref_type": "family_indexed_ref",
                    "raw_text": match.group(0),
                    "condition_text": _extract_condition_prefix(text, match.start()),
                    "target_page_family": family,
                    "target_page_index": index,
                    "target_page_count": count,
                }
            )

        for match in PAGE_RANGE_RE.finditer(text):
            refs.append(
                {
                    "source_kind": "rule_graph_node",
                    "source_node_id": node_id,
                    "source_page_code": page_code,
                    "ref_type": "page_range_ref",
                    "raw_text": match.group(0),
                    "condition_text": _extract_condition_prefix(text, match.start()),
                    "target_page_start": _normalize_page_code(match.group("start")),
                    "target_page_end": _normalize_page_code(match.group("end")),
                }
            )

        taken_spans = [m.span() for m in FAMILY_INDEXED_RE.finditer(text)]
        taken_spans += [m.span() for m in PAGE_RANGE_RE.finditer(text)]

        def in_taken(pos: int) -> bool:
            return any(start <= pos < end for start, end in taken_spans)

        for match in SINGLE_PAGE_RE.finditer(text):
            if in_taken(match.start()):
                continue
            refs.append(
                {
                    "source_kind": "rule_graph_node",
                    "source_node_id": node_id,
                    "source_page_code": page_code,
                    "ref_type": "single_page_ref",
                    "raw_text": match.group(0),
                    "condition_text": _extract_condition_prefix(text, match.start()),
                    "target_page_code": match.group("page"),
                }
            )
    return refs


def _build_explicit_refs_from_native_markdown(native_md_path: Path) -> list[dict[str, Any]]:
    text = native_md_path.read_text(errors="ignore")
    refs: list[dict[str, Any]] = []
    for match in FAMILY_INDEXED_RE.finditer(text):
        family, index, count = _family_indexed_parts(match)
        refs.append(
            {
                "source_kind": "native_primary_md",
                "ref_type": "family_indexed_ref",
                "raw_text": match.group(0),
                "condition_text": _extract_condition_prefix(text, match.start()),
                "target_page_family": family,
                "target_page_index": index,
                "target_page_count": count,
            }
        )
    for match in PAGE_RANGE_RE.finditer(text):
        refs.append(
            {
                "source_kind": "native_primary_md",
                "ref_type": "page_range_ref",
                "raw_text": match.group(0),
                "condition_text": _extract_condition_prefix(text, match.start()),
                "target_page_start": _normalize_page_code(match.group("start")),
                "target_page_end": _normalize_page_code(match.group("end")),
            }
        )
    taken_spans = [m.span() for m in FAMILY_INDEXED_RE.finditer(text)]
    taken_spans += [m.span() for m in PAGE_RANGE_RE.finditer(text)]

    def in_taken(pos: int) -> bool:
        return any(start <= pos < end for start, end in taken_spans)

    for match in SINGLE_PAGE_RE.finditer(text):
        if in_taken(match.start()):
            continue
        refs.append(
            {
                "source_kind": "native_primary_md",
                "ref_type": "single_page_ref",
                "raw_text": match.group(0),
                "condition_text": _extract_condition_prefix(text, match.start()),
                "target_page_code": match.group("page"),
            }
        )
    return refs


def _build_page_footnotes(pages_json: dict[str, Any]) -> dict[int, dict[str, str]]:
    out: dict[int, dict[str, str]] = {}
    for page in pages_json["pages"]:
        page_number = page["page_number"]
        footnotes: dict[str, str] = {}
        current_label = None
        current_lines: list[str] = []
        for raw_line in page["text"].splitlines():
            line = raw_line.strip()
            if not line:
                continue
            match = re.match(r"^([a-z]{1,2})\s+(.*)$", line)
            if match and re.match(r"^[A-Z0-9(β]", match.group(2)):
                if current_label is not None:
                    footnotes[current_label] = " ".join(current_lines).strip()
                current_label = match.group(1)
                current_lines = [match.group(2)]
                continue
            if current_label is not None:
                if re.match(r"^[A-Z][A-Z0-9 /\-]+$", line):
                    footnotes[current_label] = " ".join(current_lines).strip()
                    current_label = None
                    current_lines = []
                else:
                    current_lines.append(line)
        if current_label is not None:
            footnotes[current_label] = " ".join(current_lines).strip()
        out[page_number] = footnotes
    return out


def _build_page_footnote_asset(
    pages_json: dict[str, Any],
    rule_graph: dict[str, Any],
) -> dict[str, Any]:
    footnotes_by_page = _build_page_footnotes(pages_json)
    page_code_map: dict[int, set[str]] = {}
    for node in rule_graph["nodes"]:
        page_number = node.get("page_number")
        page_code = node.get("page_code")
        if page_number is None or not page_code or page_code == "EXTERNAL":
            continue
        page_code_map.setdefault(page_number, set()).add(page_code)

    pages = []
    for page_number in sorted(footnotes_by_page):
        page_footnotes = footnotes_by_page[page_number]
        pages.append(
            {
                "page_number": page_number,
                "page_codes": sorted(page_code_map.get(page_number, set())),
                "footnotes": [
                    {"label": label, "text": text}
                    for label, text in sorted(page_footnotes.items())
                ],
            }
        )
    return {"pages": pages}


def _build_page_lines(pages_json: dict[str, Any]) -> dict[int, list[str]]:
    out: dict[int, list[str]] = {}
    for page in pages_json["pages"]:
        out[page["page_number"]] = [line.strip() for line in page["text"].splitlines() if line.strip()]
    return out


def _normalize_line(text: str) -> str:
    text = text.replace("•", " ")
    return re.sub(r"\s+", " ", text.strip())


def _window_page_lines(page_lines: list[str], max_window: int = 4) -> list[str]:
    windows: list[str] = []
    line_count = len(page_lines)
    for idx in range(line_count):
        if re.match(r"^[a-z]\s+", page_lines[idx]):
            continue
        combined = []
        for end_idx in range(idx, min(idx + max_window, line_count)):
            if end_idx > idx and re.match(r"^[a-z]\s+", page_lines[end_idx]):
                break
            combined.append(page_lines[end_idx])
            windows.append(" ".join(combined))
    return windows


def _line_has_footnote_suffix(raw: str, valid_labels: set[str]) -> bool:
    pattern = re.compile(r"([A-Za-z][A-Za-z0-9/\-\+\)\]]{2,}?)([a-z](?:,[a-z])*)(?=[\s.;:,]|$)")
    for match in pattern.finditer(raw):
        parts = [part for part in match.group(2).split(",") if part]
        if parts and all(part in valid_labels for part in parts):
            return True
    return False


def _stem_supported_by_node_line(stem: str, node_line: str) -> bool:
    stem_n = _normalize_line(stem).lower()
    node_n = _normalize_line(node_line).lower()
    if not stem_n or len(stem_n) < 4:
        return False
    return stem_n in node_n


def _strip_inline_suffixes(
    text: str,
    valid_labels: set[str],
    node_line: Optional[str] = None,
) -> tuple[str, list[str]]:
    labels: list[str] = []

    def repl(match: re.Match[str]) -> str:
        stem = match.group(1)
        suffix = match.group(2)
        parts = [part for part in suffix.split(",") if part]
        if (
            parts
            and all(part in valid_labels for part in parts)
            and (node_line is None or _stem_supported_by_node_line(stem, node_line))
        ):
            labels.extend(parts)
            return stem
        return match.group(0)

    pattern = re.compile(r"([A-Za-z][A-Za-z0-9/\-\+\)\]]{2,}?)([a-z](?:,[a-z])*)(?=[\s.;:,]|$)")
    stripped = pattern.sub(repl, text)
    return stripped, labels


def _extract_suffix_labels_from_page_lines(
    text: str,
    page_lines: list[str],
    valid_labels: set[str],
) -> list[str]:
    labels: list[str] = []
    seen: set[str] = set()
    node_lines = [_normalize_line(line) for line in text.splitlines() if _normalize_line(line)]
    for node_line in node_lines:
        best = None
        best_score = 0.0
        best_len_diff = None
        best_label_count = None
        for raw in _window_page_lines(page_lines):
            stripped, line_labels = _strip_inline_suffixes(raw, valid_labels, node_line=node_line)
            candidate = _normalize_line(stripped)
            if not candidate:
                continue
            score = 1.0 if node_line == candidate else 0.0
            if score == 0.0:
                from difflib import SequenceMatcher

                score = SequenceMatcher(a=node_line, b=candidate).ratio()
            len_diff = abs(len(candidate) - len(node_line))
            label_count = len(line_labels)
            if (
                score > best_score
                or (
                    abs(score - best_score) < 1e-9
                    and best_len_diff is not None
                    and (
                        len_diff < best_len_diff
                        or (
                            len_diff == best_len_diff
                            and best_label_count is not None
                            and label_count < best_label_count
                        )
                    )
                )
            ):
                best = {
                    "raw": raw,
                    "labels": line_labels,
                    "score": score,
                }
                best_score = score
                best_len_diff = len_diff
                best_label_count = label_count
        if (
            best
            and best["score"] >= 0.90
            and best["labels"]
            and _line_has_footnote_suffix(best["raw"], valid_labels)
        ):
            for label in best["labels"]:
                if label not in seen:
                    seen.add(label)
                    labels.append(label)
    return labels


def _build_footnote_candidates(
    rule_graph: dict[str, Any],
    pages_json: dict[str, Any],
) -> dict[str, Any]:
    page_footnotes = _build_page_footnotes(pages_json)
    page_lines = _build_page_lines(pages_json)
    candidates = []
    for node in rule_graph["nodes"]:
        page_number = node.get("page_number")
        if page_number is None:
            continue
        footnotes = page_footnotes.get(page_number, {})
        labels = _extract_suffix_labels_from_page_lines(
            node.get("verbatim_text", ""),
            page_lines.get(page_number, []),
            set(footnotes),
        )
        if not labels:
            continue
        candidates.append(
            {
                "source_node_id": node["id"],
                "page_code": node["page_code"],
                "page_number": page_number,
                "verbatim_text": node.get("verbatim_text", ""),
                "footnote_candidates": [
                    {
                        "label": label,
                        "text": footnotes.get(label),
                        "link_method": "inline_suffix_on_same_page",
                    }
                    for label in labels
                ],
            }
        )
    return {"candidates": candidates}


def _candidate_index(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {item["source_node_id"]: item for item in payload.get("candidates", [])}


def _flatten_overrides(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    flat: dict[str, dict[str, Any]] = {}
    for page_code, page_overrides in payload.get("pages", {}).items():
        for node_id, override in page_overrides.items():
            item = dict(override)
            item["_page_code"] = page_code
            flat[node_id] = item
    for node_id, override in payload.get("overrides", {}).items():
        flat[node_id] = dict(override)
    return flat


def _emit_links(
    labels: list[str],
    footnotes_by_page: dict[int, dict[str, str]],
    page_number: int,
    method: str,
) -> list[dict[str, Any]]:
    page_footnotes = footnotes_by_page.get(page_number, {})
    return [
        {
            "label": label,
            "text": page_footnotes.get(label),
            "link_method": method,
        }
        for label in labels
    ]


def _normalize_text(text: Optional[str]) -> str:
    if not text:
        return ""
    return re.sub(r"\s+", " ", text).strip()


def _is_short_ref_like_node(text: str) -> bool:
    compact = _normalize_text(text)
    if not compact:
        return True
    if len(compact) <= 80 and (
        "OV-" in compact
        or "LCOC-" in compact
        or compact.startswith("See ")
        or re.search(r"\([A-Z]+-[A-Z0-9]+", compact)
    ):
        return True
    return False


def _is_short_state_like_node(text: str) -> bool:
    compact = _normalize_text(text)
    words = compact.split()
    return len(compact) <= 70 and len(words) <= 10 and "\n" not in text and ";" not in compact


def _keep_auto_links(verbatim_text: str, links: list[dict[str, Any]]) -> list[dict[str, Any]]:
    compact = _normalize_text(verbatim_text)
    if not compact or not links:
        return []
    if _is_short_ref_like_node(compact):
        return []
    if _is_short_state_like_node(compact):
        return []
    return links


def _build_reviewed_footnote_links(
    rule_graph: dict[str, Any],
    pages_json: dict[str, Any],
    footnote_candidates: dict[str, Any],
    overrides_payload: dict[str, Any],
) -> dict[str, Any]:
    footnotes_by_page = _build_page_footnotes(pages_json)
    by_node = _candidate_index(footnote_candidates)
    overrides = _flatten_overrides(overrides_payload)

    reviewed = []
    for node in rule_graph["nodes"]:
        source_node_id = node["id"]
        page_number = node.get("page_number")
        page_code = node.get("page_code")
        if page_number is None:
            continue

        override = overrides.get(source_node_id)
        if override and "exact_labels" in override:
            labels = override["exact_labels"]
            links = _emit_links(labels, footnotes_by_page, page_number, "reviewed_override")
        else:
            links = _keep_auto_links(
                node.get("verbatim_text", ""),
                by_node.get(source_node_id, {}).get("footnote_candidates", []),
            )

        if not links:
            continue

        reviewed.append(
            {
                "source_node_id": source_node_id,
                "page_code": page_code,
                "page_number": page_number,
                "verbatim_text": node.get("verbatim_text", ""),
                "footnotes": links,
            }
        )

    return {"nodes": reviewed}


def _index_explicit_refs(payload: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    refs_by_node: dict[str, list[dict[str, Any]]] = {}
    for bucket in ("rule_graph_refs", "native_md_refs"):
        for ref in payload.get(bucket, []):
            source_node_id = ref.get("source_node_id")
            if not source_node_id:
                continue
            refs_by_node.setdefault(source_node_id, []).append(ref)
    return refs_by_node


def _iter_explicit_refs(explicit_refs: dict[str, Any]) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    for bucket in ("rule_graph_refs", "native_md_refs"):
        refs.extend(explicit_refs.get(bucket, []))
    return refs


def _build_reference_assets(explicit_refs: dict[str, Any]) -> dict[str, Any]:
    assets: dict[str, dict[str, Any]] = {}
    for ref in _iter_explicit_refs(explicit_refs):
        ref_type = ref["ref_type"]
        if ref_type == "single_page_ref":
            asset_id = f"single:{ref['target_page_code']}"
            asset = {
                "asset_id": asset_id,
                "asset_type": "single_page_ref",
                "target_page_code": ref["target_page_code"],
                "mentions": [],
            }
        elif ref_type == "family_indexed_ref":
            asset_id = (
                f"family:{ref['target_page_family']}:{ref['target_page_index']}:{ref['target_page_count']}"
            )
            asset = {
                "asset_id": asset_id,
                "asset_type": "family_indexed_ref",
                "target_page_family": ref["target_page_family"],
                "target_page_index": ref["target_page_index"],
                "target_page_count": ref["target_page_count"],
                "mentions": [],
            }
        elif ref_type == "page_range_ref":
            asset_id = f"range:{ref['target_page_start']}:{ref['target_page_end']}"
            asset = {
                "asset_id": asset_id,
                "asset_type": "page_range_ref",
                "target_page_start": ref["target_page_start"],
                "target_page_end": ref["target_page_end"],
                "mentions": [],
            }
        else:
            continue
        entry = assets.setdefault(asset_id, asset)
        entry["mentions"].append(
            {
                "source_kind": ref.get("source_kind"),
                "source_node_id": ref.get("source_node_id"),
                "source_page_code": ref.get("source_page_code"),
                "condition_text": ref.get("condition_text"),
                "raw_text": ref.get("raw_text"),
            }
        )
    return {"assets": sorted(assets.values(), key=lambda item: item["asset_id"])}


def _build_nccn_taxonomy_asset() -> dict[str, Any]:
    return {
        "evidence_categories": [
            {
                "code": "1",
                "label": "Category 1",
                "definition": "Based upon high-level evidence (≥1 randomized phase 3 trials or high-quality, robust meta-analyses), there is uniform NCCN consensus (≥85% support of the Panel) that the intervention is appropriate.",
            },
            {
                "code": "2A",
                "label": "Category 2A",
                "definition": "Based upon lower-level evidence, there is uniform NCCN consensus (≥85% support of the Panel) that the intervention is appropriate.",
            },
            {
                "code": "2B",
                "label": "Category 2B",
                "definition": "Based upon lower-level evidence, there is NCCN consensus (≥50%, but <85% support of the Panel) that the intervention is appropriate.",
            },
            {
                "code": "3",
                "label": "Category 3",
                "definition": "Based upon any level of evidence, there is major NCCN disagreement that the intervention is appropriate.",
            },
        ],
        "preference_categories": [
            {
                "code": "preferred",
                "label": "Preferred",
                "definition": "Interventions that are based on superior efficacy, safety, and evidence; and, when appropriate, affordability.",
            },
            {
                "code": "other_recommended",
                "label": "Other recommended",
                "definition": "Other interventions that may be somewhat less efficacious, more toxic, or based on less mature data; or significantly less affordable for similar outcomes.",
            },
            {
                "code": "useful_in_certain_circumstances",
                "label": "Useful in certain circumstances",
                "definition": "Other interventions that may be used for selected patient populations (defined with recommendation).",
            },
        ],
        "defaults": {
            "recommendation_default_evidence_category": "2A",
            "note": "All recommendations are category 2A unless otherwise indicated.",
        },
    }


def _extract_taxonomy_annotations(
    *,
    verbatim_text: str,
    footnotes: list[dict[str, Any]],
) -> dict[str, Any]:
    evidence_categories: list[str] = []
    preference_categories: list[str] = []

    for code, pattern in CATEGORY_PATTERNS:
        if pattern.search(verbatim_text):
            evidence_categories.append(code)

    for code, pattern in PREFERENCE_PATTERNS:
        if pattern.search(verbatim_text):
            preference_categories.append(code)

    footnote_mentions = []
    for footnote in footnotes:
        text = footnote.get("text") or ""
        footnote_label = footnote.get("label")
        local_categories = []
        local_preferences = []
        for code, pattern in CATEGORY_PATTERNS:
            if pattern.search(text):
                local_categories.append(code)
        for code, pattern in PREFERENCE_PATTERNS:
            if pattern.search(text):
                local_preferences.append(code)
        if local_categories or local_preferences:
            footnote_mentions.append(
                {
                    "footnote_label": footnote_label,
                    "evidence_categories": local_categories,
                    "preference_categories": local_preferences,
                }
            )

    return {
        "evidence_categories_explicit": sorted(set(evidence_categories)),
        "preference_categories_explicit": sorted(set(preference_categories)),
        "footnote_taxonomy_mentions": footnote_mentions,
    }


def _extract_refs_from_text(
    text: str,
    *,
    source_kind: str,
    source_node_id: str,
    source_page_code: str,
    source_page_number: int,
    footnote_label: Optional[str] = None,
) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []

    for match in FAMILY_INDEXED_RE.finditer(text):
        family, index, count = _family_indexed_parts(match)
        refs.append(
            {
                "source_kind": source_kind,
                "source_node_id": source_node_id,
                "source_page_code": source_page_code,
                "source_page_number": source_page_number,
                "footnote_label": footnote_label,
                "ref_type": "family_indexed_ref",
                "raw_text": match.group(0),
                "condition_text": None,
                "target_page_family": family,
                "target_page_index": index,
                "target_page_count": count,
            }
        )

    for match in PAGE_RANGE_RE.finditer(text):
        refs.append(
            {
                "source_kind": source_kind,
                "source_node_id": source_node_id,
                "source_page_code": source_page_code,
                "source_page_number": source_page_number,
                "footnote_label": footnote_label,
                "ref_type": "page_range_ref",
                "raw_text": match.group(0),
                "condition_text": None,
                "target_page_start": _normalize_page_code(match.group("start")),
                "target_page_end": _normalize_page_code(match.group("end")),
            }
        )

    taken_spans = [m.span() for m in FAMILY_INDEXED_RE.finditer(text)]
    taken_spans += [m.span() for m in PAGE_RANGE_RE.finditer(text)]

    def in_taken(pos: int) -> bool:
        return any(start <= pos < end for start, end in taken_spans)

    for match in SINGLE_PAGE_RE.finditer(text):
        if in_taken(match.start()):
            continue
        refs.append(
            {
                "source_kind": source_kind,
                "source_node_id": source_node_id,
                "source_page_code": source_page_code,
                "source_page_number": source_page_number,
                "footnote_label": footnote_label,
                "ref_type": "single_page_ref",
                "raw_text": match.group(0),
                "condition_text": None,
                "target_page_code": match.group("page"),
            }
        )

    for match in FAMILY_PLAIN_RE.finditer(text):
        if in_taken(match.start()):
            continue
        refs.append(
            {
                "source_kind": source_kind,
                "source_node_id": source_node_id,
                "source_page_code": source_page_code,
                "source_page_number": source_page_number,
                "footnote_label": footnote_label,
                "ref_type": "family_plain_ref",
                "raw_text": match.group(0),
                "condition_text": None,
                "target_page_family": match.group("family"),
            }
        )
    return refs


def _build_footnote_reference_assets(
    reviewed_footnote_links: dict[str, Any],
    page_footnotes: dict[str, Any],
) -> dict[str, Any]:
    assets: dict[str, dict[str, Any]] = {}

    def add_ref(ref: dict[str, Any], mention: dict[str, Any]) -> None:
        ref_type = ref["ref_type"]
        if ref_type == "single_page_ref":
            asset_id = f"single:{ref['target_page_code']}"
            asset = {
                "asset_id": asset_id,
                "asset_type": "single_page_ref",
                "target_page_code": ref["target_page_code"],
                "mentions": [],
            }
        elif ref_type == "family_indexed_ref":
            asset_id = (
                f"family:{ref['target_page_family']}:{ref['target_page_index']}:{ref['target_page_count']}"
            )
            asset = {
                "asset_id": asset_id,
                "asset_type": "family_indexed_ref",
                "target_page_family": ref["target_page_family"],
                "target_page_index": ref["target_page_index"],
                "target_page_count": ref["target_page_count"],
                "mentions": [],
            }
        elif ref_type == "page_range_ref":
            asset_id = f"range:{ref['target_page_start']}:{ref['target_page_end']}"
            asset = {
                "asset_id": asset_id,
                "asset_type": "page_range_ref",
                "target_page_start": ref["target_page_start"],
                "target_page_end": ref["target_page_end"],
                "mentions": [],
            }
        elif ref_type == "family_plain_ref":
            asset_id = f"family_plain:{ref['target_page_family']}"
            asset = {
                "asset_id": asset_id,
                "asset_type": "family_plain_ref",
                "target_page_family": ref["target_page_family"],
                "mentions": [],
            }
        else:
            return
        entry = assets.setdefault(asset_id, asset)
        entry["mentions"].append(mention)

    for node in reviewed_footnote_links.get("nodes", []):
        source_node_id = node["source_node_id"]
        source_page_code = node["page_code"]
        source_page_number = node["page_number"]
        for footnote in node.get("footnotes", []):
            text = footnote.get("text") or ""
            label = footnote.get("label")
            refs = _extract_refs_from_text(
                text,
                source_kind="reviewed_footnote",
                source_node_id=source_node_id,
                source_page_code=source_page_code,
                source_page_number=source_page_number,
                footnote_label=label,
            )
            for ref in refs:
                add_ref(
                    ref,
                    {
                        "source_kind": "reviewed_footnote",
                        "source_node_id": source_node_id,
                        "source_page_code": source_page_code,
                        "source_page_number": source_page_number,
                        "footnote_label": label,
                        "raw_text": ref["raw_text"],
                    },
                )

    for page in page_footnotes.get("pages", []):
        page_number = page["page_number"]
        page_codes = page.get("page_codes", [])
        source_page_code = page_codes[0] if page_codes else None
        for footnote in page.get("footnotes", []):
            text = footnote.get("text") or ""
            label = footnote.get("label")
            refs = _extract_refs_from_text(
                text,
                source_kind="page_footnote",
                source_node_id="",
                source_page_code=source_page_code or "",
                source_page_number=page_number,
                footnote_label=label,
            )
            for ref in refs:
                add_ref(
                    ref,
                    {
                        "source_kind": "page_footnote",
                        "source_node_id": None,
                        "source_page_code": source_page_code,
                        "source_page_number": page_number,
                        "footnote_label": label,
                        "raw_text": ref["raw_text"],
                    },
                )
    return {"assets": sorted(assets.values(), key=lambda item: item["asset_id"])}


def _index_footnote_refs(payload: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    refs_by_node: dict[str, list[dict[str, Any]]] = {}
    for asset in payload.get("assets", []):
        for mention in asset.get("mentions", []):
            source_node_id = mention.get("source_node_id")
            if not source_node_id:
                continue
            refs_by_node.setdefault(source_node_id, []).append(
                {
                    key: value
                    for key, value in asset.items()
                    if key != "mentions"
                }
                | {
                    "footnote_label": mention.get("footnote_label"),
                    "raw_text": mention.get("raw_text"),
                }
            )
    return refs_by_node


def _index_reviewed_footnotes(payload: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    links_by_node: dict[str, list[dict[str, Any]]] = {}
    for item in payload.get("nodes", []):
        source_node_id = item.get("source_node_id")
        if source_node_id:
            links_by_node[source_node_id] = item.get("footnotes", [])
    return links_by_node


def _index_reference_asset_ids(payload: dict[str, Any]) -> dict[str, list[str]]:
    refs_by_node: dict[str, list[str]] = {}
    for asset in payload.get("assets", []):
        asset_id = asset.get("asset_id")
        if not asset_id:
            continue
        for mention in asset.get("mentions", []):
            source_node_id = mention.get("source_node_id")
            if not source_node_id:
                continue
            refs_by_node.setdefault(source_node_id, []).append(asset_id)
    return refs_by_node


def _apply_support_assets_to_rule_graph(
    *,
    rule_graph: dict[str, Any],
    reference_assets: dict[str, Any],
    reviewed_footnote_links: dict[str, Any],
    footnote_reference_assets: dict[str, Any],
) -> dict[str, Any]:
    refs_by_node = _index_reference_asset_ids(reference_assets)
    reviewed_footnotes_by_node = _index_reviewed_footnotes(reviewed_footnote_links)
    footnote_refs_by_node = _index_footnote_refs(footnote_reference_assets)

    enriched_nodes: list[dict[str, Any]] = []
    for node in rule_graph.get("nodes", []):
        node_id = node.get("id")
        page_code = str(node.get("page_code") or "")
        explicit_ref_ids = _dedupe_strings(refs_by_node.get(node_id, []))
        reviewed_footnotes = reviewed_footnotes_by_node.get(node_id, [])
        reviewed_labels = _dedupe_strings(
            [footnote.get("label") for footnote in reviewed_footnotes]
        )
        reviewed_texts = _dedupe_strings(
            [footnote.get("text") for footnote in reviewed_footnotes]
        )
        reviewed_footnote_ids = [
            _reviewed_footnote_id(page_code, label) for label in reviewed_labels
        ]
        reviewed_footnote_ref_ids = _dedupe_strings(
            [asset.get("asset_id") for asset in footnote_refs_by_node.get(node_id, [])]
        )

        enriched_node = dict(node)
        enriched_node["explicit_ref_ids"] = explicit_ref_ids
        enriched_node["explicit_ref_count"] = len(explicit_ref_ids)
        enriched_node["has_explicit_refs"] = bool(explicit_ref_ids)
        enriched_node["reviewed_footnote_ids"] = reviewed_footnote_ids
        enriched_node["reviewed_footnote_labels"] = reviewed_labels
        enriched_node["reviewed_footnote_texts"] = reviewed_texts
        enriched_node["reviewed_footnote_count"] = len(reviewed_footnote_ids)
        enriched_node["has_reviewed_footnotes"] = bool(reviewed_footnote_ids)
        enriched_node["reviewed_footnote_ref_ids"] = reviewed_footnote_ref_ids
        enriched_node["reviewed_footnote_ref_count"] = len(reviewed_footnote_ref_ids)
        enriched_node["has_reviewed_footnote_refs"] = bool(reviewed_footnote_ref_ids)
        enriched_nodes.append(enriched_node)

    return {
        **rule_graph,
        "nodes": enriched_nodes,
    }


def _count_rule_graph_support_fields(rule_graph: dict[str, Any]) -> dict[str, int]:
    nodes = rule_graph.get("nodes", [])
    return {
        "nodes_with_explicit_refs": sum(1 for node in nodes if node.get("has_explicit_refs")),
        "nodes_with_reviewed_footnotes": sum(
            1 for node in nodes if node.get("has_reviewed_footnotes")
        ),
        "nodes_with_reviewed_footnote_refs": sum(
            1 for node in nodes if node.get("has_reviewed_footnote_refs")
        ),
    }


def _default_support_asset_inputs() -> dict[str, Path]:
    roots = ov_2025_roots()
    return {
        "rule_graph_path": roots["rule_graph"] / "ov_2025_global.rule_graph.json",
        "native_md_path": (
            roots["raw_root"]
            / "text_extraction"
            / "22_nccn_ovarian_cancer_v3_2025"
            / "raw"
            / "native"
            / "primary.md"
        ),
        "pages_json_path": (
            roots["raw_root"]
            / "text_extraction"
            / "22_nccn_ovarian_cancer_v3_2025"
            / "raw"
            / "native"
            / "pages.json"
        ),
        "raw_primary_md_path": (
            roots["raw_root"]
            / "text_extraction"
            / "22_nccn_ovarian_cancer_v3_2025"
            / "raw"
            / "primary.md"
        ),
        "overrides_path": REPO_ROOT / "data" / "manifests" / "ov_2025_footnote_link_overrides.json",
        "text_root": roots["processed_root"] / "text",
        "report_root": roots["reports"],
    }


def _build_support_asset_payloads(
    *,
    rule_graph_path: Path,
    native_md_path: Path,
    pages_json_path: Path,
    overrides_path: Path,
    text_root: Path,
) -> dict[str, Any]:
    rule_graph = _read_json(rule_graph_path)
    pages_json = _read_json(pages_json_path)
    overrides_payload = _read_json(overrides_path)

    explicit_refs = {
        "meta": {
            "rule_graph_path": str(rule_graph_path),
            "native_md_path": str(native_md_path),
        },
        "rule_graph_refs": _build_explicit_refs_from_rule_graph(rule_graph),
        "native_md_refs": _build_explicit_refs_from_native_markdown(native_md_path),
    }
    explicit_refs["meta"]["rule_graph_ref_count"] = len(explicit_refs["rule_graph_refs"])
    explicit_refs["meta"]["native_md_ref_count"] = len(explicit_refs["native_md_refs"])

    page_footnotes = _build_page_footnote_asset(pages_json, rule_graph)
    page_footnotes["meta"] = {
        "pages_json_path": str(pages_json_path),
        "page_count": len(page_footnotes["pages"]),
    }

    reference_assets = _build_reference_assets(explicit_refs)
    reference_assets["meta"] = {
        "explicit_refs_path": str(text_root / "ov_2025_explicit_refs.json"),
        "asset_count": len(reference_assets["assets"]),
    }

    footnote_candidates = _build_footnote_candidates(rule_graph, pages_json)
    footnote_candidates["meta"] = {
        "rule_graph_path": str(rule_graph_path),
        "pages_json_path": str(pages_json_path),
        "candidate_count": len(footnote_candidates["candidates"]),
    }

    reviewed_footnote_links = _build_reviewed_footnote_links(
        rule_graph,
        pages_json,
        footnote_candidates,
        overrides_payload,
    )
    reviewed_footnote_links["meta"] = {
        "rule_graph_path": str(rule_graph_path),
        "pages_json_path": str(pages_json_path),
        "candidates_path": str(text_root / "ov_2025_footnote_candidates.json"),
        "overrides_path": str(overrides_path),
        "reviewed_link_count": len(reviewed_footnote_links["nodes"]),
    }

    footnote_reference_assets = _build_footnote_reference_assets(
        reviewed_footnote_links,
        page_footnotes,
    )
    footnote_reference_assets["meta"] = {
        "reviewed_footnote_links_path": str(text_root / "ov_2025_reviewed_footnote_links.json"),
        "asset_count": len(footnote_reference_assets["assets"]),
    }

    nccn_taxonomy = _build_nccn_taxonomy_asset()
    nccn_taxonomy["meta"] = {
        "version": "2025",
        "guideline": "NCCN_OV_2025",
    }

    return {
        "rule_graph": rule_graph,
        "pages_json": pages_json,
        "explicit_refs": explicit_refs,
        "page_footnotes": page_footnotes,
        "reference_assets": reference_assets,
        "footnote_candidates": footnote_candidates,
        "reviewed_footnote_links": reviewed_footnote_links,
        "footnote_reference_assets": footnote_reference_assets,
        "nccn_taxonomy": nccn_taxonomy,
    }


def build_support_assets(
    *,
    rule_graph_path: Optional[Path] = None,
    native_md_path: Optional[Path] = None,
    pages_json_path: Optional[Path] = None,
    overrides_path: Optional[Path] = None,
    text_root: Optional[Path] = None,
    report_root: Optional[Path] = None,
) -> dict[str, Any]:
    defaults = _default_support_asset_inputs()
    rule_graph_path = rule_graph_path or defaults["rule_graph_path"]
    native_md_path = native_md_path or defaults["native_md_path"]
    pages_json_path = pages_json_path or defaults["pages_json_path"]
    overrides_path = overrides_path or defaults["overrides_path"]
    text_root = text_root or defaults["text_root"]
    report_root = report_root or defaults["report_root"]

    payloads = _build_support_asset_payloads(
        rule_graph_path=rule_graph_path,
        native_md_path=native_md_path,
        pages_json_path=pages_json_path,
        overrides_path=overrides_path,
        text_root=text_root,
    )

    explicit_refs_path = text_root / "ov_2025_explicit_refs.json"
    page_footnotes_path = text_root / "ov_2025_page_footnotes.json"
    reference_assets_path = text_root / "ov_2025_reference_assets.json"
    footnote_reference_assets_path = text_root / "ov_2025_footnote_reference_assets.json"
    taxonomy_path = text_root / "ov_2025_nccn_taxonomy.json"
    footnote_candidates_path = text_root / "ov_2025_footnote_candidates.json"
    reviewed_footnote_links_path = text_root / "ov_2025_reviewed_footnote_links.json"
    report_path = report_root / "ov_2025_support_assets_report.json"

    _write_json(explicit_refs_path, payloads["explicit_refs"])
    _write_json(page_footnotes_path, payloads["page_footnotes"])
    _write_json(reference_assets_path, payloads["reference_assets"])
    _write_json(footnote_reference_assets_path, payloads["footnote_reference_assets"])
    _write_json(taxonomy_path, payloads["nccn_taxonomy"])
    _write_json(footnote_candidates_path, payloads["footnote_candidates"])
    _write_json(reviewed_footnote_links_path, payloads["reviewed_footnote_links"])

    report = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "status": "ok",
        "rule_graph_path": str(rule_graph_path),
        "native_md_path": str(native_md_path),
        "pages_json_path": str(pages_json_path),
        "overrides_path": str(overrides_path),
        "explicit_refs_path": str(explicit_refs_path),
        "page_footnotes_path": str(page_footnotes_path),
        "reference_assets_path": str(reference_assets_path),
        "footnote_reference_assets_path": str(footnote_reference_assets_path),
        "taxonomy_path": str(taxonomy_path),
        "footnote_candidates_path": str(footnote_candidates_path),
        "reviewed_footnote_links_path": str(reviewed_footnote_links_path),
        "rule_graph_ref_count": len(payloads["explicit_refs"]["rule_graph_refs"]),
        "native_md_ref_count": len(payloads["explicit_refs"]["native_md_refs"]),
        "page_footnote_page_count": len(payloads["page_footnotes"]["pages"]),
        "reference_asset_count": len(payloads["reference_assets"]["assets"]),
        "footnote_reference_asset_count": len(payloads["footnote_reference_assets"]["assets"]),
        "taxonomy_evidence_category_count": len(payloads["nccn_taxonomy"]["evidence_categories"]),
        "taxonomy_preference_category_count": len(payloads["nccn_taxonomy"]["preference_categories"]),
        "footnote_candidate_count": len(payloads["footnote_candidates"]["candidates"]),
        "reviewed_footnote_node_count": len(payloads["reviewed_footnote_links"]["nodes"]),
    }
    _write_json(report_path, report)

    return {
        "status": "ok",
        "explicit_refs_path": str(explicit_refs_path),
        "page_footnotes_path": str(page_footnotes_path),
        "reference_assets_path": str(reference_assets_path),
        "footnote_reference_assets_path": str(footnote_reference_assets_path),
        "taxonomy_path": str(taxonomy_path),
        "footnote_candidates_path": str(footnote_candidates_path),
        "reviewed_footnote_links_path": str(reviewed_footnote_links_path),
        "report_path": str(report_path),
        "reference_asset_count": len(payloads["reference_assets"]["assets"]),
        "footnote_reference_asset_count": len(payloads["footnote_reference_assets"]["assets"]),
        "reviewed_footnote_node_count": len(payloads["reviewed_footnote_links"]["nodes"]),
    }


def build_query_assets_with_support(
    *,
    rule_graph_path: Optional[Path] = None,
    native_md_path: Optional[Path] = None,
    pages_json_path: Optional[Path] = None,
    overrides_path: Optional[Path] = None,
    text_root: Optional[Path] = None,
    query_root: Optional[Path] = None,
    report_root: Optional[Path] = None,
) -> dict[str, Any]:
    defaults = _default_support_asset_inputs()
    rule_graph_path = rule_graph_path or defaults["rule_graph_path"]
    native_md_path = native_md_path or defaults["native_md_path"]
    pages_json_path = pages_json_path or defaults["pages_json_path"]
    overrides_path = overrides_path or defaults["overrides_path"]
    text_root = text_root or defaults["text_root"]
    report_root = report_root or defaults["report_root"]

    support_result = build_support_assets(
        rule_graph_path=rule_graph_path,
        native_md_path=native_md_path,
        pages_json_path=pages_json_path,
        overrides_path=overrides_path,
        text_root=text_root,
        report_root=report_root,
    )

    rule_graph = _read_json(rule_graph_path)
    reference_assets = _read_json(Path(support_result["reference_assets_path"]))
    footnote_reference_assets = _read_json(
        Path(support_result["footnote_reference_assets_path"])
    )
    reviewed_footnote_links = _read_json(Path(support_result["reviewed_footnote_links_path"]))
    enriched_rule_graph = _apply_support_assets_to_rule_graph(
        rule_graph=rule_graph,
        reference_assets=reference_assets,
        reviewed_footnote_links=reviewed_footnote_links,
        footnote_reference_assets=footnote_reference_assets,
    )
    _write_json(rule_graph_path, enriched_rule_graph)

    refresh_report_path = report_root / "ov_2025_rule_graph_support_refresh_report.json"
    refresh_report = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "status": "ok",
        "rule_graph_path": str(rule_graph_path),
        "reference_assets_path": support_result["reference_assets_path"],
        "footnote_reference_assets_path": support_result["footnote_reference_assets_path"],
        "reviewed_footnote_links_path": support_result["reviewed_footnote_links_path"],
        **_count_rule_graph_support_fields(enriched_rule_graph),
    }
    _write_json(refresh_report_path, refresh_report)

    query_result = build_query_assets(
        rule_graph_path=rule_graph_path,
        query_root=query_root,
        report_root=report_root,
    )
    return {
        **query_result,
        "rule_graph_path": str(rule_graph_path),
        "support_assets_report_path": support_result["report_path"],
        "rule_graph_support_refresh_report_path": str(refresh_report_path),
        "reference_assets_path": support_result["reference_assets_path"],
        "footnote_reference_assets_path": support_result["footnote_reference_assets_path"],
        "reviewed_footnote_links_path": support_result["reviewed_footnote_links_path"],
    }


def build_engine_handoff_assets(
    *,
    rule_graph_path: Optional[Path] = None,
    native_md_path: Optional[Path] = None,
    pages_json_path: Optional[Path] = None,
    overrides_path: Optional[Path] = None,
    text_root: Optional[Path] = None,
    query_root: Optional[Path] = None,
    report_root: Optional[Path] = None,
) -> dict[str, Any]:
    defaults = _default_support_asset_inputs()
    rule_graph_path = rule_graph_path or defaults["rule_graph_path"]
    native_md_path = native_md_path or defaults["native_md_path"]
    pages_json_path = pages_json_path or defaults["pages_json_path"]
    overrides_path = overrides_path or defaults["overrides_path"]
    text_root = text_root or defaults["text_root"]
    report_root = report_root or defaults["report_root"]
    raw_primary_md_path = defaults["raw_primary_md_path"]

    query_result = build_query_assets_with_support(
        rule_graph_path=rule_graph_path,
        native_md_path=native_md_path,
        pages_json_path=pages_json_path,
        overrides_path=overrides_path,
        text_root=text_root,
        query_root=query_root,
        report_root=report_root,
    )

    combined_report_path = report_root / "ov_2025_engine_handoff_assets_report.json"
    combined_report = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "status": "ok",
        "rule_graph_path": str(rule_graph_path),
        "native_md_path": str(native_md_path),
        "pages_json_path": str(pages_json_path),
        "raw_primary_md_path": str(raw_primary_md_path),
        "support_assets_report_path": query_result["support_assets_report_path"],
        "rule_graph_support_refresh_report_path": query_result["rule_graph_support_refresh_report_path"],
        "query_assets_report_path": query_result["report_path"],
        "required_handoff_assets": [
            query_result["query_graph_path"],
            query_result["schema_path"],
            query_result["sample_queries_path"],
            query_result["verbalisation_path"],
            query_result["reference_assets_path"],
            query_result["footnote_reference_assets_path"],
            query_result["reviewed_footnote_links_path"],
            str(raw_primary_md_path),
            str(pages_json_path),
        ],
    }
    _write_json(combined_report_path, combined_report)

    return {
        "status": "ok",
        "rule_graph_path": str(rule_graph_path),
        "native_md_path": str(native_md_path),
        "pages_json_path": str(pages_json_path),
        "raw_primary_md_path": str(raw_primary_md_path),
        "query_graph_path": query_result["query_graph_path"],
        "schema_path": query_result["schema_path"],
        "sample_queries_path": query_result["sample_queries_path"],
        "verbalisation_path": query_result["verbalisation_path"],
        "neo4j_like_path": query_result["neo4j_like_path"],
        "nodes_csv_path": query_result["nodes_csv_path"],
        "edges_csv_path": query_result["edges_csv_path"],
        "reference_assets_path": query_result["reference_assets_path"],
        "footnote_reference_assets_path": query_result["footnote_reference_assets_path"],
        "reviewed_footnote_links_path": query_result["reviewed_footnote_links_path"],
        "support_assets_report_path": query_result["support_assets_report_path"],
        "rule_graph_support_refresh_report_path": query_result["rule_graph_support_refresh_report_path"],
        "query_assets_report_path": query_result["report_path"],
        "report_path": str(combined_report_path),
    }


def build_adu_enrichment(
    *,
    rule_graph_path: Optional[Path] = None,
    adu_path: Optional[Path] = None,
    native_md_path: Optional[Path] = None,
    pages_json_path: Optional[Path] = None,
    overrides_path: Optional[Path] = None,
    text_root: Optional[Path] = None,
    adu_root: Optional[Path] = None,
    report_root: Optional[Path] = None,
) -> dict[str, Any]:
    roots = ov_2025_roots()
    defaults = _default_support_asset_inputs()
    rule_graph_path = rule_graph_path or defaults["rule_graph_path"]
    adu_path = adu_path or (roots["adu"] / "ov_2025_global.adu_draft.json")
    native_md_path = native_md_path or defaults["native_md_path"]
    pages_json_path = pages_json_path or defaults["pages_json_path"]
    overrides_path = overrides_path or defaults["overrides_path"]
    text_root = text_root or defaults["text_root"]
    adu_root = adu_root or roots["adu"]
    report_root = report_root or defaults["report_root"]

    support_result = build_support_assets(
        rule_graph_path=rule_graph_path,
        native_md_path=native_md_path,
        pages_json_path=pages_json_path,
        overrides_path=overrides_path,
        text_root=text_root,
        report_root=report_root,
    )

    rule_graph = _read_json(rule_graph_path)
    adu_payload = _read_json(adu_path)
    explicit_refs = _read_json(Path(support_result["explicit_refs_path"]))
    reference_assets = _read_json(Path(support_result["reference_assets_path"]))
    footnote_reference_assets = _read_json(Path(support_result["footnote_reference_assets_path"]))
    reviewed_footnote_links = _read_json(Path(support_result["reviewed_footnote_links_path"]))
    nccn_taxonomy = _read_json(Path(support_result["taxonomy_path"]))
    page_footnotes_path = Path(support_result["page_footnotes_path"])
    footnote_candidates_path = Path(support_result["footnote_candidates_path"])
    page_footnotes = _read_json(page_footnotes_path)
    footnote_candidates = _read_json(footnote_candidates_path)

    refs_by_node = _index_explicit_refs(explicit_refs)
    footnotes_by_node = _index_reviewed_footnotes(reviewed_footnote_links)
    footnote_refs_by_node = _index_footnote_refs(footnote_reference_assets)
    enriched_adus = []
    enriched_count = 0
    for adu in adu_payload.get("adus", []):
        source_node_id = adu.get("source_node_id")
        refs = refs_by_node.get(source_node_id, [])
        footnotes = footnotes_by_node.get(source_node_id, [])
        footnote_refs = footnote_refs_by_node.get(source_node_id, [])
        taxonomy = _extract_taxonomy_annotations(
            verbatim_text=adu.get("verbatim_text", ""),
            footnotes=footnotes,
        )
        if refs or footnotes or footnote_refs:
            enriched_count += 1
        enriched_adu = dict(adu)
        enriched_adu["enrichment"] = {
            "explicit_refs": refs,
            "footnotes": footnotes,
            "footnote_refs": footnote_refs,
            "taxonomy": taxonomy,
        }
        enriched_adus.append(enriched_adu)

    enriched_payload = {
        "meta": {
            **adu_payload.get("meta", {}),
            "status": "adu_enriched",
            "source_adu_path": str(adu_path),
            "explicit_refs_path": str(text_root / "ov_2025_explicit_refs.json"),
            "footnote_links_path": str(text_root / "ov_2025_reviewed_footnote_links.json"),
            "enriched_count": enriched_count,
            "total_adus": len(enriched_adus),
        },
        "adus": enriched_adus,
    }

    explicit_refs_path = Path(support_result["explicit_refs_path"])
    reference_assets_path = Path(support_result["reference_assets_path"])
    footnote_reference_assets_path = Path(support_result["footnote_reference_assets_path"])
    taxonomy_path = Path(support_result["taxonomy_path"])
    reviewed_footnote_links_path = Path(support_result["reviewed_footnote_links_path"])
    enriched_path = adu_root / "ov_2025_global.adu_enriched.json"
    report_path = report_root / "ov_2025_adu_enrichment_report.json"

    _write_json(enriched_path, enriched_payload)

    report = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "status": "ok",
        "rule_graph_path": str(rule_graph_path),
        "adu_path": str(adu_path),
        "native_md_path": str(native_md_path),
        "pages_json_path": str(pages_json_path),
        "overrides_path": str(overrides_path),
        "explicit_refs_path": str(explicit_refs_path),
        "page_footnotes_path": str(page_footnotes_path),
        "reference_assets_path": str(reference_assets_path),
        "footnote_reference_assets_path": str(footnote_reference_assets_path),
        "taxonomy_path": str(taxonomy_path),
        "footnote_candidates_path": str(footnote_candidates_path),
        "reviewed_footnote_links_path": str(reviewed_footnote_links_path),
        "enriched_path": str(enriched_path),
        "rule_graph_ref_count": len(explicit_refs["rule_graph_refs"]),
        "native_md_ref_count": len(explicit_refs["native_md_refs"]),
        "page_footnote_page_count": len(page_footnotes["pages"]),
        "reference_asset_count": len(reference_assets["assets"]),
        "footnote_reference_asset_count": len(footnote_reference_assets["assets"]),
        "taxonomy_evidence_category_count": len(nccn_taxonomy["evidence_categories"]),
        "taxonomy_preference_category_count": len(nccn_taxonomy["preference_categories"]),
        "footnote_candidate_count": len(footnote_candidates["candidates"]),
        "reviewed_footnote_node_count": len(reviewed_footnote_links["nodes"]),
        "total_adus": len(enriched_adus),
        "enriched_count": enriched_count,
    }
    _write_json(report_path, report)

    return {
        "status": "ok",
        "explicit_refs_path": str(explicit_refs_path),
        "page_footnotes_path": str(page_footnotes_path),
        "reference_assets_path": str(reference_assets_path),
        "footnote_reference_assets_path": str(footnote_reference_assets_path),
        "taxonomy_path": str(taxonomy_path),
        "footnote_candidates_path": str(footnote_candidates_path),
        "reviewed_footnote_links_path": str(reviewed_footnote_links_path),
        "enriched_path": str(enriched_path),
        "report_path": str(report_path),
        "total_adus": len(enriched_adus),
        "enriched_count": enriched_count,
        "reviewed_footnote_node_count": len(reviewed_footnote_links["nodes"]),
    }
