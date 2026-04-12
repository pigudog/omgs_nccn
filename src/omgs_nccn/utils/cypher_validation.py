from __future__ import annotations

import re
from typing import Any


NEGATION_CUES = (
    " no ",
    " negative",
    " absence",
    " not ",
    " without ",
)


def sanitize_cypher(cypher: str) -> str:
    fixed = cypher.strip()
    fixed = re.sub(
        r"\b(MATCH|OPTIONAL MATCH)\s+([A-Za-z_][A-Za-z0-9_]*)\s*:(GuidelineNode)\b",
        r"\1 (\2:\3)",
        fixed,
    )
    fixed = fixed.replace("|:", "|")
    fixed = re.sub(
        r"MATCH\s+p\s*=\s*([A-Za-z_][A-Za-z0-9_]*)\s*-\[",
        r"MATCH p = (\1)-[",
        fixed,
    )
    return fixed


def cypher_declared_vars_before_first_where(cypher: str) -> set[str]:
    prefix = re.split(r"\bWHERE\b", cypher, maxsplit=1, flags=re.IGNORECASE)[0]
    return set(re.findall(r"\(([A-Za-z_][A-Za-z0-9_]*)\s*:", prefix))


def apply_negative_anchor_guard(cypher: str, *, question: str) -> str:
    normalized = f" {question.lower()} "
    if any(cue in normalized for cue in NEGATION_CUES):
        return cypher
    declared_vars = cypher_declared_vars_before_first_where(cypher)

    def _replace(match: re.Match[str]) -> str:
        alias = match.group("alias")
        if alias not in declared_vars:
            return match.group(0)
        injected = (
            f"WHERE NOT toLower({alias}.verbatim_text) STARTS WITH 'no ' "
            f"AND {match.group('body').lstrip()}"
        )
        return injected

    return re.sub(
        r"WHERE(?P<body>\s+toLower\((?P<alias>[A-Za-z_][A-Za-z0-9_]*)\.verbatim_text\)\s+CONTAINS\s+['\"][^'\"]+['\"])",
        _replace,
        cypher,
        count=1,
        flags=re.IGNORECASE,
    )


def split_cypher_clauses(cypher: str) -> list[tuple[str, str]]:
    pattern = re.compile(r"\b(MATCH|OPTIONAL MATCH|WHERE|WITH|RETURN|ORDER BY)\b", re.IGNORECASE)
    matches = list(pattern.finditer(cypher))
    clauses: list[tuple[str, str]] = []
    for idx, match in enumerate(matches):
        start = match.start()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(cypher)
        clause_type = match.group(1).upper()
        clause_body = cypher[start + len(match.group(1)) : end].strip()
        clauses.append((clause_type, clause_body))
    return clauses


def cypher_projected_vars(clause_body: str) -> set[str]:
    body = re.sub(r"^\s*DISTINCT\s+", "", clause_body, flags=re.IGNORECASE)
    parts = [part.strip() for part in body.split(",")]
    projected: set[str] = set()
    for part in parts:
        if not part:
            continue
        token = part.split()[0]
        token = token.split(".")[0]
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", token):
            projected.add(token)
    return projected


def validate_cypher(cypher: str, *, schema_payload: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if re.search(r"\bMATCH\s+[A-Za-z_][A-Za-z0-9_]*\s*:GuidelineNode\b", cypher):
        errors.append("invalid_match_node_pattern")
    allowed_props = set(schema_payload.get("node_properties", []))
    allowed_relations = set(schema_payload.get("relation_type_vocab", []))
    for var, prop in re.findall(r"([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)", cypher):
        if prop not in allowed_props:
            errors.append(f"unknown_property:{prop}")
    for rel_blob in re.findall(r"\[:([A-Z_|]+)(?:\*[0-9]+\.\.[0-9]+)?\]", cypher):
        for rel in rel_blob.split("|"):
            if rel and rel not in allowed_relations:
                errors.append(f"unknown_relation:{rel}")
    declared_vars = set(re.findall(r"\(([A-Za-z_][A-Za-z0-9_]*)\s*:", cypher))
    referenced_vars = {
        var
        for var, _prop in re.findall(r"([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)", cypher)
    }
    for var in sorted(referenced_vars - declared_vars):
        errors.append(f"undefined_variable:{var}")
    projected_after_with: set[str] | None = None
    for clause_type, clause_body in split_cypher_clauses(cypher):
        if clause_type == "WITH":
            projected_after_with = cypher_projected_vars(clause_body)
        elif clause_type == "RETURN":
            projected_after_with = cypher_projected_vars(clause_body)
        elif clause_type == "ORDER BY" and projected_after_with is not None:
            for var, _prop in re.findall(r"([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)", clause_body):
                if var not in projected_after_with:
                    errors.append(f"order_by_scope:{var}")
    return sorted(set(errors))


def repair_cypher(cypher: str, *, schema_payload: dict[str, Any]) -> str:
    fixed = sanitize_cypher(cypher)
    errors = validate_cypher(fixed, schema_payload=schema_payload)
    if "ORDER BY" in fixed and "RETURN DISTINCT" in fixed:
        fixed = re.sub(r"\bORDER BY\b.*$", "", fixed, flags=re.IGNORECASE | re.DOTALL).strip()
    elif any(item.startswith("order_by_scope:") for item in errors):
        fixed = re.sub(r"\bORDER BY\b.*$", "", fixed, flags=re.IGNORECASE | re.DOTALL).strip()
    return fixed
