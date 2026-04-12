from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from omgs_nccn.config.paths import REPO_ROOT
from omgs_nccn.config.paths import ov_2025_roots
from omgs_nccn.llm.client import init_client


QUERY_TYPES = [
    "pathway_overview",
    "disease_to_treatment",
    "disease_to_evaluation",
    "evaluation_to_decision",
    "treatment_followup",
    "page_jump_continuation",
    "monitoring_followup",
    "biomarker_or_testing",
    "branch_disambiguation",
    "stress_test_partial_support",
]

PROMPT_VERSION = "qwen_query_testset_v1"
MAX_PAGE_RETRIES = 3


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text())


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")


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


def _default_page_labels(input_root: Path) -> list[str]:
    labels: list[str] = []
    for child in sorted(input_root.iterdir()):
        if not child.is_dir():
            continue
        if (child / "page_graph.typed.json").exists() and (child / "page_context.json").exists():
            labels.append(child.name)
    return sorted(labels, key=_page_sort_key)


def _page_sort_key(page_code: str) -> tuple[int, str, int]:
    match = re.match(r"^(OV|LCOC)-(\d+)$", page_code)
    if match:
        family_rank = 0 if match.group(1) == "OV" else 1
        return (family_rank, match.group(1), int(match.group(2)))
    return (99, page_code, 0)


def _typed_nodes_for_prompt(page_graph: dict[str, Any]) -> list[dict[str, Any]]:
    payload: list[dict[str, Any]] = []
    for node in page_graph.get("nodes", []):
        payload.append(
            {
                "id": node.get("id"),
                "node_label": node.get("node_label"),
                "verbatim_text": node.get("verbatim_text"),
            }
        )
    return payload


def _build_messages(*, page_context: dict[str, Any], typed_nodes: list[dict[str, Any]]) -> list[dict[str, str]]:
    system_prompt = """You generate evaluation-ready clinical queries for an NCCN ovarian cancer pathway graph.

The graph is built from NCCN flowchart pages.
It is a typed pathway graph, not a general biomedical knowledge graph.

Your task is to generate realistic English patient-style or clinician-style queries that are answerable, partially answerable, or intentionally stress-test the current page graph.

Requirements:
- Generate exactly 10 queries for the given page.
- Use only information grounded in the provided page context and nodes.
- Prefer clinically realistic wording.
- Cover diverse query intents.
- Do not invent unsupported biomarkers, therapies, or page references unless the goal is an explicit stress-test query.
- Output JSON only.
"""
    user_prompt = f"""Generate 10 English query cases for this single NCCN page.

Return exactly one JSON object with this schema:
{{
  "page_code": str,
  "page_title": str,
  "queries": [
    {{
      "query_id": str,
      "query_type": str,
      "question": str,
      "seed_node_ids": [str],
      "expected_focus": str
    }}
  ]
}}

Allowed query_type values:
- pathway_overview
- disease_to_treatment
- disease_to_evaluation
- evaluation_to_decision
- treatment_followup
- page_jump_continuation
- monitoring_followup
- biomarker_or_testing
- branch_disambiguation
- stress_test_partial_support

Generation rules:
1. Generate exactly 10 queries, one per query_type above.
2. Keep questions in English.
3. Make questions sound like realistic clinician or case-review queries.
4. Ground each query in the provided page context and node texts.
5. Use `seed_node_ids` to name the node ids most relevant to the query.
6. `expected_focus` should briefly state what the query is trying to retrieve.
7. For `stress_test_partial_support`, you may add one realistic extra patient detail that may be only partially supported by the current page graph.
8. For `page_jump_continuation`, use a node that leads to another numbered pathway page when available.
9. Do not output markdown. Output JSON only.

Page context:
{json.dumps(page_context, ensure_ascii=False, indent=2)}

Typed nodes:
{json.dumps(typed_nodes, ensure_ascii=False, indent=2)}
"""
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]


