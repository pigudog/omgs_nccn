from __future__ import annotations

import base64
import json
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any

from omgs_nccn.config.paths import ov_2025_roots
from omgs_nccn.llm import init_client_from_config


VALID_NODE_LABELS = {
    "Disease Condition",
    "Evaluation",
    "Treatment Option",
    "Page Jump",
}
VALID_EDGE_LABELS = {"requires", "indicates", "is_followed_by"}
FLOWCHART_PAGE_PREFIXES = ("OV-", "LCOC-")
SUPPLEMENT_PAGE_PREFIXES = ("OV-A", "OV-B", "OV-C", "OV-D", "OV-E", "LCOC-A", "LCOC-B")


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def _reviewed_page_path(page_code: str, input_root: Path) -> Path:
    return input_root / page_code / "page_graph.reviewed.json"


def _typed_page_path(page_code: str, input_root: Path) -> Path:
    return input_root / page_code / "page_graph.typed.json"


def _typed_audit_path(page_code: str, input_root: Path) -> Path:
    return input_root / page_code / "page_graph.typed.audit.json"


def _page_context_path(page_code: str, input_root: Path) -> Path:
    return input_root / page_code / "page_context.json"


def _node_semantics_path(page_code: str, input_root: Path) -> Path:
    return input_root / page_code / "page_node_semantics.json"


def _edge_relations_path(page_code: str, input_root: Path) -> Path:
    return input_root / page_code / "page_edge_relations.json"


def _load_page_text_by_number(pages_json_path: Path) -> dict[int, str]:
    payload = _read_json(pages_json_path)
    pages = payload.get("pages", [])
    result: dict[int, str] = {}
    for item in pages:
        number = item.get("page_number")
        text = item.get("text", "")
        if isinstance(number, int):
            result[number] = text
    return result


