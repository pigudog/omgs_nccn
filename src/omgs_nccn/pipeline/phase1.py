from __future__ import annotations

import base64
import json
import re
from dataclasses import asdict
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any

from pypdf import PdfReader

from omgs_nccn.config.paths import ov_2025_roots
from omgs_nccn.llm import init_client_from_config
from omgs_nccn.nccn.schemas import PageAsset
from omgs_nccn.nccn.schemas import PageDimensions
from omgs_nccn.nccn.schemas import PageInventoryRecord


OV_FLOWCHART_RE = re.compile(r"\b(OV-\d+)\b")
OV_PRINCIPLES_RE = re.compile(r"\b(OV-[A-Z])\b")
LCOC_FLOWCHART_RE = re.compile(r"\b(LCOC-\d+)\b")
LCOC_AUX_RE = re.compile(r"\b(LCOC-[A-Z])\b")
ST_RE = re.compile(r"\b(ST-\d+)\b")
ABBR_RE = re.compile(r"\b(ABBR-\d+)\b")
OV_2025_INITIAL_IN_SCOPE_PAGE_RANGE = range(8, 30)
OV_2025_APPROVED_PAGE_MAP = {
    "OV-1": 8,
    "OV-2": 9,
    "OV-3": 10,
    "OV-4": 11,
    "OV-5": 12,
    "OV-6": 13,
    "OV-7": 14,
    "OV-8": 15,
    "LCOC-1": 16,
    "LCOC-2": 17,
    "LCOC-3": 18,
    "LCOC-4": 19,
    "LCOC-5": 20,
    "LCOC-6": 21,
    "LCOC-7": 22,
    "LCOC-8": 23,
    "LCOC-9": 24,
    "LCOC-10": 25,
    "LCOC-11": 26,
    "LCOC-12": 27,
    "LCOC-13": 28,
    "LCOC-14": 29,
}


def ensure_ov_2025_layout() -> dict[str, Path]:
    roots = ov_2025_roots()
    for path in roots.values():
        path.mkdir(parents=True, exist_ok=True)
    (roots["processed_root"] / "pages").mkdir(parents=True, exist_ok=True)
    return roots


def _extract_page_label(text: str) -> str | None:
    for pattern in (
        OV_FLOWCHART_RE,
        OV_PRINCIPLES_RE,
        LCOC_FLOWCHART_RE,
        LCOC_AUX_RE,
        ST_RE,
        ABBR_RE,
    ):
        match = pattern.search(text)
        if match:
            return match.group(1)
    return None


def classify_page(text: str, page_label: str | None) -> tuple[str, bool, str]:
    if page_label and OV_FLOWCHART_RE.fullmatch(page_label):
        return ("flowchart", True, "page_label_matches_ov_flowchart")
    if page_label and OV_PRINCIPLES_RE.fullmatch(page_label):
        return ("principles", False, "page_label_matches_ov_principles")
    if page_label and LCOC_FLOWCHART_RE.fullmatch(page_label):
        return ("flowchart", True, "page_label_matches_lcoc_flowchart")
    if page_label and (LCOC_AUX_RE.fullmatch(page_label) or page_label.startswith("ST-")):
        return ("other", False, "page_label_out_of_scope_for_phase1")

    lowered = text.lower()
    if "discussion" in lowered or "table of contents" in lowered:
        return ("discussion", False, "discussion_or_table_of_contents")
    return ("other", False, "no_in_scope_ov_label_detected")


def extract_page_assets_from_pdf(
    pdf_path: Path,
    document_id: str = "ov_2025",
) -> list[PageAsset]:
    reader = PdfReader(str(pdf_path))
    assets: list[PageAsset] = []

    for index, page in enumerate(reader.pages, start=1):
        text = page.extract_text() or ""
        page_label = _extract_page_label(text)
        width = float(page.mediabox.width)
        height = float(page.mediabox.height)
        image_path = ov_2025_roots()["page_assets"] / f"page_{index:03d}.png"
        assets.append(
            PageAsset(
                document_id=document_id,
                page_index=index,
                page_label=page_label,
                image_path=str(image_path),
                embedded_text=text,
                extracted_text=text,
                page_dimensions=PageDimensions(width=width, height=height),
                source_pdf_path=str(pdf_path),
            )
        )
    return assets


