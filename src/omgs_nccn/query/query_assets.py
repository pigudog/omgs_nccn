from __future__ import annotations

import csv
import json
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any

from omgs_nccn.config.paths import ov_2025_roots


ALLOWED_QUERY_NODE_LABELS = {
    "Disease Condition",
    "Evaluation",
    "Treatment Option",
    "Page Jump",
}
ALLOWED_RELATION_TYPES = {"REQUIRES", "INDICATES", "IS_FOLLOWED_BY"}


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")


def _write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _join_list(values: list[str] | None) -> str:
    if not values:
        return ""
    return "|".join(str(value) for value in values if str(value).strip())


def _relation_type_for_edge(edge: dict[str, Any]) -> str:
    label = edge.get("edge_label")
    if label == "requires":
        return "REQUIRES"
    if label == "indicates":
        return "INDICATES"
    return "IS_FOLLOWED_BY"


def _page_context_map(source_pages: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        item["page_code"]: item.get("page_context", {})
        for item in source_pages
        if item.get("page_code")
    }


def _build_sample_queries() -> list[dict[str, Any]]:
    return [
        {
            "query_id": "Q01",
            "question": "What treatment options are directly required by disease-condition nodes on OV-1?",
            "cypher": (
                "MATCH (d:GuidelineNode {page_code: 'OV-1', node_label: 'Disease Condition'})"
                "-[:REQUIRES]->(t:GuidelineNode {node_label: 'Treatment Option'}) "
                "RETURN d.id, d.verbatim_text, t.id, t.verbatim_text "
                "ORDER BY d.id, t.id"
            ),
            "intent": "page_scoped_direct_treatment_lookup",
        },
        {
            "query_id": "Q02",
            "question": "Which page-jump nodes are reachable from OV-1?",
            "cypher": (
                "MATCH (n:GuidelineNode {page_code: 'OV-1'})-[:IS_FOLLOWED_BY]->"
                "(j:GuidelineNode {node_label: 'Page Jump'}) "
                "RETURN n.id, n.verbatim_text, j.id, j.verbatim_text "
                "ORDER BY n.id, j.id"
            ),
            "intent": "page_scoped_jump_lookup",
        },
        {
            "query_id": "Q03",
            "question": "What evaluation nodes indicate disease conditions on OV-1?",
            "cypher": (
                "MATCH (e:GuidelineNode {page_code: 'OV-1', node_label: 'Evaluation'})"
                "-[:INDICATES]->(d:GuidelineNode {node_label: 'Disease Condition'}) "
                "RETURN e.id, e.verbatim_text, d.id, d.verbatim_text "
                "ORDER BY e.id, d.id"
            ),
            "intent": "page_scoped_indication_lookup",
        },
        {
            "query_id": "Q04",
            "question": "What treatment path follows the Stage II-IV condition on LCOC-3?",
            "cypher": (
                "MATCH p = (:GuidelineNode {page_code: 'LCOC-3', verbatim_text: 'Stage II-IV'})"
                "-[:REQUIRES|IS_FOLLOWED_BY*1..3]->(n:GuidelineNode) "
                "RETURN p"
            ),
            "intent": "short_path_from_condition",
        },
        {
            "query_id": "Q05",
            "question": "Which nodes on OV-7 are treatment options?",
            "cypher": (
                "MATCH (n:GuidelineNode {page_code: 'OV-7', node_label: 'Treatment Option'}) "
                "RETURN n.id, n.verbatim_text ORDER BY n.id"
            ),
            "intent": "page_scoped_treatment_inventory",
        },
    ]


