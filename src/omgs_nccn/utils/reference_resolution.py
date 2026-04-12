from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


FAMILY_INDEXED_REF_RE = re.compile(r"\b([A-Z]+-[A-Z])\s*,\s*(\d+)\s+of\s+(\d+)\b")
PAGE_REF_RE = re.compile(r"\b(OV-\d+|LCOC-\d+)\b")
FAMILY_PLAIN_REF_RE = re.compile(r"\b(OV-[A-Z]|LCOC-[A-Z])\b")
PRIMARY_HEADING_RE = re.compile(
    r"^(?:##|###)\s+\[(?P<label>(?P<family>(?:OV|LCOC)-[A-Z])\s+\\\((?P<index>\d+)\s+of\s+(?P<count>\d+)\\\))\]\(#page-(?P<anchor>\d+)-0\)",
    re.MULTILINE,
)


def read_json_any(path: Path) -> Any:
    return json.loads(path.read_text())


def asset_map_by_id(payload: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    if not payload:
        return {}
    return {
        asset["asset_id"]: asset
        for asset in payload.get("assets", [])
        if asset.get("asset_id")
    }


def reviewed_footnotes_by_node(payload: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    if not payload:
        return {}
    return {
        item["source_node_id"]: item
        for item in payload.get("nodes", [])
        if item.get("source_node_id")
    }


def read_pages_json(path: Path) -> list[dict[str, Any]]:
    payload = read_json_any(path)
    if isinstance(payload, dict):
        return list(payload.get("pages", []))
    if isinstance(payload, list):
        return payload
    return []


def pages_by_number(path: Path) -> dict[int, dict[str, Any]]:
    pages = read_pages_json(path)
    return {
        int(page["page_number"]): page
        for page in pages
        if isinstance(page, dict) and page.get("page_number") is not None
    }


def truncate_text(text: str, *, limit: int = 500) -> str:
    normalized = re.sub(r"\s+", " ", text).strip()
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 3].rstrip() + "..."


def indexed_reference_page_lookup(
    *,
    primary_md_path: Path,
    pages_by_number_map: dict[int, dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    text = primary_md_path.read_text()
    indexed_by_asset_id: dict[str, dict[str, Any]] = {}
    family_entries: dict[str, list[dict[str, Any]]] = {}
    matches = list(PRIMARY_HEADING_RE.finditer(text))
    for idx, match in enumerate(matches):
        body_start = match.end()
        body_end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
        body = text[body_start:body_end].strip()
        family = match.group("family")
        page_index = int(match.group("index"))
        page_count = int(match.group("count"))
        asset_id = f"family:{family}:{page_index}:{page_count}"
        anchor_page = int(match.group("anchor"))
        page_number = anchor_page + 1
        page_payload = pages_by_number_map.get(page_number) or pages_by_number_map.get(anchor_page)
        page_text = ""
        if page_payload:
            page_text = str(page_payload.get("text") or page_payload.get("markdown") or "")
        excerpt_source = page_text or body
        entry = {
            "asset_id": asset_id,
            "target_label": match.group("label"),
            "target_page_family": family,
            "target_page_index": page_index,
            "target_page_count": page_count,
            "page_number": page_number,
            "content_excerpt": truncate_text(excerpt_source, limit=700),
        }
        indexed_by_asset_id[asset_id] = entry
        family_entries.setdefault(family, []).append(entry)
    for family in family_entries:
        family_entries[family] = sorted(
            family_entries[family],
            key=lambda item: (item["target_page_index"], item["page_number"]),
        )
    return indexed_by_asset_id, family_entries


def enrich_supporting_asset(
    asset: dict[str, Any],
    *,
    indexed_pages_by_asset_id: dict[str, dict[str, Any]],
    family_entries: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    enriched = dict(asset)
    asset_id = asset.get("asset_id")
    family = asset.get("target_page_family")
    if asset_id and asset_id in indexed_pages_by_asset_id:
        enriched["resolved_ref_page"] = indexed_pages_by_asset_id[asset_id]
    elif family and family in family_entries:
        overview_entries = family_entries[family][:4]
        enriched["resolved_ref_family_overview"] = {
            "target_page_family": family,
            "available_pages": [
                {
                    "target_label": item["target_label"],
                    "page_number": item["page_number"],
                }
                for item in overview_entries
            ],
            "overview_excerpt": overview_entries[0]["content_excerpt"],
        }
    return enriched


def inline_ref_asset_ids_from_text(
    text: str,
    *,
    reference_assets_by_id: dict[str, dict[str, Any]],
    footnote_reference_assets_by_id: dict[str, dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    explicit_assets: list[dict[str, Any]] = []
    footnote_assets: list[dict[str, Any]] = []
    seen_explicit: set[str] = set()
    seen_footnote: set[str] = set()
    for family, idx, count in FAMILY_INDEXED_REF_RE.findall(text):
        asset_id = f"family:{family}:{idx}:{count}"
        asset = reference_assets_by_id.get(asset_id)
        if asset and asset_id not in seen_explicit:
            seen_explicit.add(asset_id)
            explicit_assets.append(asset)
    for ref in PAGE_REF_RE.findall(text):
        for prefix in ("single:", "range_member:"):
            asset_id = f"{prefix}{ref}"
            asset = reference_assets_by_id.get(asset_id)
            if asset and asset_id not in seen_explicit:
                seen_explicit.add(asset_id)
                explicit_assets.append(asset)
    for family in FAMILY_PLAIN_REF_RE.findall(text):
        asset_id = f"family_plain:{family}"
        asset = footnote_reference_assets_by_id.get(asset_id)
        if asset and asset_id not in seen_footnote:
            seen_footnote.add(asset_id)
            footnote_assets.append(asset)
    return {
        "explicit_refs": explicit_assets,
        "footnote_supporting_refs": footnote_assets,
    }