def build_page_inventory_from_assets(
    assets: list[PageAsset],
) -> list[PageInventoryRecord]:
    records: list[PageInventoryRecord] = []
    for asset in assets:
        page_type, in_scope, inclusion_reason = classify_page(
            asset.extracted_text,
            asset.page_label,
        )
        approved_page_index = OV_2025_APPROVED_PAGE_MAP.get(asset.page_label or "")
        if approved_page_index is not None:
            page_type = "flowchart"
            in_scope = True
            inclusion_reason = "approved_phase1_page_label"
        elif asset.page_index in OV_2025_INITIAL_IN_SCOPE_PAGE_RANGE:
            page_type = "flowchart"
            in_scope = True
            inclusion_reason = "approved_initial_ov_2025_flowchart_range"
        records.append(
            PageInventoryRecord(
                document_id=asset.document_id,
                page_index=asset.page_index,
                page_label=asset.page_label,
                page_type=page_type,
                in_scope=in_scope,
                inclusion_reason=inclusion_reason,
                source_pdf_path=asset.source_pdf_path,
            )
        )
    return records


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")


def initialize_phase1_ov_2025(pdf_path: Path) -> dict[str, Any]:
    roots = ensure_ov_2025_layout()
    assets = extract_page_assets_from_pdf(pdf_path)
    inventory = build_page_inventory_from_assets(assets)

    asset_manifest_path = roots["page_assets"] / "manifest.json"
    inventory_path = roots["page_assets"] / "page_inventory.json"

    _write_json(
        asset_manifest_path,
        {
            "document_id": "ov_2025",
            "source_pdf_path": str(pdf_path),
            "page_count": len(assets),
            "pages": [asset.to_dict() for asset in assets],
        },
    )
    _write_json(
        inventory_path,
        {
            "document_id": "ov_2025",
            "source_pdf_path": str(pdf_path),
            "records": [record.to_dict() for record in inventory],
        },
    )

    in_scope_pages = [record.page_index for record in inventory if record.in_scope]
    summary = {
        "document_id": "ov_2025",
        "source_pdf_path": str(pdf_path),
        "page_count": len(assets),
        "in_scope_page_count": len(in_scope_pages),
        "in_scope_pages": in_scope_pages,
        "page_assets_manifest": str(asset_manifest_path),
        "page_inventory_manifest": str(inventory_path),
    }
    _write_json(roots["reports"] / "phase1_init_summary.json", summary)
    return summary


def _load_page_inventory_records(
    inventory_path: Path | None = None,
) -> list[dict[str, Any]]:
    roots = ensure_ov_2025_layout()
    inventory_path = inventory_path or (roots["page_assets"] / "page_inventory.json")
    payload = json.loads(inventory_path.read_text())
    return payload["records"]


def _page_record_by_label(
    page_label: str,
    inventory_path: Path | None = None,
) -> dict[str, Any]:
    records = _load_page_inventory_records(inventory_path=inventory_path)
    approved_page_index = OV_2025_APPROVED_PAGE_MAP.get(page_label)
    if approved_page_index is not None:
        for record in records:
            if int(record["page_index"]) == approved_page_index:
                return record

    matches = [record for record in records if record.get("page_label") == page_label]
    if not matches:
        raise KeyError(f"Page label not found in inventory: {page_label}")

    in_scope_matches = [record for record in matches if record.get("in_scope")]
    if in_scope_matches:
        return sorted(in_scope_matches, key=lambda item: int(item["page_index"]))[0]

    flowchart_matches = [
        record for record in matches if record.get("page_type") == "flowchart"
    ]
    if flowchart_matches:
        return sorted(flowchart_matches, key=lambda item: int(item["page_index"]))[0]

    return sorted(matches, key=lambda item: int(item["page_index"]))[0]


def _page_dir(page_label: str) -> Path:
    return ov_2025_roots()["processed_root"] / "pages" / page_label


def _image_path_for_page(page_index: int, image_root: Path) -> Path:
    return image_root / f"page_{page_index:03d}.png"


