from __future__ import annotations

import json
import re
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any

from neo4j import GraphDatabase

from omgs_nccn.config.paths import ov_2025_roots
from omgs_nccn.llm.client import init_client
from omgs_nccn.utils.cypher_validation import apply_negative_anchor_guard
from omgs_nccn.utils.cypher_validation import repair_cypher
from omgs_nccn.utils.cypher_validation import validate_cypher
from omgs_nccn.utils.page_context import page_directory
from omgs_nccn.utils.query_normalization import candidate_nodes
from omgs_nccn.utils.query_normalization import question_intents
from omgs_nccn.utils.query_normalization import question_tokens
from omgs_nccn.utils.reference_resolution import asset_map_by_id
from omgs_nccn.utils.reference_resolution import enrich_supporting_asset
from omgs_nccn.utils.reference_resolution import indexed_reference_page_lookup
from omgs_nccn.utils.reference_resolution import inline_ref_asset_ids_from_text
from omgs_nccn.utils.reference_resolution import pages_by_number
from omgs_nccn.utils.reference_resolution import read_json_any
from omgs_nccn.utils.reference_resolution import read_pages_json
from omgs_nccn.utils.reference_resolution import reviewed_footnotes_by_node
from omgs_nccn.utils.reference_resolution import truncate_text


STOPWORDS = {
    "with",
    "prior",
    "previously",
    "patient",
    "patients",
    "ovarian",
    "cancer",
    "year",
    "years",
    "old",
    "used",
    "use",
    "and",
    "the",
    "for",
    "after",
    "before",
    "setting",
}

PAGE_JUMP_HINT_TERMS = (
    "which page",
    "page number",
    "where should",
    "where can i find",
    "where can i look",
    "refer",
    "subsequent pathway reference",
    "covers the management",
    "follow-up page",
    "翻到哪一页",
    "哪一页",
    "继续查看",
    "后续路径",
    "跳转",
    "翻页",
)

NEGATION_CUES = (
    " no ",
    " negative",
    " absence",
    " not ",
    " without ",
)

TERM_EXPANSIONS = {
    "卵巢癌": ["ovarian cancer"],
    "铂耐药": ["platinum-resistant", "resistant", "recurrence"],
    "铂敏感": ["platinum-sensitive", "sensitive", "recurrence"],
    "三线": ["third-line", "line", "recurrence"],
    "既往使用过parp抑制剂": ["parpi", "parp"],
    "parp抑制剂": ["parpi", "parp"],
    "贝伐单抗": ["bevacizumab"],
    "叶酸受体": ["folate receptor", "folate"],
    "叶酸受体表达阳性": ["folate receptor", "folate"],
    "her2++": ["her2"],
    "her2": ["her2"],
}

FAMILY_INDEXED_REF_RE = re.compile(r"\b([A-Z]+-[A-Z])\s*,\s*(\d+)\s+of\s+(\d+)\b")
PAGE_REF_RE = re.compile(r"\b(OV-\d+|LCOC-\d+)\b")
FAMILY_PLAIN_REF_RE = re.compile(r"\b(OV-[A-Z]|LCOC-[A-Z])\b")
PRIMARY_HEADING_RE = re.compile(
    r"^(?:##|###)\s+\[(?P<label>(?P<family>(?:OV|LCOC)-[A-Z])\s+\\\((?P<index>\d+)\s+of\s+(?P<count>\d+)\\\))\]\(#page-(?P<anchor>\d+)-0\)",
    re.MULTILINE,
)


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def _read_json_any(path: Path) -> Any:
    return json.loads(path.read_text())


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")


