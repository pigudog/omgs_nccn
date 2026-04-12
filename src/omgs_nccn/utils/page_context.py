from __future__ import annotations

from typing import Any


def page_directory(query_graph: dict[str, Any]) -> list[dict[str, Any]]:
    pages: dict[str, dict[str, Any]] = {}
    for node in query_graph.get("nodes", []):
        page_code = node.get("page_code")
        if not page_code or page_code == "EXTERNAL":
            continue
        record = pages.setdefault(
            page_code,
            {
                "page_code": page_code,
                "page_number": node.get("page_number"),
                "page_title": node.get("page_title"),
                "page_scope_summary": node.get("page_scope_summary"),
            },
        )
        if record.get("page_number") is None and node.get("page_number") is not None:
            record["page_number"] = node.get("page_number")
        if not record.get("page_title") and node.get("page_title"):
            record["page_title"] = node.get("page_title")
        if not record.get("page_scope_summary") and node.get("page_scope_summary"):
            record["page_scope_summary"] = node.get("page_scope_summary")
    return sorted(
        pages.values(),
        key=lambda item: (item.get("page_number") or 9999, str(item.get("page_code") or "")),
    )