def _page_id_prefix(page_label: str) -> str:
    return page_label.replace("-", "")


def _encode_image_as_data_url(image_path: Path) -> str:
    with image_path.open("rb") as handle:
        encoded = base64.b64encode(handle.read()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def _node_inventory_system_prompt() -> str:
    return (
        "You are a precise parser for NCCN medical flowcharts. "
        "Your job is to identify real flowchart nodes only. "
        "Do not summarize the page. Do not invent hidden nodes. "
        "Do not convert headers, footnotes, page furniture, or section titles into nodes."
    )


def _node_inventory_user_prompt(page_label: str, page_number: int) -> str:
    page_prefix = _page_id_prefix(page_label)
    return (
        "Task: extract a node inventory for this single NCCN flowchart page.\n"
        "Important rules:\n"
        "1. A node is only a real flowchart box, decision box, action box, stage box, or an explicit cross-page destination box.\n"
        "2. Do NOT treat page headers, NCCN boilerplate, section headers, note blocks, or footnote paragraphs as nodes.\n"
        "3. Do NOT infer edges yet.\n"
        "4. If uncertain whether something is a node, include it only if it is visually part of the flowchart.\n"
        "5. Keep original wording as much as possible.\n\n"
        "Return exactly one JSON object with this schema:\n"
        "{\n"
        f'  "page_code": "{page_label}",\n'
        f'  "page_number": {page_number},\n'
        '  "status": "draft_for_manual_review",\n'
        '  "excluded_text_blocks": [str],\n'
        '  "jump_refs": [{"raw_label": str, "target_label_guess": str|null}],\n'
        '  "nodes": [\n'
        "    {\n"
        '      "id": str,\n'
        '      "verbatim_text": str,\n'
        '      "column": str|null,\n'
        '      "is_uncertain": bool,\n'
        '      "why_node": str\n'
        "    }\n"
        "  ],\n"
        '  "notes": [str]\n'
        "}\n\n"
        f"Use ids like {page_prefix}_N01, {page_prefix}_N02 in approximate reading order from upper-left to lower-right.\n"
        "Do not assign node_type yet. Do not generate text_snippet.\n"
        "Return JSON only. No markdown fences."
    )


def _edge_extraction_system_prompt() -> str:
    return (
        "You are a precise parser for NCCN medical flowcharts. "
        "Your task is to extract visible flow edges only between an existing set of nodes. "
        "Do not invent new nodes. Do not infer hidden edges unless visually strongly supported."
    )


def _edge_extraction_user_prompt(
    page_label: str,
    page_number: int,
    node_inventory: list[dict[str, Any]],
) -> str:
    page_prefix = _page_id_prefix(page_label)
    nodes_json = json.dumps(node_inventory, ensure_ascii=False, indent=2)
    return (
        "Task: edge extraction for a single NCCN flowchart page.\n"
        "You are given an image and an approved draft node inventory.\n"
        "Use only the provided node ids.\n\n"
        "Rules:\n"
        "1. Extract only visible or strongly supported directed flow edges.\n"
        "2. Do not create new nodes.\n"
        "3. If one source visibly branches to multiple targets, include multiple edges.\n"
        "4. If a relationship is ambiguous, either omit it or mark it uncertain in notes.\n"
        "5. Do not connect page headers or excluded text blocks.\n\n"
        "Return exactly one JSON object with this schema:\n"
        "{\n"
        f'  "page_code": "{page_label}",\n'
        f'  "page_number": {page_number},\n'
        '  "status": "draft_for_manual_review",\n'
        '  "edges": [\n'
        "    {\n"
        '      "id": str,\n'
        '      "source_node_id": str,\n'
        '      "target_node_id": str,\n'
        '      "is_uncertain": bool,\n'
        '      "why_edge": str\n'
        "    }\n"
        "  ],\n"
        '  "notes": [str]\n'
        "}\n\n"
        f"Use edge ids like {page_prefix}_E01, {page_prefix}_E02 in approximate reading order.\n\n"
        "Do not assign edge_type yet. Do not assign edge_label yet.\n\n"
        "Node inventory:\n"
        f"{nodes_json}\n\n"
        "Return JSON only. No markdown fences."
    )


def _call_llm_json(
    *,
    model: str,
    db_path: Path,
    system_prompt: str,
    user_prompt: str,
    image_path: Path,
) -> dict[str, Any]:
    data_url = _encode_image_as_data_url(image_path)
    messages = [
        {"role": "system", "content": system_prompt},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": user_prompt},
                {"type": "image_url", "image_url": {"url": data_url}},
            ],
        },
    ]
    client = init_client_from_config(model, db_path=str(db_path))
    response = client.chat_completion(
        model=model,
        messages=messages,
        max_completion_tokens=50000,
    )
    content = response.choices[0].message.content
    return {
        "response_text": content,
        "response_json": json.loads(content),
        "finish_reason": response.choices[0].finish_reason,
        "usage": response.usage.model_dump() if getattr(response, "usage", None) else None,
    }


