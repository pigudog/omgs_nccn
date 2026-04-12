from __future__ import annotations

import re
from typing import Any


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


def question_tokens(question: str) -> list[str]:
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


def question_intents(question: str) -> dict[str, bool]:
    lowered = f" {question.lower()} "
    return {
        "page_jump": any(term in lowered for term in PAGE_JUMP_HINT_TERMS),
        "allow_negative_anchor": any(term in lowered for term in NEGATION_CUES),
        "support_check": "does this guideline support" in lowered or "instead of" in lowered,
    }


def candidate_nodes(question: str, query_graph: dict[str, Any], *, limit: int = 12) -> list[dict[str, Any]]:
    tokens = question_tokens(question)
    intents = question_intents(question)
    ranked: list[tuple[int, dict[str, Any]]] = []
    for node in query_graph["nodes"]:
        text = f"{node.get('verbatim_text', '')} {node.get('text_snippet', '')}".lower()
        score = sum(1 for token in tokens if token in text)
        if score > 0:
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