def _build_repair_messages(
    *,
    page_context: dict[str, Any],
    typed_nodes: list[dict[str, Any]],
    previous_output: str,
    validation_error: str,
) -> list[dict[str, str]]:
    messages = _build_messages(page_context=page_context, typed_nodes=typed_nodes)
    messages.append(
        {
            "role": "assistant",
            "content": previous_output,
        }
    )
    messages.append(
        {
            "role": "user",
            "content": (
                "Your previous JSON failed validation.\n"
                f"Validation error: {validation_error}\n"
                "Return a corrected JSON object only.\n"
                "Do not repeat any query_type. Use each allowed query_type exactly once.\n"
                "Keep exactly 10 queries."
            ),
        }
    )
    return messages


def _validate_page_queries(payload: dict[str, Any], *, expected_page_code: str, expected_page_title: str) -> dict[str, Any]:
    queries = payload.get("queries")
    if not isinstance(queries, list) or len(queries) != 10:
        raise ValueError("page_query_generation_did_not_return_10_queries")
    seen_types: set[str] = set()
    normalized_queries: list[dict[str, Any]] = []
    for item in queries:
        query_type = item.get("query_type")
        if query_type not in QUERY_TYPES:
            raise ValueError(f"invalid_query_type:{query_type}")
        if query_type in seen_types:
            raise ValueError(f"duplicate_query_type:{query_type}")
        seen_types.add(query_type)
        normalized_queries.append(
            {
                "query_id": str(item.get("query_id") or ""),
                "query_type": query_type,
                "question": str(item.get("question") or "").strip(),
                "seed_node_ids": [str(x) for x in item.get("seed_node_ids", [])],
                "expected_focus": str(item.get("expected_focus") or "").strip(),
            }
        )
    return {
        "page_code": expected_page_code,
        "page_title": expected_page_title,
        "queries": normalized_queries,
    }


def _generate_single_page_queries(
    *,
    client: Any,
    model: str,
    page_code: str,
    page_context: dict[str, Any],
    typed_nodes: list[dict[str, Any]],
) -> dict[str, Any]:
    messages = _build_messages(page_context=page_context, typed_nodes=typed_nodes)
    last_error: Exception | None = None
    for _attempt in range(MAX_PAGE_RETRIES):
        response = client.chat_completion(
            model=model,
            messages=messages,
            temperature=0,
            max_completion_tokens=2200,
        )
        response_text = str(response.choices[0].message.content or "")
        try:
            page_result = _extract_json_object(response_text)
            return _validate_page_queries(
                page_result,
                expected_page_code=page_code,
                expected_page_title=str(page_context.get("page_title") or page_code),
            )
        except Exception as exc:
            last_error = exc
            messages = _build_repair_messages(
                page_context=page_context,
                typed_nodes=typed_nodes,
                previous_output=response_text,
                validation_error=str(exc),
            )
    if last_error is not None:
        raise last_error
    raise RuntimeError(f"query_testset_generation_failed:{page_code}")


def generate_query_testset(
    *,
    page_labels: list[str] | None = None,
    input_root: Path | None = None,
    provider: str = "qwen",
    model: str = "qwen3-max",
    output_path: Path | None = None,
) -> dict[str, Any]:
    roots = ov_2025_roots()
    input_root = input_root or (roots["processed_root"] / "pages")
    output_path = output_path or (REPO_ROOT / "example" / "query_test.json")
    page_labels = page_labels or _default_page_labels(input_root)

    client = init_client(provider=provider)
    page_payloads: list[dict[str, Any]] = []
    for page_code in page_labels:
        page_dir = input_root / page_code
        page_context = _read_json(page_dir / "page_context.json")
        page_graph = _read_json(page_dir / "page_graph.typed.json")
        typed_nodes = _typed_nodes_for_prompt(page_graph)
        page_payloads.append(
            _generate_single_page_queries(
                client=client,
                model=model,
                page_code=page_code,
                page_context=page_context,
                typed_nodes=typed_nodes,
            )
        )

    payload = {
        "status": "query_testset_ready",
        "prompt_version": PROMPT_VERSION,
        "provider": provider,
        "model": model,
        "page_count": len(page_payloads),
        "query_count": sum(len(page["queries"]) for page in page_payloads),
        "pages": page_payloads,
    }
    _write_json(output_path, payload)
    payload["output_path"] = str(output_path)
    return payload