def build_llm_drafts_for_pages(
    *,
    page_labels: list[str],
    image_root: Path,
    inventory_path: Path | None = None,
    model: str = "gpt-5.1",
) -> dict[str, Any]:
    ensure_ov_2025_layout()
    run_summary: dict[str, Any] = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "model": model,
        "image_root": str(image_root),
        "pages": [],
    }

    for page_label in page_labels:
        record = _page_record_by_label(page_label, inventory_path=inventory_path)
        page_index = int(record["page_index"])
        page_dir = _page_dir(page_label)
        page_dir.mkdir(parents=True, exist_ok=True)
        image_path = _image_path_for_page(page_index, image_root)
        if not image_path.exists():
            raise FileNotFoundError(f"Missing page image for {page_label}: {image_path}")

        node_payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "page_code": page_label,
            "page_number": page_index,
            "image_path": str(image_path),
            "status": "started",
            "model": model,
        }
        try:
            node_result = _call_llm_json(
                model=model,
                db_path=page_dir / "llm_node_draft.trace.db",
                system_prompt=_node_inventory_system_prompt(),
                user_prompt=_node_inventory_user_prompt(page_label, page_index),
                image_path=image_path,
            )
            node_payload["status"] = "ok"
            node_payload.update(node_result)
        except Exception as exc:
            node_payload["status"] = "error"
            node_payload["error_type"] = type(exc).__name__
            node_payload["error"] = str(exc)
            _write_json(page_dir / "llm_node_draft.json", node_payload)
            run_summary["pages"].append(
                {
                    "page_code": page_label,
                    "page_number": page_index,
                    "status": "node_error",
                    "page_dir": str(page_dir),
                }
            )
            continue

        _write_json(page_dir / "llm_node_draft.json", node_payload)

        edge_payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "page_code": page_label,
            "page_number": page_index,
            "image_path": str(image_path),
            "node_draft_path": str(page_dir / "llm_node_draft.json"),
            "status": "started",
            "model": model,
        }
        try:
            edge_result = _call_llm_json(
                model=model,
                db_path=page_dir / "llm_edge_draft.trace.db",
                system_prompt=_edge_extraction_system_prompt(),
                user_prompt=_edge_extraction_user_prompt(
                    page_label,
                    page_index,
                    node_payload["response_json"]["nodes"],
                ),
                image_path=image_path,
            )
            edge_payload["status"] = "ok"
            edge_payload.update(edge_result)
        except Exception as exc:
            edge_payload["status"] = "error"
            edge_payload["error_type"] = type(exc).__name__
            edge_payload["error"] = str(exc)
            _write_json(page_dir / "llm_edge_draft.json", edge_payload)
            run_summary["pages"].append(
                {
                    "page_code": page_label,
                    "page_number": page_index,
                    "status": "edge_error",
                    "page_dir": str(page_dir),
                }
            )
            continue

        _write_json(page_dir / "llm_edge_draft.json", edge_payload)
        run_summary["pages"].append(
            {
                "page_code": page_label,
                "page_number": page_index,
                "status": "ok",
                "page_dir": str(page_dir),
                "node_count": len(node_payload["response_json"].get("nodes", [])),
                "edge_count": len(edge_payload["response_json"].get("edges", [])),
            }
        )

    summary_path = ov_2025_roots()["reports"] / "llm_draft_run_summary.json"
    _write_json(summary_path, run_summary)
    run_summary["summary_path"] = str(summary_path)
    return run_summary