def _build_verbalisation_templates() -> list[dict[str, str]]:
    return [
        {
            "source_node_label": "Disease Condition",
            "relation_type": "REQUIRES",
            "target_node_label": "Treatment Option",
            "template_first": "If the disease condition is {source}, use the treatment {target}.",
            "template_followup": "If that disease condition has occurred, use the treatment {target}.",
        },
        {
            "source_node_label": "Evaluation",
            "relation_type": "INDICATES",
            "target_node_label": "Disease Condition",
            "template_first": "Evaluate the patient for {source}, and check whether it indicates {target}.",
            "template_followup": "Based on the evaluation, check whether it indicates {target}.",
        },
        {
            "source_node_label": "Disease Condition",
            "relation_type": "IS_FOLLOWED_BY",
            "target_node_label": "Evaluation",
            "template_first": "If the disease condition is {source}, then evaluate the patient for {target}.",
            "template_followup": "After that disease condition, evaluate the patient for {target}.",
        },
        {
            "source_node_label": "Treatment Option",
            "relation_type": "IS_FOLLOWED_BY",
            "target_node_label": "Evaluation",
            "template_first": "After the treatment {source}, evaluate the patient for {target}.",
            "template_followup": "After treatment, evaluate the patient for {target}.",
        },
        {
            "source_node_label": "Treatment Option",
            "relation_type": "IS_FOLLOWED_BY",
            "target_node_label": "Page Jump",
            "template_first": "After the treatment {source}, continue on the linked page {target}.",
            "template_followup": "Then continue on the linked page {target}.",
        },
    ]