def _encode_image_as_data_url(image_path: Path) -> str:
    with image_path.open("rb") as handle:
        encoded = base64.b64encode(handle.read()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def _to_text_snippet(text: str) -> str:
    value = text.strip()
    return f"{value[:85]}..." if len(value) > 88 else value


def _call_llm_json(
    *,
    model: str,
    db_path: Path,
    system_prompt: str,
    user_prompt: str,
    image_path: Path | None = None,
) -> dict[str, Any]:
    client = init_client_from_config(model, db_path=str(db_path))
    user_content: Any
    if image_path is None:
        user_content = user_prompt
    else:
        user_content = [
            {"type": "text", "text": user_prompt},
            {"type": "image_url", "image_url": {"url": _encode_image_as_data_url(image_path)}},
        ]
    response = client.chat_completion(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        max_completion_tokens=4000,
    )
    content = response.choices[0].message.content
    return json.loads(content)


def _page_context_system_prompt() -> str:
    return (
        "You extract page-level context from a single NCCN guideline flowchart page. "
        "Use the page image and the provided reviewed node texts. "
        "Return only guideline_header, page_title, and page_scope_summary. "
        "Do not type nodes. Do not type edges. Do not extract footnotes. "
        "Do not include NCCN footer furniture, references, or discussion prose."
    )


def _page_context_user_prompt(
    *,
    page_code: str,
    page_number: int,
    page_text: str,
    nodes: list[dict[str, Any]],
) -> str:
    node_texts = [{"id": node["id"], "text": node["verbatim_text"]} for node in nodes]
    return (
        "Task: extract page-level context for one NCCN flowchart page.\n\n"
        "Return exactly one JSON object with this schema:\n"
        "{\n"
        f'  "page_code": "{page_code}",\n'
        '  "guideline_header": str,\n'
        '  "page_title": str,\n'
        '  "page_scope_summary": str\n'
        "}\n\n"
        "Rules:\n"
        "1. guideline_header should be the top NCCN guideline line for the page when visible.\n"
        "2. page_title should be the main page title.\n"
        "3. page_scope_summary should be one short sentence explaining what this page does in the guideline.\n"
        "4. Do not list headers, node ids, footnotes, or page furniture.\n"
        "5. Keep wording close to the page.\n\n"
        f"Page number: {page_number}\n"
        f"Native page text:\n{page_text}\n\n"
        f"Reviewed node texts:\n{json.dumps(node_texts, ensure_ascii=False, indent=2)}\n\n"
        "Return JSON only."
    )


def _node_label_system_prompt() -> str:
    return (
        "You are classifying NCCN clinical guideline nodes.\n"
        "Allowed node labels:\n"
        "- Disease Condition\n"
        "- Evaluation\n"
        "- Treatment Option\n"
        "- Page Jump\n"
        "Return one label per node.\n"
        "Do not change node text.\n"
        "Do not invent extra label types."
    )


def _node_label_user_prompt(
    *,
    page_code: str,
    page_context: dict[str, Any],
    nodes: list[dict[str, Any]],
) -> str:
    node_payload = [{"id": node["id"], "text": node["verbatim_text"]} for node in nodes]
    return (
        "Task: assign one node label to each reviewed node on this NCCN flowchart page.\n\n"
        "Return exactly one JSON object with this schema:\n"
        "{\n"
        f'  "page_code": "{page_code}",\n'
        '  "nodes": [\n'
        "    {\n"
        '      "id": str,\n'
        '      "node_label": "Disease Condition"|"Evaluation"|"Treatment Option"|"Page Jump",\n'
        '      "rationale": str\n'
        "    }\n"
        "  ]\n"
        "}\n\n"
        "Rules:\n"
        "1. Return exactly one record for each input node id.\n"
        "2. You must choose one of the four allowed labels for every node; do not return null.\n"
        "3. Use Page Jump only for explicit main-flow cross-page continuation boxes that jump to another numbered flowchart page, such as OV-2, OV-4, OV-5, OV-6, OV-7, OV-8, or LCOC-1.\n"
        "4. Do not use Page Jump for principles/supplement references such as OV-A, OV-B, OV-C, OV-D, OV-E, LCOC-A, or LCOC-B. Those usually remain Evaluation or Treatment Option based on the node text.\n"
        "5. Disease state, stage, eligibility, and status nodes should usually be Disease Condition.\n"
        "6. Workup, testing, assessment, diagnosis, and evaluation steps should usually be Evaluation.\n"
        "7. Surgery, systemic therapy, maintenance therapy, surveillance plans, and other management options should usually be Treatment Option.\n\n"
        f"Page context:\n{json.dumps(page_context, ensure_ascii=False, indent=2)}\n\n"
        f"Reviewed nodes:\n{json.dumps(node_payload, ensure_ascii=False, indent=2)}\n\n"
        "Return JSON only."
    )


def _validate_page_context(payload: dict[str, Any], page_code: str) -> None:
    if payload.get("page_code") != page_code:
        raise ValueError(f"page_context_page_code_mismatch:{payload.get('page_code')}:{page_code}")
    for field in ("guideline_header", "page_title", "page_scope_summary"):
        if not str(payload.get(field, "")).strip():
            raise ValueError(f"missing_{field}:{page_code}")


def _validate_node_semantics(
    payload: dict[str, Any],
    page_code: str,
    reviewed_nodes: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    if payload.get("page_code") != page_code:
        raise ValueError(f"node_semantics_page_code_mismatch:{payload.get('page_code')}:{page_code}")
    reviewed_ids = {node["id"] for node in reviewed_nodes}
    by_id: dict[str, dict[str, Any]] = {}
    for item in payload.get("nodes", []):
        node_id = item.get("id")
        if node_id not in reviewed_ids:
            raise ValueError(f"unknown_node_in_semantics:{page_code}:{node_id}")
        node_label = item.get("node_label")
        if not str(node_label or "").strip():
            raise ValueError(f"missing_node_label:{page_code}:{node_id}")
        if node_label not in VALID_NODE_LABELS:
            raise ValueError(f"invalid_node_label:{page_code}:{node_id}:{node_label}")
        by_id[node_id] = item
    if by_id.keys() != reviewed_ids:
        missing = sorted(reviewed_ids.difference(by_id.keys()))
        raise ValueError(f"missing_node_semantics:{page_code}:{missing}")
    return by_id


def _normalize_node_label(node: dict[str, Any], proposed_label: str | None) -> str | None:
    node_type = str(node.get("node_type") or "")
    text = str(node.get("verbatim_text") or "")

    if node_type == "cross_page":
        upper = text.upper()
        if any(prefix in upper for prefix in SUPPLEMENT_PAGE_PREFIXES):
            return proposed_label
        if any(prefix in upper for prefix in FLOWCHART_PAGE_PREFIXES):
            return "Page Jump"
        return "Page Jump"

    return proposed_label


def _page_image_path(page_number: int) -> Path:
    roots = ov_2025_roots()
    return roots["page_assets"] / f"page_{page_number:03d}.png"


def _edge_label_from_node_labels(
    source_label: str | None,
    target_label: str | None,
    target_node_type: str | None,
) -> str | None:
    if source_label == "Page Jump" or target_label == "Page Jump":
        return "is_followed_by"
    if source_label == "Disease Condition" and target_label == "Treatment Option":
        return "requires"
    if source_label == "Evaluation" and target_label == "Disease Condition":
        return "indicates"
    if source_label in VALID_NODE_LABELS and target_label in VALID_NODE_LABELS:
        return "is_followed_by"
    return None


def build_page_semantics(
    *,
    page_labels: list[str],
    input_root: Path | None = None,
    pages_json_path: Path | None = None,
    model: str = "gpt-5.1",
    resume: bool = True,
    force_pages: list[str] | None = None,
) -> dict[str, Any]:
    roots = ov_2025_roots()
    input_root = input_root or (roots["processed_root"] / "pages")
    pages_json_path = pages_json_path or (
        roots["raw_root"] / "text_extraction" / "22_nccn_ovarian_cancer_v3_2025" / "raw" / "native" / "pages.json"
    )
    force_pages = force_pages or []
    page_text_by_number = _load_page_text_by_number(pages_json_path)

    summary: dict[str, Any] = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "model": model,
        "pages": [],
    }

    for page_code in page_labels:
        page_dir = input_root / page_code
        reviewed_path = _reviewed_page_path(page_code, input_root)
        typed_path = _typed_page_path(page_code, input_root)
        typed_audit_path = _typed_audit_path(page_code, input_root)
        page_context_path = _page_context_path(page_code, input_root)
        node_semantics_path = _node_semantics_path(page_code, input_root)
        edge_relations_path = _edge_relations_path(page_code, input_root)

        if resume and page_code not in force_pages and typed_path.exists() and typed_audit_path.exists():
            summary["pages"].append({"page_code": page_code, "status": "skipped", "typed_path": str(typed_path)})
            continue

        reviewed = _read_json(reviewed_path)
        page_number = int(reviewed["page_number"])
        page_text = page_text_by_number.get(page_number, "")
        reviewed_nodes = reviewed.get("nodes", [])
        reviewed_edges = reviewed.get("edges", [])

        page_context = _call_llm_json(
            model=model,
            db_path=page_dir / "page_context.trace.db",
            system_prompt=_page_context_system_prompt(),
            user_prompt=_page_context_user_prompt(
                page_code=page_code,
                page_number=page_number,
                page_text=page_text,
                nodes=reviewed_nodes,
            ),
            image_path=_page_image_path(page_number),
        )
        _validate_page_context(page_context, page_code)
        _write_json(page_context_path, page_context)

        node_semantics = _call_llm_json(
            model=model,
            db_path=page_dir / "page_node_semantics.trace.db",
            system_prompt=_node_label_system_prompt(),
            user_prompt=_node_label_user_prompt(
                page_code=page_code,
                page_context=page_context,
                nodes=reviewed_nodes,
            ),
        )
        node_semantics_by_id = _validate_node_semantics(node_semantics, page_code, reviewed_nodes)
        _write_json(node_semantics_path, node_semantics)

        typed_nodes: list[dict[str, Any]] = []
        audit_nodes: list[dict[str, Any]] = []
        for node in reviewed_nodes:
            semantics = node_semantics_by_id[node["id"]]
            normalized_label = _normalize_node_label(node, semantics.get("node_label"))
            typed_nodes.append(
                {
                    "id": node["id"],
                    "node_type": node.get("node_type", "process"),
                    "node_label": normalized_label,
                    "is_uncertain": bool(node.get("is_uncertain", False)),
                    "verbatim_text": node["verbatim_text"],
                    "text_snippet": _to_text_snippet(node["verbatim_text"]),
                    "bbox": node["bbox"],
                }
            )
            audit_nodes.append(
                {
                    "id": node["id"],
                    "shape_id": node.get("shape_id"),
                    "why_node": node.get("why_node", ""),
                    "node_label_raw": semantics.get("node_label"),
                    "node_label_rationale": semantics.get("rationale", ""),
                }
            )

        typed_node_lookup = {node["id"]: node for node in typed_nodes}
        edge_relations_payload = {"page_code": page_code, "edges": []}
        typed_edges: list[dict[str, Any]] = []
        audit_edges: list[dict[str, Any]] = []

        for edge in reviewed_edges:
            source_node = typed_node_lookup.get(edge["source_node_id"])
            target_node = typed_node_lookup.get(edge["target_node_id"])
            relation = _edge_label_from_node_labels(
                source_node.get("node_label") if source_node else None,
                target_node.get("node_label") if target_node else None,
                target_node.get("node_type") if target_node else None,
            )
            edge_relations_payload["edges"].append(
                {
                    "id": edge["id"],
                    "edge_label": relation,
                    "rationale": (
                        f"{source_node.get('node_label') if source_node else None} -> "
                        f"{target_node.get('node_label') if target_node else None} "
                        f"=> {relation}"
                    ),
                }
            )
            typed_edges.append(
                {
                    "id": edge["id"],
                    "source_node_id": edge["source_node_id"],
                    "target_node_id": edge["target_node_id"],
                    "edge_type": edge.get("edge_type", "flow"),
                    "edge_label": relation,
                    "is_uncertain": bool(edge.get("is_uncertain", False)),
                }
            )
            audit_edges.append(
                {
                    "id": edge["id"],
                    "shape_id": edge.get("shape_id"),
                    "why_edge": edge.get("why_edge", ""),
                    "start": edge.get("start"),
                    "end": edge.get("end"),
                    "bend": edge.get("bend"),
                }
            )

        _write_json(edge_relations_path, edge_relations_payload)

        typed_payload = {
            "page_code": page_code,
            "page_number": page_number,
            "graph_type": reviewed.get("graph_type", "directed"),
            "status": "typed_page_graph",
            "page_context": page_context,
            "nodes": typed_nodes,
            "edges": typed_edges,
        }
        audit_payload = {
            "page_code": page_code,
            "page_number": page_number,
            "source_reviewed_graph": str(reviewed_path),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "moved_node_fields": audit_nodes,
            "moved_edge_fields": audit_edges,
        }
        _write_json(typed_path, typed_payload)
        _write_json(typed_audit_path, audit_payload)

        summary["pages"].append(
            {
                "page_code": page_code,
                "status": "ok",
                "page_context_path": str(page_context_path),
                "node_semantics_path": str(node_semantics_path),
                "edge_relations_path": str(edge_relations_path),
                "typed_path": str(typed_path),
                "typed_audit_path": str(typed_audit_path),
                "node_count": len(typed_nodes),
                "edge_count": len(typed_edges),
            }
        )

    report_path = roots["reports"] / "page_semantics_run_summary.json"
    _write_json(report_path, summary)
    summary["summary_path"] = str(report_path)
    summary["processed_count"] = sum(1 for item in summary["pages"] if item["status"] == "ok")
    summary["skipped_count"] = sum(1 for item in summary["pages"] if item["status"] == "skipped")
    return summary