def _estimate_bbox(
    *,
    column: str | None,
    node_type: str,
    verbatim_text: str,
    row_index: int,
) -> list[float]:
    canonical_column_x = {
        "left": 80.0,
        "left-center": 360.0,
        "center": 640.0,
        "right-center": 980.0,
        "right": 1280.0,
        "far-right": 1560.0,
        "bottom-center": 980.0,
    }
    x = canonical_column_x.get(column or "", 360.0)
    lines = max(1, sum(1 for line in verbatim_text.splitlines() if line.strip()))
    text_len = max(len(verbatim_text), len(verbatim_text.replace("\n", " ")))
    width = min(420.0, max(180.0, float(text_len) * 3.2))
    if node_type == "cross_page":
        width = min(width, 280.0)
    if node_type == "stage":
        width = min(width, 240.0)
    height = max(56.0, 28.0 * lines + 20.0)
    y = 120.0 + row_index * 110.0
    if column == "bottom-center":
        y = 900.0
    return [round(x, 1), round(y, 1), round(width, 1), round(height, 1)]


def _to_text_snippet(verbatim_text: str) -> str:
    text = verbatim_text.strip()
    return f"{text[:85]}..." if len(text) > 88 else text


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def _build_page_graph_draft_for_page(page_label: str) -> dict[str, Any]:
    page_dir = _page_dir(page_label)
    node_draft = _load_json(page_dir / "llm_node_draft.json")
    edge_draft = _load_json(page_dir / "llm_edge_draft.json")

    node_payload = node_draft["response_json"]
    edge_payload = edge_draft["response_json"]
    llm_nodes = node_payload.get("nodes", [])
    llm_edges = edge_payload.get("edges", [])

    nodes_by_id: dict[str, dict[str, Any]] = {}
    nodes: list[dict[str, Any]] = []
    for idx, llm_node in enumerate(llm_nodes):
        canonical_type = "process"
        verbatim_text = llm_node.get("verbatim_text", "").strip()
        draft_node = {
            "id": llm_node["id"],
            "node_type": canonical_type,
            "is_uncertain": bool(llm_node.get("is_uncertain", False)),
            "why_node": llm_node.get("why_node"),
            "verbatim_text": verbatim_text,
            "text_snippet": _to_text_snippet(verbatim_text),
            "bbox": _estimate_bbox(
                column=llm_node.get("column"),
                node_type=canonical_type,
                verbatim_text=verbatim_text,
                row_index=idx,
            ),
        }
        nodes.append(draft_node)
        nodes_by_id[draft_node["id"]] = draft_node

    edges: list[dict[str, Any]] = []
    for llm_edge in llm_edges:
        draft_edge = {
            "id": llm_edge["id"],
            "source_node_id": llm_edge["source_node_id"],
            "target_node_id": llm_edge["target_node_id"],
            "is_uncertain": bool(llm_edge.get("is_uncertain", False)),
            "edge_type": "flow",
            "edge_label": None,
            "why_edge": llm_edge.get("why_edge"),
        }
        edges.append(draft_edge)

    page_graph_draft = {
        "page_code": node_payload["page_code"],
        "page_number": node_payload["page_number"],
        "graph_type": "directed",
        "nodes": nodes,
        "edges": edges,
    }
    _write_json(page_dir / "page_graph.draft.json", page_graph_draft)
    return {
        "page_code": page_label,
        "page_number": page_graph_draft["page_number"],
        "page_dir": str(page_dir),
        "draft_path": str(page_dir / "page_graph.draft.json"),
        "status": "ok",
        "node_count": len(nodes),
        "edge_count": len(edges),
    }


def build_page_graph_drafts_for_pages(page_labels: list[str]) -> dict[str, Any]:
    ensure_ov_2025_layout()
    summary = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "pages": [],
    }
    for page_label in page_labels:
        summary["pages"].append(_build_page_graph_draft_for_page(page_label))
    summary_path = ov_2025_roots()["reports"] / "page_graph_draft_run_summary.json"
    _write_json(summary_path, summary)
    summary["summary_path"] = str(summary_path)
    return summary