def _asset_map_by_id(payload: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    if not payload:
        return {}
    return {
        asset["asset_id"]: asset
        for asset in payload.get("assets", [])
        if asset.get("asset_id")
    }


def _reviewed_footnotes_by_node(payload: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    if not payload:
        return {}
    return {
        item["source_node_id"]: item
        for item in payload.get("nodes", [])
        if item.get("source_node_id")
    }


def _read_pages_json(path: Path) -> list[dict[str, Any]]:
    payload = _read_json_any(path)
    if isinstance(payload, dict):
        return list(payload.get("pages", []))
    if isinstance(payload, list):
        return payload
    return []


def _pages_by_number(path: Path) -> dict[int, dict[str, Any]]:
    pages = _read_pages_json(path)
    return {
        int(page["page_number"]): page
        for page in pages
        if isinstance(page, dict) and page.get("page_number") is not None
    }


def _truncate_text(text: str, *, limit: int = 500) -> str:
    normalized = re.sub(r"\s+", " ", text).strip()
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 3].rstrip() + "..."


def _indexed_reference_page_lookup(
    *,
    primary_md_path: Path,
    pages_by_number: dict[int, dict[str, Any]],
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
        page_payload = pages_by_number.get(page_number) or pages_by_number.get(anchor_page)
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
            "content_excerpt": _truncate_text(excerpt_source, limit=700),
        }
        indexed_by_asset_id[asset_id] = entry
        family_entries.setdefault(family, []).append(entry)
    for family in family_entries:
        family_entries[family] = sorted(
            family_entries[family],
            key=lambda item: (item["target_page_index"], item["page_number"]),
        )
    return indexed_by_asset_id, family_entries


def _enrich_supporting_asset(
    asset: dict[str, Any],
    *,
    indexed_pages_by_asset_id: dict[str, dict[str, Any]],
    family_entries: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    enriched = dict(asset)
    asset_id = asset.get("asset_id")
    family = asset.get("target_page_family")
    if asset_id and asset_id in indexed_pages_by_asset_id:
        ref_page = indexed_pages_by_asset_id[asset_id]
        enriched["resolved_ref_page"] = ref_page
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


def _inline_ref_asset_ids_from_text(
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


def _extract_json_object(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start < 0 or end < 0 or end <= start:
        raise ValueError("llm_output_missing_json_object")
    return json.loads(stripped[start : end + 1])


def _sanitize_cypher(cypher: str) -> str:
    fixed = cypher.strip()
    fixed = re.sub(
        r"\bMATCH\s+([A-Za-z_]\w*)\s*:\s*GuidelineNode\b",
        r"MATCH (\1:GuidelineNode)",
        fixed,
    )
    fixed = re.sub(
        r"(MATCH\s+\w+\s*=\s*)([A-Za-z_]\w*)(\s*-\s*\[:)",
        r"\1(\2)\3",
        fixed,
    )
    fixed = re.sub(
        r"(MATCH\s+\w+\s*=\s*)([A-Za-z_]\w*)(\s*-\s*\*)",
        r"\1(\2)\3",
        fixed,
    )
    fixed = re.sub(
        r"\[:([A-Z_]+)\|:([A-Z_]+)",
        r"[:\1|\2",
        fixed,
    )
    fixed = re.sub(
        r"\|:([A-Z_]+)",
        r"|\1",
        fixed,
    )
    return fixed


def _cypher_declared_vars_before_first_where(cypher: str) -> set[str]:
    prefix = re.split(r"\bWHERE\b", cypher, maxsplit=1)[0]
    declared = set(re.findall(r"\((\w+):GuidelineNode\b", prefix))
    declared.update(re.findall(r"\b(\w+)\s*=", prefix))
    return declared


def _apply_negative_anchor_guard(cypher: str, *, question: str) -> str:
    intents = _question_intents(question)
    if intents["allow_negative_anchor"]:
        return cypher
    declared_before_where = _cypher_declared_vars_before_first_where(cypher)
    disease_vars = [
        var
        for var in re.findall(
            r"\((\w+):GuidelineNode\s*\{[^}]*node_label:\s*'Disease Condition'[^}]*\}\)",
            cypher,
        )
        if var in declared_before_where
    ]
    if not disease_vars:
        return cypher
    guards = [
        f"NOT toLower({var}.verbatim_text) STARTS WITH 'no '"
        for var in sorted(set(disease_vars))
        if f"NOT toLower({var}.verbatim_text) STARTS WITH 'no '" not in cypher
    ]
    if not guards:
        return cypher
    guard_text = " AND ".join(guards)
    if re.search(r"\bWHERE\b", cypher):
        return re.sub(r"\bWHERE\b", f"WHERE {guard_text} AND ", cypher, count=1)
    match_with = re.search(r"\b(WITH|RETURN)\b", cypher)
    if match_with:
        insert_at = match_with.start()
        return cypher[:insert_at] + f" WHERE {guard_text} " + cypher[insert_at:]
    return cypher


def _split_cypher_clauses(cypher: str) -> list[tuple[str, str]]:
    pattern = re.compile(r"\b(MATCH|WITH|WHERE|RETURN|ORDER BY)\b", re.IGNORECASE)
    matches = list(pattern.finditer(cypher))
    if not matches:
        return []
    clauses: list[tuple[str, str]] = []
    for idx, match in enumerate(matches):
        start = match.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(cypher)
        clauses.append((match.group(1).upper(), cypher[start:end].strip()))
    return clauses


def _cypher_projected_vars(clause_body: str) -> set[str]:
    vars_found: set[str] = set()
    for part in re.split(r"\s*,\s*", clause_body):
        stripped = part.strip()
        if not stripped:
            continue
        alias_match = re.search(r"\bAS\s+(\w+)\b", stripped, flags=re.IGNORECASE)
        if alias_match:
            vars_found.add(alias_match.group(1))
            continue
        if re.match(r"^\w+$", stripped):
            vars_found.add(stripped)
    return vars_found


def _validate_cypher(
    cypher: str,
    *,
    schema_payload: dict[str, Any],
) -> list[str]:
    errors: list[str] = []
    if re.search(r"\bMATCH\s+[A-Za-z_]\w*\s*:\s*GuidelineNode\b", cypher):
        errors.append("invalid_match_node_pattern")

    allowed_properties = set(schema_payload.get("node_properties", []))
    allowed_relations = set(schema_payload.get("relation_type_vocab", []))

    for prop in re.findall(r"\b\w+\.(\w+)\b", cypher):
        if prop not in allowed_properties:
            errors.append(f"unknown_property:{prop}")

    for rel_blob in re.findall(r"\[:([A-Z_|]+)(?:\*[0-9.]+)?\]", cypher):
        for rel in rel_blob.split("|"):
            rel = rel.strip()
            if rel and rel not in allowed_relations:
                errors.append(f"unknown_relation:{rel}")

    declared = set(re.findall(r"\((\w+):GuidelineNode\b", cypher))
    declared.update(re.findall(r"\b(\w+)\s*=", cypher))
    referenced = set(re.findall(r"\b(\w+)\.\w+\b", cypher))
    undefined = sorted(referenced - declared)
    for var in undefined:
        errors.append(f"undefined_variable:{var}")

    clauses = _split_cypher_clauses(cypher)
    for idx, (name, body) in enumerate(clauses):
        if name not in {"WITH", "RETURN"} or "DISTINCT" not in body.upper():
            continue
        projected = _cypher_projected_vars(re.sub(r"^\s*DISTINCT\s+", "", body, flags=re.IGNORECASE))
        if idx + 1 < len(clauses) and clauses[idx + 1][0] == "ORDER BY":
            order_vars = set(re.findall(r"\b(\w+)\.\w+\b", clauses[idx + 1][1]))
            for var in sorted(order_vars - projected):
                errors.append(f"order_by_scope:{var}")
    return sorted(set(errors))


def _repair_cypher(
    cypher: str,
    *,
    schema_payload: dict[str, Any],
) -> str:
    fixed = _sanitize_cypher(cypher)
    errors = _validate_cypher(fixed, schema_payload=schema_payload)
    if any(item.startswith("order_by_scope:") for item in errors):
        fixed = re.sub(r"\bORDER BY\b.*$", "", fixed, flags=re.IGNORECASE | re.DOTALL).strip()
    return fixed


def _question_tokens(question: str) -> list[str]:
    normalized = question.lower()
    kept: list[str] = []
    for source, expansions in TERM_EXPANSIONS.items():
        if source in normalized:
            kept.extend(expansions)
    tokens = re.findall(r"[A-Za-z0-9αβγ+/.-]+", normalized)
    for token in tokens:
        if len(token) < 4:
            continue
        if token in STOPWORDS:
            continue
        kept.append(token)
    return sorted(set(kept))


def _question_intents(question: str) -> dict[str, bool]:
    lowered = f" {question.lower()} "
    return {
        "page_jump": any(term in lowered for term in PAGE_JUMP_HINT_TERMS),
        "allow_negative_anchor": any(term in lowered for term in NEGATION_CUES),
        "support_check": "does this guideline support" in lowered or "instead of" in lowered,
    }


def _candidate_nodes(question: str, query_graph: dict[str, Any], *, limit: int = 12) -> list[dict[str, Any]]:
    tokens = _question_tokens(question)
    intents = _question_intents(question)
    ranked: list[tuple[int, dict[str, Any]]] = []
    for node in query_graph["nodes"]:
        text = f"{node.get('verbatim_text', '')} {node.get('text_snippet', '')}".lower()
        score = sum(1 for token in tokens if token in text)
        if score <= 0:
            continue
        verbatim = str(node.get("verbatim_text", "")).strip().lower()
        if (
            node.get("node_label") == "Disease Condition"
            and verbatim.startswith("no ")
            and not intents["allow_negative_anchor"]
        ):
            score -= 2
        if score <= 0:
            continue
        ranked.append(
            (
                score,
                {
                    "id": node["id"],
                    "page_code": node["page_code"],
                    "node_label": node["node_label"],
                    "verbatim_text": node["verbatim_text"],
                },
            )
        )
    ranked.sort(key=lambda item: (-item[0], item[1]["page_code"], item[1]["id"]))
    return [item[1] for item in ranked[:limit]]


def _build_text2cypher_messages(
    *,
    question: str,
    schema_payload: dict[str, Any],
    sample_queries_payload: dict[str, Any],
    page_directory: list[dict[str, Any]],
) -> list[dict[str, str]]:
    intents = _question_intents(question)
    system_prompt = (
        "You generate one read-only Cypher query for a typed NCCN ovarian cancer graph. "
        "This graph is constructed from NCCN ovarian cancer flowcharts and represents typed clinical pathway transitions, not a general biomedical knowledge graph. "
        "Internally, you may normalize the clinical question into English if needed, but your main job is to produce the most useful treatment-pathway retrieval Cypher. "
        "Use only the provided schema, relation types, and node properties. "
        "Do not force unsupported facts into Cypher. "
        "Prefer returning a path variable p when the question asks for a pathway. "
        "Return JSON only."
    )
    extra_rules: list[str] = []
    if not intents["allow_negative_anchor"]:
        extra_rules.append(
            "Avoid anchoring on disease-condition nodes that begin with `No ` or otherwise express negation unless the question explicitly asks for a negative state."
        )
    if intents["support_check"]:
        extra_rules.append(
            "If the asked treatment term is not clearly represented in the graph, prefer retrieving the closest supported treatment pathway rather than inventing a direct support claim."
        )
    if intents["page_jump"]:
        extra_rules.append(
            "For continuation or page-reference questions, prefer a path-shaped query that reaches the relevant continuation point or the first non-navigation node after it. Do not default to a page inventory query unless no path-shaped query is plausible."
        )
    user_prompt = (
        "Return exactly one JSON object with this schema:\n"
        "{\n"
        '  "cypher": str,\n'
        '  "query_strategy": str\n'
        "}\n\n"
        "Rules:\n"
        "1. Keep the original clinical question as the external user query.\n"
        "2. Your primary output is `cypher`. It should be the most useful retrieval query for finding guideline treatment pathways relevant to the case.\n"
        "3. Use only label `GuidelineNode`.\n"
        "4. Use only relation types `REQUIRES`, `INDICATES`, `IS_FOLLOWED_BY`.\n"
        "5. Prefer these node properties for retrieval: `node_label`, `verbatim_text`, `page_code`. Use `page_title` and `page_scope_summary` only as secondary page-selection hints. Avoid relying on `id`, `page_number`, or `node_type` unless clearly necessary.\n"
        "6. Do not write any mutating Cypher.\n"
        "7. If the graph does not explicitly contain a biomarker, line-of-therapy, or prior-therapy concept, omit it from the Cypher constraints rather than forcing it.\n"
        "8. Prefer exact `page_code` / `node_label` filters plus `toLower(... ) CONTAINS ...` textual constraints.\n"
        "9. Use the page directory only to identify likely relevant pages or page families. Do not assume a page code unless the page title or scope clearly matches the question.\n"
        "10. Prefer path-first retrieval. When possible, use `MATCH p = ... RETURN p` rather than returning only page codes, titles, or node inventories.\n"
        "11. If the question asks for treatment or pathway, anchor on the strongest supported disease-condition node first, then expand to treatment/path nodes.\n"
        "12. If the question asks for a pathway, continuation, next step, subsequent page, or follow-up management, prefer `MATCH p = ... RETURN p`.\n"
        "13. For relationship alternation in Cypher, write `[:REQUIRES|IS_FOLLOWED_BY*1..3]`, not `[:REQUIRES|:IS_FOLLOWED_BY*1..3]`.\n"
        "14. Always write node patterns with parentheses, for example `MATCH (n:GuidelineNode)` and never `MATCH n:GuidelineNode`.\n"
        "15. After `WITH DISTINCT ...`, do not `ORDER BY` variables that were not projected through the `WITH` or `RETURN`.\n"
        "16. Do not reference undefined variables. Every variable used in `WHERE`, `RETURN`, or `ORDER BY` must be introduced in a preceding `MATCH` or `WITH` clause.\n"
        "17. Keep `query_strategy` short and focused on retrieval intent.\n\n"
        "Cypher safety examples:\n"
        "- Good: `MATCH (n:GuidelineNode {page_code: 'OV-7', node_label: 'Treatment Option'}) RETURN n.id, n.verbatim_text ORDER BY n.id`\n"
        "- Good: `MATCH (d:GuidelineNode {node_label: 'Disease Condition'}) WHERE toLower(d.verbatim_text) CONTAINS 'platinum-resistant' WITH d MATCH p = (d)-[:REQUIRES|IS_FOLLOWED_BY*1..3]->(t:GuidelineNode {node_label: 'Treatment Option'}) RETURN p`\n"
        "- Bad: `MATCH startCond:GuidelineNode RETURN startCond`\n"
        "- Bad: `MATCH (n:GuidelineNode) RETURN DISTINCT n.page_code ORDER BY n.page_number`\n"
        "- Bad: `MATCH (a:GuidelineNode) WHERE toLower(b.verbatim_text) CONTAINS 'x' RETURN a`\n\n"
        f"Question-specific guidance:\n{json.dumps(extra_rules, ensure_ascii=False, indent=2)}\n\n"
        f"Schema:\n{json.dumps(schema_payload, ensure_ascii=False, indent=2)}\n\n"
        f"Sample queries:\n{json.dumps(sample_queries_payload['queries'], ensure_ascii=False, indent=2)}\n\n"
        f"Page directory from current graph:\n{json.dumps(page_directory, ensure_ascii=False, indent=2)}\n\n"
        f"Clinical question:\n{question}\n\n"
        "Return JSON only."
    )
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]


def _llm_visible_schema(schema_payload: dict[str, Any]) -> dict[str, Any]:
    payload = dict(schema_payload)
    node_vocab = list(payload.get("node_label_vocab", []))
    payload["node_label_vocab"] = [item for item in node_vocab if item != "Page Jump"]
    return payload


def _llm_visible_sample_queries(sample_queries_payload: dict[str, Any]) -> dict[str, Any]:
    queries = []
    for item in sample_queries_payload.get("queries", []):
        text = json.dumps(item, ensure_ascii=False)
        if "Page Jump" in text:
            continue
        queries.append(item)
    return {
        **sample_queries_payload,
        "queries": queries,
    }


def _page_directory(query_graph: dict[str, Any]) -> list[dict[str, Any]]:
    pages: dict[str, dict[str, Any]] = {}
    for node in query_graph.get("nodes", []):
        page_code = node.get("page_code")
        if not page_code or page_code in pages:
            continue
        pages[page_code] = {
            "page_code": page_code,
            "page_number": node.get("page_number"),
            "page_title": node.get("page_title"),
            "page_scope_summary": node.get("page_scope_summary"),
        }
    return sorted(
        pages.values(),
        key=lambda item: (
            0 if str(item.get("page_code", "")).startswith("OV-") else 1,
            int(str(item.get("page_code", "")).split("-")[1])
            if str(item.get("page_code", "")).split("-")[1].isdigit()
            else 999,
            str(item.get("page_code", "")),
        ),
    )


def _fallback_page_jump_cypher(
    question: str,
    candidate_nodes: list[dict[str, Any]],
) -> str | None:
    intents = _question_intents(question)
    if not intents["page_jump"]:
        return None
    page_jump_candidates = [n for n in candidate_nodes if n.get("node_label") == "Page Jump"]
    if not page_jump_candidates:
        return None
    primary = page_jump_candidates[0]
    node_id = primary.get("id")
    if not node_id:
        return None
    return (
        "MATCH p = (j:GuidelineNode {id: '"
        + str(node_id)
        + "'})-[:IS_FOLLOWED_BY*1..2]->(n:GuidelineNode) "
          "RETURN p"
    )


def _serialize_path(path_obj: Any) -> dict[str, Any]:
    nodes = []
    for node in path_obj.nodes:
        node_props = dict(node.items())
        nodes.append(
            {
                "id": node_props.get("id"),
                "node_label": node_props.get("node_label"),
                "page_code": node_props.get("page_code"),
                "verbatim_text": node_props.get("verbatim_text"),
            }
        )
    relationships = []
    for rel in path_obj.relationships:
        relationships.append(
            {
                "id": rel.get("id"),
                "type": rel.type,
                "source_node_id": rel.start_node.get("id"),
                "target_node_id": rel.end_node.get("id"),
            }
        )
    return {
        "nodes": nodes,
        "relationships": relationships,
    }


def _serialize_value(value: Any) -> Any:
    if hasattr(value, "nodes") and hasattr(value, "relationships"):
        return {"kind": "path", "value": _serialize_path(value)}
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, list):
        return [_serialize_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _serialize_value(item) for key, item in value.items()}
    return str(value)


def _template_map(verbalisation_payload: dict[str, Any]) -> dict[tuple[str, str, str], dict[str, str]]:
    result: dict[tuple[str, str, str], dict[str, str]] = {}
    for item in verbalisation_payload.get("templates", []):
        result[
            (
                item["source_node_label"],
                item["relation_type"],
                item["target_node_label"],
            )
        ] = item
    return result


def _verbalize_paths(
    rows: list[dict[str, Any]],
    verbalisation_payload: dict[str, Any],
) -> list[str]:
    template_map = _template_map(verbalisation_payload)
    summaries: list[str] = []
    for row in rows:
        for value in row.values():
            if not (isinstance(value, dict) and value.get("kind") == "path"):
                continue
            path_payload = value["value"]
            nodes = path_payload["nodes"]
            relationships = path_payload["relationships"]
            if not nodes or not relationships:
                continue
            sentences: list[str] = []
            for idx, rel in enumerate(relationships):
                source = nodes[idx]
                target = nodes[idx + 1]
                key = (
                    source.get("node_label"),
                    rel.get("type"),
                    target.get("node_label"),
                )
                template = template_map.get(key)
                if template is None:
                    sentence = (
                        f"{source.get('verbatim_text')} "
                        f"-[{rel.get('type')}]-> "
                        f"{target.get('verbatim_text')}"
                    )
                else:
                    template_text = (
                        template["template_first"]
                        if idx == 0
                        else template["template_followup"]
                    )
                    sentence = template_text.format(
                        source=source.get("verbatim_text"),
                        target=target.get("verbatim_text"),
                    )
                sentences.append(sentence)
            if sentences:
                summaries.append(" ".join(sentences))
    return summaries


def _is_clinical_step(node: dict[str, Any]) -> bool:
    return node.get("node_label") in {
        "Disease Condition",
        "Evaluation",
        "Treatment Option",
    }


def _build_path_traces(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    traces: list[dict[str, Any]] = []
    for row in rows:
        for key, value in row.items():
            if not (isinstance(value, dict) and value.get("kind") == "path"):
                continue
            path_payload = value["value"]
            nodes = path_payload["nodes"]
            relationships = path_payload["relationships"]
            if not nodes:
                continue
            steps: list[dict[str, Any]] = []
            lines: list[str] = []
            clinical_step_count = 0
            for idx, node in enumerate(nodes):
                is_clinical_step = _is_clinical_step(node)
                clinical_step_index = None
                if is_clinical_step:
                    clinical_step_count += 1
                    clinical_step_index = clinical_step_count
                steps.append(
                    {
                        "kind": "node",
                        "index": idx,
                        "clinical_step_index": clinical_step_index,
                        "is_clinical_step": is_clinical_step,
                        "id": node.get("id"),
                        "page_code": node.get("page_code"),
                        "node_label": node.get("node_label"),
                        "verbatim_text": node.get("verbatim_text"),
                    }
                )
                line_prefix = (
                    f"[c{clinical_step_index}]"
                    if clinical_step_index is not None
                    else "[nav]"
                )
                lines.append(
                    f"[{idx}] {line_prefix} {node.get('id')} | {node.get('page_code')} | {node.get('node_label')} | {node.get('verbatim_text')}"
                )
                if idx < len(relationships):
                    rel = relationships[idx]
                    steps.append(
                        {
                            "kind": "relationship",
                            "index": idx,
                            "id": rel.get("id"),
                            "type": rel.get("type"),
                            "source_node_id": rel.get("source_node_id"),
                            "target_node_id": rel.get("target_node_id"),
                        }
                    )
                    lines.append(f"    --{rel.get('type')}-->")
            traces.append(
                {
                    "record_key": key,
                    "graph_hop_count": len(relationships),
                    "clinical_step_count": clinical_step_count,
                    "steps": steps,
                    "trace_lines": lines,
                }
            )
    return traces


def _extend_serialized_path_through_page_jump(
    path_payload: dict[str, Any],
    *,
    driver: Any,
) -> dict[str, Any]:
    nodes = list(path_payload.get("nodes", []))
    relationships = list(path_payload.get("relationships", []))
    if not nodes:
        return path_payload
    tail = nodes[-1]
    if tail.get("node_label") != "Page Jump" or not tail.get("id"):
        return path_payload
    records, _, _ = driver.execute_query(
        (
            "MATCH p = (j:GuidelineNode {id: $node_id})-[:IS_FOLLOWED_BY*1..5]->(n:GuidelineNode) "
            "WHERE n.node_label <> 'Page Jump' "
            "RETURN p ORDER BY length(p) ASC LIMIT 1"
        ),
        node_id=tail["id"],
        database_="neo4j",
    )
    if not records:
        return path_payload
    extension = None
    for record in records:
        for value in record.values():
            serialized = _serialize_value(value)
            if isinstance(serialized, dict) and serialized.get("kind") == "path":
                extension = serialized["value"]
                break
        if extension:
            break
    if not extension:
        return path_payload
    ext_nodes = list(extension.get("nodes", []))
    ext_relationships = list(extension.get("relationships", []))
    if not ext_nodes or ext_nodes[0].get("id") != tail.get("id"):
        return path_payload
    return {
        "nodes": nodes + ext_nodes[1:],
        "relationships": relationships + ext_relationships,
    }


def _apply_page_jump_continuation(
    rows: list[dict[str, Any]],
    *,
    driver: Any,
) -> list[dict[str, Any]]:
    updated_rows: list[dict[str, Any]] = []
    for row in rows:
        updated_row: dict[str, Any] = {}
        for key, value in row.items():
            if not (isinstance(value, dict) and value.get("kind") == "path"):
                updated_row[key] = value
                continue
            updated_row[key] = {
                "kind": "path",
                "value": _extend_serialized_path_through_page_jump(
                    value["value"],
                    driver=driver,
                ),
            }
        updated_rows.append(updated_row)
    return updated_rows


def _node_support_bundle(
    node_id: str,
    *,
    query_graph_by_id: dict[str, dict[str, Any]],
    reference_assets_by_id: dict[str, dict[str, Any]],
    footnote_reference_assets_by_id: dict[str, dict[str, Any]],
    reviewed_footnotes_by_node: dict[str, dict[str, Any]],
    indexed_pages_by_asset_id: dict[str, dict[str, Any]],
    family_entries: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    node = query_graph_by_id.get(node_id, {})
    reviewed_record = reviewed_footnotes_by_node.get(node_id, {})
    explicit_ref_ids = list(node.get("explicit_ref_ids", []))
    reviewed_footnote_ref_ids = list(node.get("reviewed_footnote_ref_ids", []))
    inline_assets = _inline_ref_asset_ids_from_text(
        node.get("verbatim_text", ""),
        reference_assets_by_id=reference_assets_by_id,
        footnote_reference_assets_by_id=footnote_reference_assets_by_id,
    )
    explicit_assets = [
        _enrich_supporting_asset(
            reference_assets_by_id[asset_id],
            indexed_pages_by_asset_id=indexed_pages_by_asset_id,
            family_entries=family_entries,
        )
        for asset_id in explicit_ref_ids
        if asset_id in reference_assets_by_id
    ]
    for asset in inline_assets["explicit_refs"]:
        if asset["asset_id"] not in {item["asset_id"] for item in explicit_assets}:
            explicit_assets.append(
                _enrich_supporting_asset(
                    asset,
                    indexed_pages_by_asset_id=indexed_pages_by_asset_id,
                    family_entries=family_entries,
                )
            )
    footnote_assets = [
        _enrich_supporting_asset(
            footnote_reference_assets_by_id[asset_id],
            indexed_pages_by_asset_id=indexed_pages_by_asset_id,
            family_entries=family_entries,
        )
        for asset_id in reviewed_footnote_ref_ids
        if asset_id in footnote_reference_assets_by_id
    ]
    for asset in inline_assets["footnote_supporting_refs"]:
        if asset["asset_id"] not in {item["asset_id"] for item in footnote_assets}:
            footnote_assets.append(
                _enrich_supporting_asset(
                    asset,
                    indexed_pages_by_asset_id=indexed_pages_by_asset_id,
                    family_entries=family_entries,
                )
            )

    return {
        "node_id": node_id,
        "page_title": node.get("page_title"),
        "page_scope_summary": node.get("page_scope_summary"),
        "reviewed_footnotes": reviewed_record.get("footnotes", []),
        "explicit_refs": explicit_assets,
        "footnote_supporting_refs": footnote_assets,
    }


def _build_supporting_context(
    rows: list[dict[str, Any]],
    *,
    query_graph: dict[str, Any],
    reference_assets_payload: dict[str, Any],
    footnote_reference_assets_payload: dict[str, Any],
    reviewed_footnote_links_payload: dict[str, Any],
    primary_md_path: Path,
    pages_json_path: Path,
) -> dict[str, Any]:
    query_graph_by_id = {
        node["id"]: node for node in query_graph.get("nodes", [])
    }
    reference_assets_by_id = _asset_map_by_id(reference_assets_payload)
    footnote_reference_assets_by_id = _asset_map_by_id(footnote_reference_assets_payload)
    reviewed_footnotes = _reviewed_footnotes_by_node(reviewed_footnote_links_payload)
    indexed_pages_by_asset_id, family_entries = _indexed_reference_page_lookup(
        primary_md_path=primary_md_path,
        pages_by_number=_pages_by_number(pages_json_path),
    )
    bundles: dict[str, dict[str, Any]] = {}
    for node_id in _node_ids_from_rows(rows, query_graph_by_id=query_graph_by_id):
        if node_id in bundles:
            continue
        bundles[node_id] = _node_support_bundle(
            node_id,
            query_graph_by_id=query_graph_by_id,
            reference_assets_by_id=reference_assets_by_id,
            footnote_reference_assets_by_id=footnote_reference_assets_by_id,
            reviewed_footnotes_by_node=reviewed_footnotes,
            indexed_pages_by_asset_id=indexed_pages_by_asset_id,
            family_entries=family_entries,
        )
    return {
        "path_node_support": list(bundles.values())
    }


def _node_ids_from_rows(
    rows: list[dict[str, Any]],
    *,
    query_graph_by_id: dict[str, dict[str, Any]],
) -> list[str]:
    ordered: list[str] = []
    seen: set[str] = set()

    def add(node_id: Any) -> None:
        text = str(node_id or "").strip()
        if not text or text in seen or text not in query_graph_by_id:
            return
        seen.add(text)
        ordered.append(text)

    for trace in _build_path_traces(rows):
        for step in trace.get("steps", []):
            if step.get("kind") == "node":
                add(step.get("id"))

    for row in rows:
        for key, value in row.items():
            if not isinstance(key, str):
                continue
            if key == "id" or key.endswith(".id") or key.endswith("_id"):
                if isinstance(value, list):
                    for item in value:
                        add(item)
                else:
                    add(value)

    return ordered


def _bundle_has_helpful_supporting_content(bundle: dict[str, Any]) -> bool:
    return any(
        [
            bool(bundle.get("reviewed_footnotes")),
            bool(bundle.get("explicit_refs")),
            bool(bundle.get("footnote_supporting_refs")),
        ]
    )


def _classify_help_check(
    *,
    path_traces: list[dict[str, Any]],
    result_rows: list[dict[str, Any]],
    supporting_context: dict[str, Any],
) -> dict[str, Any]:
    direct_path_traces = [
        trace
        for trace in path_traces
        if int(trace.get("clinical_step_count", 0) or 0) > 0
    ]
    if direct_path_traces:
        return {
            "classification": "direct_path_support",
            "message": "Direct guideline path found.",
            "reason": (
                f"retrieved {len(direct_path_traces)} path trace(s) with clinical steps; "
                "main-path support is available"
            ),
        }

    support_bundles = list(supporting_context.get("path_node_support", []))
    helpful_support_bundles = [
        bundle for bundle in support_bundles if _bundle_has_helpful_supporting_content(bundle)
    ]
    if helpful_support_bundles:
        return {
            "classification": "supporting_context_only",
            "message": "No direct guideline path found; only supporting context is available.",
            "reason": (
                f"no direct clinical path was retrieved, but {len(helpful_support_bundles)} "
                "node(s) include supporting footnotes or reference content"
            ),
        }

    row_count = len(result_rows)
    if row_count > 0:
        return {
            "classification": "not_supported",
            "message": "No direct or clinically helpful support was found in the current graph.",
            "reason": (
                f"retrieved {row_count} row(s), but no clinical path trace or helpful "
                "supporting content was found"
            ),
        }

    return {
        "classification": "not_supported",
        "message": "No direct or clinically helpful support was found in the current graph.",
        "reason": "no direct clinical path or helpful supporting context was found",
    }


def _build_retrieval_verdict(help_check: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": help_check.get("classification"),
        "message": help_check.get("message"),
        "rationale": help_check.get("reason"),
    }


def _build_execution_error_payload(exc: Exception) -> dict[str, str]:
    return {
        "type": type(exc).__name__,
        "message": str(exc),
    }


def run_live_query(
    *,
    question: str,
    query_case: dict[str, Any] | None = None,
    provider: str = "azure",
    model: str = "gpt-5.1",
    neo4j_uri: str = "bolt://127.0.0.1:7687",
    neo4j_user: str = "neo4j",
    neo4j_password: str = "omgs-nccn-dev",
    query_graph_path: Path | None = None,
    schema_path: Path | None = None,
    sample_queries_path: Path | None = None,
    verbalisation_path: Path | None = None,
    reference_assets_path: Path | None = None,
    footnote_reference_assets_path: Path | None = None,
    reviewed_footnote_links_path: Path | None = None,
    primary_md_path: Path | None = None,
    pages_json_path: Path | None = None,
    output_root: Path | None = None,
) -> dict[str, Any]:
    roots = ov_2025_roots()
    query_root = roots["processed_root"] / "query"
    query_graph_path = query_graph_path or (query_root / "ov_2025_global.query_graph.json")
    schema_path = schema_path or (query_root / "ov_2025_query_schema.json")
    sample_queries_path = sample_queries_path or (query_root / "ov_2025_sample_cypher_queries.json")
    verbalisation_path = verbalisation_path or (query_root / "ov_2025_query_verbalisation_templates.json")
    reference_assets_path = reference_assets_path or (roots["processed_root"] / "text" / "ov_2025_reference_assets.json")
    footnote_reference_assets_path = footnote_reference_assets_path or (roots["processed_root"] / "text" / "ov_2025_footnote_reference_assets.json")
    reviewed_footnote_links_path = reviewed_footnote_links_path or (roots["processed_root"] / "text" / "ov_2025_reviewed_footnote_links.json")
    primary_md_path = primary_md_path or (
        roots["raw_root"]
        / "text_extraction"
        / "22_nccn_ovarian_cancer_v3_2025"
        / "raw"
        / "primary.md"
    )
    pages_json_path = pages_json_path or (
        roots["raw_root"]
        / "text_extraction"
        / "22_nccn_ovarian_cancer_v3_2025"
        / "raw"
        / "native"
        / "pages.json"
    )
    output_root = output_root or roots["live_runs"]

    query_graph = _read_json(query_graph_path)
    schema_payload = _read_json(schema_path)
    sample_queries_payload = _read_json(sample_queries_path)
    verbalisation_payload = _read_json(verbalisation_path)
    reference_assets_payload = _read_json(reference_assets_path)
    footnote_reference_assets_payload = _read_json(footnote_reference_assets_path)
    reviewed_footnote_links_payload = _read_json(reviewed_footnote_links_path)

    candidates = _candidate_nodes(question, query_graph, limit=12)
    page_directory = _page_directory(query_graph)
    llm_schema_payload = _llm_visible_schema(schema_payload)
    llm_sample_queries_payload = _llm_visible_sample_queries(sample_queries_payload)
    client = init_client(provider=provider)
    response = client.chat_completion(
        model=model,
        messages=_build_text2cypher_messages(
            question=question,
            schema_payload=llm_schema_payload,
            sample_queries_payload=llm_sample_queries_payload,
            page_directory=page_directory,
        ),
        temperature=0,
        max_completion_tokens=1800,
    )
    llm_payload = _extract_json_object(response.choices[0].message.content)
    cypher = _repair_cypher(
        llm_payload["cypher"],
        schema_payload=schema_payload,
    )
    cypher = _apply_negative_anchor_guard(cypher, question=question)
    validation_errors = _validate_cypher(
        cypher,
        schema_payload=schema_payload,
    )
    llm_payload["cypher"] = cypher
    llm_payload["validation_errors"] = validation_errors

    execution_error: dict[str, str] | None = None
    serialized_rows: list[dict[str, Any]] = []
    with GraphDatabase.driver(neo4j_uri, auth=(neo4j_user, neo4j_password)) as driver:
        try:
            records, _, _ = driver.execute_query(cypher, database_="neo4j")
            if not records:
                fallback_cypher = _fallback_page_jump_cypher(question, candidates)
                if fallback_cypher:
                    cypher = fallback_cypher
                    llm_payload["cypher"] = cypher
                    llm_payload["query_strategy"] = (
                        str(llm_payload.get("query_strategy") or "").strip()
                        + " | fallback_page_jump_path"
                    ).strip(" |")
                    records, _, _ = driver.execute_query(cypher, database_="neo4j")
            for record in records:
                row = {key: _serialize_value(value) for key, value in record.items()}
                serialized_rows.append(row)
            serialized_rows = _apply_page_jump_continuation(
                serialized_rows,
                driver=driver,
            )
        except Exception as exc:
            execution_error = _build_execution_error_payload(exc)

    verbalized_paths = _verbalize_paths(serialized_rows, verbalisation_payload)
    path_traces = _build_path_traces(serialized_rows)
    supporting_context = _build_supporting_context(
        serialized_rows,
        query_graph=query_graph,
        reference_assets_payload=reference_assets_payload,
        footnote_reference_assets_payload=footnote_reference_assets_payload,
        reviewed_footnote_links_payload=reviewed_footnote_links_payload,
        primary_md_path=primary_md_path,
        pages_json_path=pages_json_path,
    )
    help_check = _classify_help_check(
        path_traces=path_traces,
        result_rows=serialized_rows,
        supporting_context=supporting_context,
    )
    if execution_error:
        help_check = {
            "classification": "not_supported",
            "message": "No direct or clinically helpful support was found in the current graph.",
            "reason": f"query execution failed: {execution_error['type']}",
        }
    retrieval_verdict = _build_retrieval_verdict(help_check)

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    output_payload = {
        "status": "live_query_result",
        "timestamp_utc": timestamp,
        "query_case": query_case,
        "question": question,
        "generated_cypher": cypher,
        "neo4j_uri": neo4j_uri,
        "provider": provider,
        "model": model,
        "path_traces": path_traces,
        "verbalized_paths": verbalized_paths,
        "supporting_context": supporting_context,
        "help_check": help_check,
        "retrieval_verdict": retrieval_verdict,
        "execution_error": execution_error,
        "result_row_count": len(serialized_rows),
        "result_rows": serialized_rows,
        "page_directory": page_directory,
        "candidate_nodes": candidates,
        "generated_query": llm_payload,
    }

    out_path = output_root / f"{timestamp}_live_query_result.json"
    latest_path = output_root / "latest_live_query_result.json"
    _write_json(out_path, output_payload)
    _write_json(latest_path, output_payload)
    output_payload["output_path"] = str(out_path)
    output_payload["latest_path"] = str(latest_path)
    return output_payload


def resolve_question_from_case(
    *,
    case_file: Path,
    case_id: str,
    prefer_language: str = "zh",
) -> dict[str, Any]:
    payload = _read_json_any(case_file)
    if not isinstance(payload, list):
        raise ValueError("query_case_file_must_be_a_json_array")
    for item in payload:
        if str(item.get("case_id")) != str(case_id):
            continue
        question = item.get(prefer_language) or item.get("zh") or item.get("en")
        if not isinstance(question, str) or not question.strip():
            raise ValueError(f"query_case_missing_question:{case_id}")
        return {
            "case_id": str(item.get("case_id")),
            "title": item.get("title"),
            "question": question.strip(),
            "zh": item.get("zh"),
            "en": item.get("en"),
            "source_path": str(case_file),
        }
    raise ValueError(f"query_case_not_found:{case_id}")