def build_query_assets(
    *,
    rule_graph_path: Path | None = None,
    query_root: Path | None = None,
    report_root: Path | None = None,
) -> dict[str, Any]:
    roots = ov_2025_roots()
    rule_graph_path = rule_graph_path or (roots["rule_graph"] / "ov_2025_global.rule_graph.json")
    query_root = query_root or (roots["processed_root"] / "query")
    report_root = report_root or roots["reports"]

    payload = _read_json(rule_graph_path)
    page_context_by_code = _page_context_map(payload.get("source_pages", []))

    kept_nodes: list[dict[str, Any]] = []
    node_ids_kept: set[str] = set()
    dropped_external_nodes = 0
    for node in payload["nodes"]:
        if node.get("page_code") == "EXTERNAL":
            dropped_external_nodes += 1
            continue
        node_label = node.get("node_label")
        if node_label not in ALLOWED_QUERY_NODE_LABELS:
            continue
        page_context = page_context_by_code.get(node["page_code"], {})
        kept = {
            "id": node["id"],
            "page_code": node["page_code"],
            "page_number": node.get("page_number"),
            "node_type": node.get("node_type"),
            "node_label": node_label,
            "verbatim_text": node.get("verbatim_text", ""),
            "text_snippet": node.get("text_snippet", ""),
            "guideline_header": page_context.get("guideline_header"),
            "page_title": page_context.get("page_title"),
            "page_scope_summary": page_context.get("page_scope_summary"),
            "explicit_ref_ids": node.get("explicit_ref_ids", []),
            "explicit_ref_count": int(node.get("explicit_ref_count", 0) or 0),
            "has_explicit_refs": bool(node.get("has_explicit_refs", False)),
            "reviewed_footnote_ids": node.get("reviewed_footnote_ids", []),
            "reviewed_footnote_labels": node.get("reviewed_footnote_labels", []),
            "reviewed_footnote_texts": node.get("reviewed_footnote_texts", []),
            "reviewed_footnote_count": int(node.get("reviewed_footnote_count", 0) or 0),
            "has_reviewed_footnotes": bool(node.get("has_reviewed_footnotes", False)),
            "reviewed_footnote_ref_ids": node.get("reviewed_footnote_ref_ids", []),
            "reviewed_footnote_ref_count": int(node.get("reviewed_footnote_ref_count", 0) or 0),
            "has_reviewed_footnote_refs": bool(node.get("has_reviewed_footnote_refs", False)),
            "is_uncertain": bool(node.get("is_uncertain", False)),
        }
        kept_nodes.append(kept)
        node_ids_kept.add(node["id"])

    kept_edges: list[dict[str, Any]] = []
    dropped_external_edges = 0
    for edge in payload["edges"]:
        if edge.get("stitch_kind") == "external_ref":
            dropped_external_edges += 1
            continue
        source_id = edge.get("source_node_id")
        target_id = edge.get("target_node_id")
        if source_id not in node_ids_kept or target_id not in node_ids_kept:
            continue
        relation_type = _relation_type_for_edge(edge)
        kept_edges.append(
            {
                "id": edge["id"],
                "source_node_id": source_id,
                "target_node_id": target_id,
                "relation_type": relation_type,
                "source_page_code": edge.get("source_page_code"),
                "target_page_code": edge.get("target_page_code"),
                "stitch_kind": edge.get("stitch_kind"),
                "edge_type": edge.get("edge_type"),
                "edge_label": edge.get("edge_label"),
                "is_uncertain": bool(edge.get("is_uncertain", False)),
            }
        )

    query_graph_payload = {
        "graph_id": payload["graph_id"],
        "guideline": payload["guideline"],
        "version": payload["version"],
        "status": "query_graph",
        "node_label_vocab": sorted(ALLOWED_QUERY_NODE_LABELS),
        "relation_type_vocab": sorted(ALLOWED_RELATION_TYPES),
        "nodes": kept_nodes,
        "edges": kept_edges,
        "build_notes": {
            "source_rule_graph": str(rule_graph_path),
            "dropped_external_nodes": dropped_external_nodes,
            "dropped_external_edges": dropped_external_edges,
            "edge_relation_policy": {
                "requires": "REQUIRES",
                "indicates": "INDICATES",
                "everything_else": "IS_FOLLOWED_BY",
            },
        },
    }

    neo4j_like_payload = {
        "graph_id": payload["graph_id"],
        "guideline": payload["guideline"],
        "version": payload["version"],
        "status": "neo4j_like_graph",
        "schema": {
            "node_label": "GuidelineNode",
            "relation_types": sorted(ALLOWED_RELATION_TYPES),
            "node_properties": [
                "id",
                "page_code",
                "page_number",
                "node_type",
                "node_label",
                "verbatim_text",
                "text_snippet",
                "guideline_header",
                "page_title",
                "page_scope_summary",
                "explicit_ref_ids",
                "explicit_ref_count",
                "has_explicit_refs",
                "reviewed_footnote_ids",
                "reviewed_footnote_labels",
                "reviewed_footnote_texts",
                "reviewed_footnote_count",
                "has_reviewed_footnotes",
                "reviewed_footnote_ref_ids",
                "reviewed_footnote_ref_count",
                "has_reviewed_footnote_refs",
                "is_uncertain",
            ],
            "edge_properties": [
                "id",
                "relation_type",
                "source_page_code",
                "target_page_code",
                "stitch_kind",
                "edge_type",
                "edge_label",
                "is_uncertain",
            ],
        },
        "nodes": [
            {
                "labels": ["GuidelineNode"],
                "properties": {
                    "id": node["id"],
                    "page_code": node["page_code"],
                    "page_number": node["page_number"],
                    "node_type": node["node_type"],
                    "node_label": node["node_label"],
                    "verbatim_text": node["verbatim_text"],
                    "text_snippet": node["text_snippet"],
                    "guideline_header": node["guideline_header"],
                    "page_title": node["page_title"],
                    "page_scope_summary": node["page_scope_summary"],
                    "explicit_ref_ids": node["explicit_ref_ids"],
                    "explicit_ref_count": node["explicit_ref_count"],
                    "has_explicit_refs": node["has_explicit_refs"],
                    "reviewed_footnote_ids": node["reviewed_footnote_ids"],
                    "reviewed_footnote_labels": node["reviewed_footnote_labels"],
                    "reviewed_footnote_texts": node["reviewed_footnote_texts"],
                    "reviewed_footnote_count": node["reviewed_footnote_count"],
                    "has_reviewed_footnotes": node["has_reviewed_footnotes"],
                    "reviewed_footnote_ref_ids": node["reviewed_footnote_ref_ids"],
                    "reviewed_footnote_ref_count": node["reviewed_footnote_ref_count"],
                    "has_reviewed_footnote_refs": node["has_reviewed_footnote_refs"],
                    "is_uncertain": node["is_uncertain"],
                },
            }
            for node in kept_nodes
        ],
        "relationships": [
            {
                "type": edge["relation_type"],
                "start_node_id": edge["source_node_id"],
                "end_node_id": edge["target_node_id"],
                "properties": {
                    "id": edge["id"],
                    "source_page_code": edge["source_page_code"],
                    "target_page_code": edge["target_page_code"],
                    "stitch_kind": edge["stitch_kind"],
                    "edge_type": edge["edge_type"],
                    "edge_label": edge["edge_label"],
                    "is_uncertain": edge["is_uncertain"],
                },
            }
            for edge in kept_edges
        ],
    }

    neo4j_nodes_rows = [
        {
            "node_id:ID(GuidelineNode)": node["id"],
            ":LABEL": "GuidelineNode",
            "page_code": node["page_code"],
            "page_number:int": node["page_number"],
            "node_type": node["node_type"],
            "node_label": node["node_label"],
            "verbatim_text": node["verbatim_text"],
            "text_snippet": node["text_snippet"],
            "guideline_header": node["guideline_header"],
            "page_title": node["page_title"],
            "page_scope_summary": node["page_scope_summary"],
            "explicit_ref_ids": _join_list(node["explicit_ref_ids"]),
            "explicit_ref_count:int": node["explicit_ref_count"],
            "has_explicit_refs:boolean": str(node["has_explicit_refs"]).lower(),
            "reviewed_footnote_ids": _join_list(node["reviewed_footnote_ids"]),
            "reviewed_footnote_labels": _join_list(node["reviewed_footnote_labels"]),
            "reviewed_footnote_texts": _join_list(node["reviewed_footnote_texts"]),
            "reviewed_footnote_count:int": node["reviewed_footnote_count"],
            "has_reviewed_footnotes:boolean": str(node["has_reviewed_footnotes"]).lower(),
            "reviewed_footnote_ref_ids": _join_list(node["reviewed_footnote_ref_ids"]),
            "reviewed_footnote_ref_count:int": node["reviewed_footnote_ref_count"],
            "has_reviewed_footnote_refs:boolean": str(node["has_reviewed_footnote_refs"]).lower(),
            "is_uncertain:boolean": str(node["is_uncertain"]).lower(),
        }
        for node in kept_nodes
    ]
    neo4j_edges_rows = [
        {
            ":START_ID(GuidelineNode)": edge["source_node_id"],
            ":END_ID(GuidelineNode)": edge["target_node_id"],
            ":TYPE": edge["relation_type"],
            "edge_id": edge["id"],
            "source_page_code": edge["source_page_code"],
            "target_page_code": edge["target_page_code"],
            "stitch_kind": edge["stitch_kind"],
            "edge_type": edge["edge_type"],
            "edge_label_text": edge["edge_label"],
            "is_uncertain:boolean": str(edge["is_uncertain"]).lower(),
        }
        for edge in kept_edges
    ]

    query_schema_payload = {
        "node_label_vocab": sorted(ALLOWED_QUERY_NODE_LABELS),
        "relation_type_vocab": sorted(ALLOWED_RELATION_TYPES),
        "node_properties": [
            "id",
            "page_code",
            "page_number",
            "node_type",
            "node_label",
            "verbatim_text",
            "text_snippet",
            "guideline_header",
            "page_title",
            "page_scope_summary",
            "explicit_ref_ids",
            "explicit_ref_count",
            "has_explicit_refs",
            "reviewed_footnote_ids",
            "reviewed_footnote_labels",
            "reviewed_footnote_texts",
            "reviewed_footnote_count",
            "has_reviewed_footnotes",
            "reviewed_footnote_ref_ids",
            "reviewed_footnote_ref_count",
            "has_reviewed_footnote_refs",
            "is_uncertain",
        ],
        "edge_properties": [
            "id",
            "relation_type",
            "source_page_code",
            "target_page_code",
            "stitch_kind",
            "edge_type",
            "edge_label",
            "is_uncertain",
        ],
        "notes": [
            "This query graph excludes external_ref edges and EXTERNAL alias nodes.",
            "Supplement/principles references remain enrichment-layer assets, not primary query-path nodes.",
        ],
    }

    sample_queries_payload = {
        "graph_id": payload["graph_id"],
        "status": "sample_cypher_queries",
        "queries": _build_sample_queries(),
    }
    verbalisation_payload = {
        "status": "query_verbalisation_templates",
        "templates": _build_verbalisation_templates(),
    }

    query_graph_path = query_root / "ov_2025_global.query_graph.json"
    neo4j_like_path = query_root / "ov_2025_global.neo4j_like.json"
    nodes_csv_path = query_root / "ov_2025_global.neo4j_nodes.csv"
    edges_csv_path = query_root / "ov_2025_global.neo4j_edges.csv"
    schema_path = query_root / "ov_2025_query_schema.json"
    sample_queries_path = query_root / "ov_2025_sample_cypher_queries.json"
    verbalisation_path = query_root / "ov_2025_query_verbalisation_templates.json"

    _write_json(query_graph_path, query_graph_payload)
    _write_json(neo4j_like_path, neo4j_like_payload)
    _write_csv(nodes_csv_path, list(neo4j_nodes_rows[0].keys()) if neo4j_nodes_rows else [
        "node_id:ID(GuidelineNode)", ":LABEL", "page_code", "page_number:int", "node_type",
        "node_label", "verbatim_text", "text_snippet", "guideline_header", "page_title",
        "page_scope_summary", "explicit_ref_ids", "explicit_ref_count:int",
        "has_explicit_refs:boolean", "reviewed_footnote_ids", "reviewed_footnote_labels",
        "reviewed_footnote_texts", "reviewed_footnote_count:int",
        "has_reviewed_footnotes:boolean", "reviewed_footnote_ref_ids",
        "reviewed_footnote_ref_count:int", "has_reviewed_footnote_refs:boolean",
        "is_uncertain:boolean",
    ], neo4j_nodes_rows)
    _write_csv(edges_csv_path, list(neo4j_edges_rows[0].keys()) if neo4j_edges_rows else [
        ":START_ID(GuidelineNode)", ":END_ID(GuidelineNode)", ":TYPE", "edge_id", "source_page_code",
        "target_page_code", "stitch_kind", "edge_type", "edge_label_text", "is_uncertain:boolean",
    ], neo4j_edges_rows)
    _write_json(schema_path, query_schema_payload)
    _write_json(sample_queries_path, sample_queries_payload)
    _write_json(verbalisation_path, verbalisation_payload)

    report_payload = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "status": "ok",
        "source_rule_graph": str(rule_graph_path),
        "query_graph_path": str(query_graph_path),
        "neo4j_like_path": str(neo4j_like_path),
        "nodes_csv_path": str(nodes_csv_path),
        "edges_csv_path": str(edges_csv_path),
        "schema_path": str(schema_path),
        "sample_queries_path": str(sample_queries_path),
        "verbalisation_path": str(verbalisation_path),
        "node_count": len(kept_nodes),
        "edge_count": len(kept_edges),
        "dropped_external_nodes": dropped_external_nodes,
        "dropped_external_edges": dropped_external_edges,
        "nodes_with_explicit_refs": sum(1 for node in kept_nodes if node["has_explicit_refs"]),
        "nodes_with_reviewed_footnotes": sum(1 for node in kept_nodes if node["has_reviewed_footnotes"]),
        "nodes_with_reviewed_footnote_refs": sum(1 for node in kept_nodes if node["has_reviewed_footnote_refs"]),
        "page_jump_node_count": sum(1 for node in kept_nodes if node["node_label"] == "Page Jump"),
    }
    report_path = report_root / "ov_2025_query_assets_report.json"
    _write_json(report_path, report_payload)

    return {
        "status": "ok",
        "query_graph_path": str(query_graph_path),
        "neo4j_like_path": str(neo4j_like_path),
        "nodes_csv_path": str(nodes_csv_path),
        "edges_csv_path": str(edges_csv_path),
        "schema_path": str(schema_path),
        "sample_queries_path": str(sample_queries_path),
        "verbalisation_path": str(verbalisation_path),
        "report_path": str(report_path),
        "node_count": len(kept_nodes),
        "edge_count": len(kept_edges),
    }
