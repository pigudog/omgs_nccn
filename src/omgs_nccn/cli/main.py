import argparse
import json
import re
from pathlib import Path

from omgs_nccn.cli.spinner import Spinner
from omgs_nccn.pipeline.bootstrap import build_bootstrap_summary
from omgs_nccn.pipeline.phase1 import build_llm_drafts_for_pages
from omgs_nccn.pipeline.phase1 import build_page_graph_drafts_for_pages
from omgs_nccn.pipeline.phase1 import ensure_ov_2025_layout
from omgs_nccn.pipeline.phase1 import initialize_phase1_ov_2025
from omgs_nccn.pipeline.phase2_semantics import build_page_semantics
from omgs_nccn.pipeline.phase2 import build_reviewed_global_graph
from omgs_nccn.pipeline.phase3 import build_rule_graph
from omgs_nccn.pipeline.phase5 import build_engine_handoff_assets
from omgs_nccn.pipeline.phase5 import build_query_assets_with_support
from omgs_nccn.pipeline.phase5 import build_support_assets
from omgs_nccn.query.query_testset import generate_query_testset
from omgs_nccn.query.live_query import run_live_query
from omgs_nccn.query.live_query import resolve_question_from_case


def _truncate_text(value: str | None, *, limit: int = 220) -> str:
    if not value:
        return ""
    compact = " ".join(str(value).split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 3] + "..."


def _extract_where_contains(cypher: str, alias: str) -> list[str]:
    patterns = [
        rf"toLower\(\s*{alias}\.verbatim_text\s*\)\s+CONTAINS\s+'([^']+)'",
        rf'toLower\(\s*{alias}\.verbatim_text\s*\)\s+CONTAINS\s+"([^"]+)"',
    ]
    matches: list[str] = []
    for pattern in patterns:
        matches.extend(re.findall(pattern, cypher, flags=re.IGNORECASE))
    deduped: list[str] = []
    seen: set[str] = set()
    for item in matches:
        normalized = item.strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        deduped.append(normalized)
    return deduped


def _extract_node_label_filter(cypher: str, alias: str) -> str | None:
    patterns = [
        rf"{alias}:GuidelineNode\s*\{{\s*node_label:\s*'([^']+)'\s*\}}",
        rf'{alias}:GuidelineNode\s*\{{\s*node_label:\s*"([^"]+)"\s*\}}',
    ]
    for pattern in patterns:
        match = re.search(pattern, cypher)
        if match:
            return match.group(1).strip()
    return None


def _extract_path_plan(cypher: str) -> dict:
    plan = {
        "anchor_node_label": None,
        "anchor_text_terms": [],
        "path_relations": [],
        "hop_range": None,
        "target_node_label": None,
        "target_text_terms": [],
    }
    path_match = re.search(
        r"MATCH\s+p\s*=\s*\((\w+)\)\s*-\[:([A-Z_|]+)\*([0-9]+)\.\.([0-9]+)\]->\((\w+)(?::GuidelineNode)?(?:\s*\{\s*node_label:\s*'([^']+)'\s*\})?\)",
        cypher,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if path_match:
        source_alias = path_match.group(1)
        rel_blob = path_match.group(2)
        hop_from = path_match.group(3)
        hop_to = path_match.group(4)
        target_alias = path_match.group(5)
        inline_target_label = path_match.group(6)
        plan["anchor_node_label"] = _extract_node_label_filter(cypher, source_alias)
        plan["anchor_text_terms"] = _extract_where_contains(cypher, source_alias)
        plan["path_relations"] = [item.strip() for item in rel_blob.split("|") if item.strip()]
        plan["hop_range"] = f"{hop_from}..{hop_to}"
        plan["target_node_label"] = inline_target_label or _extract_node_label_filter(cypher, target_alias)
        plan["target_text_terms"] = _extract_where_contains(cypher, target_alias)
    return plan


def _extract_anchor_nodes(result: dict) -> list[dict]:
    anchors: list[dict] = []
    seen: set[str] = set()
    for trace in result.get("path_traces", []):
        steps = trace.get("steps", [])
        if not steps:
            continue
        first = steps[0]
        if first.get("kind") != "node":
            continue
        node_id = first.get("id")
        if not node_id or node_id in seen:
            continue
        seen.add(node_id)
        anchors.append(first)
    return anchors


def _explain_anchor_match(node: dict, plan: dict) -> str:
    reasons: list[str] = []
    expected_label = plan.get("anchor_node_label")
    if expected_label:
        actual_label = node.get("node_label")
        if actual_label == expected_label:
            reasons.append(f"node_label={actual_label}")
    text = (node.get("verbatim_text") or "").lower()
    matched_terms = [
        term for term in plan.get("anchor_text_terms", []) if term.lower() in text
    ]
    if matched_terms:
        reasons.append("text contains " + ", ".join(matched_terms))
    if not reasons:
        return "selected as path start by generated Cypher"
    return "matched because " + " and ".join(reasons)


def _print_phase5_live_query_text(result: dict) -> None:
    print("phase6 live query ready")
    if result.get("query_case"):
        case_meta = result["query_case"]
        print(f"case_id={case_meta['case_id']}")
        if case_meta.get("title"):
            print(f"title={case_meta['title']}")
    print(f"output_path={result['output_path']}")
    print(f"latest_path={result['latest_path']}")
    print("")
    print("Question")
    print(f"- {result.get('question')}")
    print("")
    print("Generated Cypher")
    print(result["generated_cypher"])
    plan = _extract_path_plan(result.get("generated_cypher", ""))
    if any(
        [
            plan.get("anchor_node_label"),
            plan.get("anchor_text_terms"),
            plan.get("path_relations"),
            plan.get("hop_range"),
            plan.get("target_node_label"),
            plan.get("target_text_terms"),
        ]
    ):
        print("")
        print("Cypher Plan")
        if plan.get("anchor_node_label"):
            print(f"- anchor_node_label={plan['anchor_node_label']}")
        if plan.get("anchor_text_terms"):
            print(f"- anchor_text_terms={', '.join(plan['anchor_text_terms'])}")
        if plan.get("path_relations"):
            print(f"- path_relations={', '.join(plan['path_relations'])}")
        if plan.get("hop_range"):
            print(f"- hop_range={plan['hop_range']}")
        if plan.get("target_node_label"):
            print(f"- target_node_label={plan['target_node_label']}")
        if plan.get("target_text_terms"):
            print(f"- target_text_terms={', '.join(plan['target_text_terms'])}")
    print("")
    print("Retrieval Summary")
    print(f"- result_row_count={result['result_row_count']}")
    print(f"- candidate_node_count={len(result.get('candidate_nodes', []))}")
    verdict = result.get("retrieval_verdict") or {}
    if verdict.get("status"):
        print("- retrieval_verdict:")
        print(f"  - status={verdict['status']}")
        if verdict.get("message"):
            print(f"  - message={verdict['message']}")
        if verdict.get("rationale"):
            print(f"  - rationale={verdict['rationale']}")
    execution_error = result.get("execution_error") or {}
    if execution_error.get("type"):
        print("- execution_error:")
        print(f"  - type={execution_error['type']}")
        if execution_error.get("message"):
            print(f"  - message={execution_error['message']}")
    anchors = _extract_anchor_nodes(result)
    if anchors:
        print("- matched_anchor_nodes:")
        for node in anchors:
            snippet = _truncate_text(node.get("verbatim_text"))
            print(
                f"  - {node.get('id')} | {node.get('page_code')} | "
                f"{node.get('node_label')} | {snippet}"
            )
            print(f"    why: {_explain_anchor_match(node, plan)}")
    if result.get("candidate_nodes"):
        print("- top_candidate_nodes:")
        for node in result["candidate_nodes"][:5]:
            snippet = _truncate_text(node.get("verbatim_text"))
            print(
                f"  - {node.get('id')} | {node.get('page_code')} | "
                f"{node.get('node_label')} | {snippet}"
            )
    if result.get("path_traces"):
        print("")
        print("Path Traces")
        for idx, trace in enumerate(result["path_traces"], start=1):
            print(f"- trace_{idx}:")
            print(
                f"  graph_hops={trace.get('graph_hop_count', 0)} | "
                f"clinical_steps={trace.get('clinical_step_count', 0)}"
            )
            for line in trace.get("trace_lines", []):
                print(f"  {line}")
    if result.get("verbalized_paths"):
        print("")
        print("Path Interpretation")
        for item in result["verbalized_paths"]:
            print(f"- {item}")
    support = result.get("supporting_context", {}).get("path_node_support", [])
    if support:
        print("")
        print("Supporting Context")
        for bundle in support:
            print(f"- {bundle['node_id']}:")
            if bundle.get("page_title"):
                print(f"  page_title: {bundle['page_title']}")
            if bundle.get("page_scope_summary"):
                print(f"  page_scope_summary: {_truncate_text(bundle['page_scope_summary'], limit=180)}")
            reviewed = bundle.get("reviewed_footnotes", [])
            if reviewed:
                print("  reviewed_footnotes:")
                for item in reviewed[:5]:
                    print(f"    - [{item.get('label')}] {_truncate_text(item.get('text'), limit=220)}")
            explicit_refs = bundle.get("explicit_refs", [])
            if explicit_refs:
                print("  explicit_refs:")
                for asset in explicit_refs[:5]:
                    asset_desc = asset.get("target_page_family") or asset.get("target_page_code") or asset.get("asset_id")
                    print(f"    - {asset.get('asset_id')} | {asset_desc}")
                    resolved = asset.get("resolved_ref_page")
                    if resolved:
                        print(
                            f"      page: {resolved.get('target_label')} | page_number={resolved.get('page_number')}"
                        )
                        excerpt = resolved.get("content_excerpt")
                        if excerpt:
                            print(f"      excerpt: {_truncate_text(excerpt, limit=260)}")
            footnote_refs = bundle.get("footnote_supporting_refs", [])
            if footnote_refs:
                print("  footnote_supporting_refs:")
                for asset in footnote_refs[:5]:
                    asset_desc = asset.get("target_page_family") or asset.get("target_page_code") or asset.get("asset_id")
                    print(f"    - {asset.get('asset_id')} | {asset_desc}")
                    resolved = asset.get("resolved_ref_page")
                    if resolved:
                        print(
                            f"      page: {resolved.get('target_label')} | page_number={resolved.get('page_number')}"
                        )
                        excerpt = resolved.get("content_excerpt")
                        if excerpt:
                            print(f"      excerpt: {_truncate_text(excerpt, limit=260)}")
                    overview = asset.get("resolved_ref_family_overview")
                    if overview:
                        pages = ", ".join(
                            item.get("target_label", "")
                            for item in overview.get("available_pages", [])
                            if item.get("target_label")
                        )
                        if pages:
                            print(f"      family_pages: {pages}")
                        excerpt = overview.get("overview_excerpt")
                        if excerpt:
                            print(f"      overview: {_truncate_text(excerpt, limit=220)}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="omgs-nccn",
        description="Root CLI for the omgs-nccn repository.",
    )
    parser.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="Output format.",
    )
    subparsers = parser.add_subparsers(dest="command")

    phase1 = subparsers.add_parser(
        "phase1",
        help="Phase 1 OV 2025 utilities.",
    )
    phase1_subparsers = phase1.add_subparsers(dest="phase1_command")

    phase1_subparsers.add_parser(
        "init-layout",
        help="Create the canonical Phase 1 OV 2025 directory layout.",
    )

    build_inventory = phase1_subparsers.add_parser(
        "build-page-inventory",
        help="Generate page assets manifest and page inventory from the OV 2025 PDF.",
    )
    build_inventory.add_argument(
        "--pdf",
        default="data/ref/nccn_ovarian_cancer_v3_2025.pdf",
        help="Path to the OV 2025 NCCN PDF.",
    )

    build_llm = phase1_subparsers.add_parser(
        "build-llm-drafts",
        help="Generate page-local LLM node and edge drafts for selected OV 2025 pages.",
    )
    build_llm.add_argument(
        "--pages",
        nargs="+",
        required=True,
        help="Page labels such as OV-1 OV-2 OV-3.",
    )
    build_llm.add_argument(
        "--image-root",
        required=True,
        help="Directory containing page_XXX.png page images.",
    )
    build_llm.add_argument(
        "--inventory",
        default="data/raw/ov_2025/page_assets/page_inventory.json",
        help="Path to the Phase 1 page inventory manifest.",
    )
    build_llm.add_argument(
        "--model",
        default="gpt-5.1",
        help="Model name to use for LLM draft generation.",
    )

    build_page_graph = phase1_subparsers.add_parser(
        "build-page-graph-drafts",
        help="Normalize page-local LLM drafts into canonical page_graph.draft.json files.",
    )
    build_page_graph.add_argument(
        "--pages",
        nargs="+",
        required=True,
        help="Page labels such as OV-1 OV-2 OV-3.",
    )

    phase2 = subparsers.add_parser(
        "phase2",
        help="Phase 2 OV 2025 page-level cleanup/context/typing utilities.",
    )
    phase2_subparsers = phase2.add_subparsers(dest="phase2_command")

    build_page_semantics_cmd = phase2_subparsers.add_parser(
        "build-page-semantics",
        help="Build typed page graph assets from reviewed page graphs using page context, node typing, and edge relation labeling.",
    )
    build_page_semantics_cmd.add_argument(
        "--input-root",
        default="data/processed/ov_2025/pages",
        help="Directory containing per-page reviewed graph files.",
    )
    build_page_semantics_cmd.add_argument(
        "--pages",
        nargs="+",
        required=True,
        help="Page labels such as OV-1 OV-2 OV-3.",
    )
    build_page_semantics_cmd.add_argument(
        "--pages-json",
        default="data/raw/ov_2025/text_extraction/22_nccn_ovarian_cancer_v3_2025/raw/native/pages.json",
        help="Path to native per-page text JSON used for page-level context extraction.",
    )
    build_page_semantics_cmd.add_argument(
        "--model",
        default="gpt-5.1",
        help="Model name to use for page context, node typing, and edge relation labeling.",
    )
    build_page_semantics_cmd.add_argument(
        "--no-resume",
        action="store_true",
        help="Disable page-level resume and recompute all selected pages.",
    )
    build_page_semantics_cmd.add_argument(
        "--force-page",
        nargs="+",
        default=[],
        help="One or more page codes to force-rerun while leaving other cached page outputs untouched.",
    )

    phase3 = subparsers.add_parser(
        "phase3",
        help="Phase 3 OV 2025 global stitch utilities.",
    )
    phase3_subparsers = phase3.add_subparsers(dest="phase3_command")

    build_global = phase3_subparsers.add_parser(
        "build-reviewed-global-graph",
        help="Validate typed page graphs and stitch them into the global OV 2025 graph.",
    )
    build_global.add_argument(
        "--input-root",
        default="data/processed/ov_2025/pages",
        help="Directory containing per-page typed graph files.",
    )
    build_global.add_argument(
        "--stitch-map",
        default="data/manifests/ov_2025_stitch_map.json",
        help="Path to the repository-owned stitch map JSON.",
    )

    phase4 = subparsers.add_parser(
        "phase4",
        help="Phase 4 OV 2025 canonical rule graph utilities.",
    )
    phase4_subparsers = phase4.add_subparsers(dest="phase4_command")

    build_rule = phase4_subparsers.add_parser(
        "build-rule-graph",
        help="Normalize the stitched reviewed global graph into the canonical rule graph.",
    )
    build_rule.add_argument(
        "--reviewed-graph",
        default="data/processed/ov_2025/reviewed_graph/ov_2025_global.reviewed_graph.json",
        help="Path to the stitched reviewed global graph JSON.",
    )

    phase5 = subparsers.add_parser(
        "phase5",
        help="Phase 5 OV 2025 engine handoff asset build utilities.",
    )
    phase5_subparsers = phase5.add_subparsers(dest="phase5_command")

    build_query = phase5_subparsers.add_parser(
        "build-query-assets",
        help="Build Neo4j import-ready query assets, sample Cypher, and verbalisation templates from the canonical rule graph.",
    )
    build_query.add_argument(
        "--rule-graph",
        default="data/processed/ov_2025/rule_graph/ov_2025_global.rule_graph.json",
        help="Path to the canonical rule graph JSON.",
    )
    build_support = phase5_subparsers.add_parser(
        "build-support-assets",
        help="Build deterministic support sidecars needed for engine handoff and query-time supporting context.",
    )
    build_support.add_argument(
        "--rule-graph",
        default="data/processed/ov_2025/rule_graph/ov_2025_global.rule_graph.json",
        help="Path to the canonical rule graph JSON.",
    )
    build_support.add_argument(
        "--native-md",
        default="data/raw/ov_2025/text_extraction/22_nccn_ovarian_cancer_v3_2025/raw/native/primary.md",
        help="Path to the native primary markdown used for explicit ref extraction.",
    )
    build_support.add_argument(
        "--pages-json",
        default="data/raw/ov_2025/text_extraction/22_nccn_ovarian_cancer_v3_2025/raw/native/pages.json",
        help="Path to the native per-page JSON used for page-anchored footnote recovery.",
    )
    build_support.add_argument(
        "--overrides",
        default="data/manifests/ov_2025_footnote_link_overrides.json",
        help="Repository-owned page/node reviewed footnote override manifest.",
    )
    build_handoff = phase5_subparsers.add_parser(
        "build-engine-handoff-assets",
        help="Build the full NCCN handoff asset set required by omgs_engine.",
    )
    build_handoff.add_argument(
        "--rule-graph",
        default="data/processed/ov_2025/rule_graph/ov_2025_global.rule_graph.json",
        help="Path to the canonical rule graph JSON.",
    )
    build_handoff.add_argument(
        "--native-md",
        default="data/raw/ov_2025/text_extraction/22_nccn_ovarian_cancer_v3_2025/raw/native/primary.md",
        help="Path to the native primary markdown used for explicit ref extraction.",
    )
    build_handoff.add_argument(
        "--pages-json",
        default="data/raw/ov_2025/text_extraction/22_nccn_ovarian_cancer_v3_2025/raw/native/pages.json",
        help="Path to the native per-page JSON used for page-anchored footnote recovery.",
    )
    build_handoff.add_argument(
        "--overrides",
        default="data/manifests/ov_2025_footnote_link_overrides.json",
        help="Repository-owned page/node reviewed footnote override manifest.",
    )

    phase6 = subparsers.add_parser(
        "phase6",
        help="Phase 6 OV 2025 query smoke and runtime validation utilities.",
    )
    phase6_subparsers = phase6.add_subparsers(dest="phase6_command")

    build_query_testset_cmd = phase6_subparsers.add_parser(
        "build-query-testset",
        help="Generate page-scoped English query cases serially from typed NCCN pages.",
    )
    build_query_testset_cmd.add_argument(
        "--input-root",
        default="data/processed/ov_2025/pages",
        help="Root directory containing per-page typed graph folders.",
    )
    build_query_testset_cmd.add_argument(
        "--pages",
        nargs="+",
        default=None,
        help="Optional subset of page codes to generate. Defaults to all typed pages under input-root.",
    )
    build_query_testset_cmd.add_argument(
        "--provider",
        choices=("azure", "qwen", "qwen-2.5-3b", "openai", "openrouter", "qwen_compat"),
        default="qwen",
        help="LLM provider for page-scoped query generation.",
    )
    build_query_testset_cmd.add_argument(
        "--model",
        default="qwen3-max",
        help="LLM model used to generate the page-scoped query test set.",
    )
    build_query_testset_cmd.add_argument(
        "--output",
        default="example/query_test.json",
        help="Output JSON path for the generated query test set.",
    )
    run_query = phase6_subparsers.add_parser(
        "run-live-query",
        help="Generate Cypher from a clinical question, execute it against live Neo4j, and persist the retrieval result.",
    )
    run_query.add_argument(
        "--question",
        default=None,
        help="Clinical question to convert into Cypher and execute.",
    )
    run_query.add_argument(
        "--query-case-file",
        default="example/query_cases.json",
        help="Path to a repository-owned query case JSON file.",
    )
    run_query.add_argument(
        "--case-id",
        default=None,
        help="Case id in the query case file. When provided, the question is loaded from the case file.",
    )
    run_query.add_argument(
        "--case-language",
        choices=("zh", "en"),
        default="zh",
        help="Preferred language field to load from the query case file.",
    )
    run_query.add_argument(
        "--provider",
        choices=("azure", "qwen", "qwen-2.5-3b", "openai", "openrouter", "qwen_compat"),
        default="qwen",
        help="LLM provider for Text2Cypher generation.",
    )
    run_query.add_argument(
        "--model",
        default="qwen3-max",
        help="Model or deployment name to use for Text2Cypher generation.",
    )
    run_query.add_argument(
        "--neo4j-uri",
        default="bolt://127.0.0.1:7687",
        help="Bolt URI for the live Neo4j instance.",
    )
    run_query.add_argument(
        "--neo4j-user",
        default="neo4j",
        help="Neo4j username.",
    )
    run_query.add_argument(
        "--neo4j-password",
        default="omgs-nccn-dev",
        help="Neo4j password.",
    )
 
    return parser


def main() -> None:
    args = build_parser().parse_args()

    if args.command == "phase1":
        if args.phase1_command == "init-layout":
            result = {key: str(value) for key, value in ensure_ov_2025_layout().items()}
            if args.format == "json":
                print(json.dumps(result, indent=2, ensure_ascii=False))
            else:
                print("phase1 layout ready")
                for key, value in result.items():
                    print(f"{key}={value}")
            return

        if args.phase1_command == "build-page-inventory":
            result = initialize_phase1_ov_2025(Path(args.pdf))
            if args.format == "json":
                print(json.dumps(result, indent=2, ensure_ascii=False))
            else:
                print("phase1 page inventory ready")
                print(f"page_count={result['page_count']}")
                print(f"in_scope_page_count={result['in_scope_page_count']}")
                print(f"in_scope_pages={result['in_scope_pages']}")
            return

        if args.phase1_command == "build-llm-drafts":
            result = build_llm_drafts_for_pages(
                page_labels=args.pages,
                image_root=Path(args.image_root),
                inventory_path=Path(args.inventory),
                model=args.model,
            )
            if args.format == "json":
                print(json.dumps(result, indent=2, ensure_ascii=False))
            else:
                print("phase1 llm drafts ready")
                for item in result["pages"]:
                    print(
                        f"{item['page_code']} page={item['page_number']} "
                        f"status={item['status']}"
                    )
                print(f"summary_path={result['summary_path']}")
            return

        if args.phase1_command == "build-page-graph-drafts":
            result = build_page_graph_drafts_for_pages(args.pages)
            if args.format == "json":
                print(json.dumps(result, indent=2, ensure_ascii=False))
            else:
                print("phase1 page graph drafts ready")
                for item in result["pages"]:
                    print(
                        f"{item['page_code']} page={item['page_number']} "
                        f"status={item['status']} draft={item['draft_path']}"
                    )
                print(f"summary_path={result['summary_path']}")
            return

    if args.command == "phase2":
        if args.phase2_command == "build-page-semantics":
            with Spinner("Running phase2 page semantics"):
                result = build_page_semantics(
                    page_labels=args.pages,
                    input_root=Path(args.input_root),
                    pages_json_path=Path(args.pages_json),
                    model=args.model,
                    resume=not args.no_resume,
                    force_pages=args.force_page,
                )
            if args.format == "json":
                print(json.dumps(result, indent=2, ensure_ascii=False))
            else:
                print("phase2 page semantics ready")
                print(f"summary_path={result['summary_path']}")
                print(f"processed_count={result['processed_count']}")
                print(f"skipped_count={result['skipped_count']}")
                for item in result["pages"]:
                    print(f"{item['page_code']} status={item['status']}")
            return

    if args.command == "phase3":
        if args.phase3_command == "build-reviewed-global-graph":
            with Spinner("Running phase3 global stitch"):
                result = build_reviewed_global_graph(
                    input_root=Path(args.input_root),
                    stitch_map_path=Path(args.stitch_map),
                    page_filename="page_graph.typed.json",
                )
            if args.format == "json":
                print(json.dumps(result, indent=2, ensure_ascii=False))
            else:
                if result["status"] == "ok":
                    print("phase3 global graph ready")
                    print(f"graph_ir_path={result['graph_ir_path']}")
                    print(f"reviewed_graph_path={result['reviewed_graph_path']}")
                    print(f"summary_path={result['summary_path']}")
                    print(f"freeze_dir={result['freeze_dir']}")
                    print(f"page_count={result['page_count']}")
                    print(f"node_count={result['node_count']}")
                    print(f"edge_count={result['edge_count']}")
                else:
                    print("phase3 global graph failed")
                    print(f"stage={result['stage']}")
                    print(f"validation_report_path={result['validation_report_path']}")
                    if "stitch_report_path" in result:
                        print(f"stitch_report_path={result['stitch_report_path']}")
            return

    if args.command == "phase4":
        if args.phase4_command == "build-rule-graph":
            with Spinner("Running phase4 rule graph build"):
                result = build_rule_graph(
                    reviewed_graph_path=Path(args.reviewed_graph),
                )
            if args.format == "json":
                print(json.dumps(result, indent=2, ensure_ascii=False))
            else:
                if result["status"] == "ok":
                    print("phase4 rule graph ready")
                    print(f"rule_graph_path={result['rule_graph_path']}")
                    print(f"audit_path={result['audit_path']}")
                    print(f"report_path={result['report_path']}")
                    print(f"node_count={result['node_count']}")
                    print(f"edge_count={result['edge_count']}")
                else:
                    print("phase4 rule graph failed")
                    print(f"stage={result['stage']}")
                    print(f"report_path={result['report_path']}")
            return

    if args.command == "phase5":
        if args.phase5_command == "build-query-assets":
            with Spinner("Running phase5 query asset build"):
                result = build_query_assets_with_support(
                    rule_graph_path=Path(args.rule_graph),
                )
            if args.format == "json":
                print(json.dumps(result, indent=2, ensure_ascii=False))
            else:
                print("phase5 query assets ready")
                print(f"query_graph_path={result['query_graph_path']}")
                print(f"neo4j_like_path={result['neo4j_like_path']}")
                print(f"nodes_csv_path={result['nodes_csv_path']}")
                print(f"edges_csv_path={result['edges_csv_path']}")
                print(f"schema_path={result['schema_path']}")
                print(f"sample_queries_path={result['sample_queries_path']}")
                print(f"verbalisation_path={result['verbalisation_path']}")
                print(f"report_path={result['report_path']}")
                print(f"node_count={result['node_count']}")
                print(f"edge_count={result['edge_count']}")
            return
        if args.phase5_command == "build-support-assets":
            with Spinner("Running phase5 support-asset build"):
                result = build_support_assets(
                    rule_graph_path=Path(args.rule_graph),
                    native_md_path=Path(args.native_md),
                    pages_json_path=Path(args.pages_json),
                    overrides_path=Path(args.overrides),
                )
            if args.format == "json":
                print(json.dumps(result, indent=2, ensure_ascii=False))
            else:
                print("phase5 support assets ready")
                print(f"explicit_refs_path={result['explicit_refs_path']}")
                print(f"page_footnotes_path={result['page_footnotes_path']}")
                print(f"reference_assets_path={result['reference_assets_path']}")
                print(f"footnote_reference_assets_path={result['footnote_reference_assets_path']}")
                print(f"taxonomy_path={result['taxonomy_path']}")
                print(f"footnote_candidates_path={result['footnote_candidates_path']}")
                print(f"reviewed_footnote_links_path={result['reviewed_footnote_links_path']}")
                print(f"report_path={result['report_path']}")
            return
        if args.phase5_command == "build-engine-handoff-assets":
            with Spinner("Running phase5 engine handoff build"):
                result = build_engine_handoff_assets(
                    rule_graph_path=Path(args.rule_graph),
                    native_md_path=Path(args.native_md),
                    pages_json_path=Path(args.pages_json),
                    overrides_path=Path(args.overrides),
                )
            if args.format == "json":
                print(json.dumps(result, indent=2, ensure_ascii=False))
            else:
                print("phase5 engine handoff assets ready")
                print(f"query_graph_path={result['query_graph_path']}")
                print(f"schema_path={result['schema_path']}")
                print(f"sample_queries_path={result['sample_queries_path']}")
                print(f"verbalisation_path={result['verbalisation_path']}")
                print(f"reference_assets_path={result['reference_assets_path']}")
                print(f"footnote_reference_assets_path={result['footnote_reference_assets_path']}")
                print(f"reviewed_footnote_links_path={result['reviewed_footnote_links_path']}")
                print(f"raw_primary_md_path={result['raw_primary_md_path']}")
                print(f"pages_json_path={result['pages_json_path']}")
                print(f"report_path={result['report_path']}")
            return

    if args.command == "phase6":
        if args.phase6_command == "build-query-testset":
            with Spinner("Running phase6 query testset generation"):
                result = generate_query_testset(
                    page_labels=args.pages,
                    input_root=Path(args.input_root),
                    provider=args.provider,
                    model=args.model,
                    output_path=Path(args.output),
                )
            if args.format == "json":
                print(json.dumps(result, indent=2, ensure_ascii=False))
            else:
                print("phase6 query testset ready")
                print(f"provider={result['provider']}")
                print(f"model={result['model']}")
                print(f"page_count={result['page_count']}")
                print(f"query_count={result['query_count']}")
                print(f"output_path={result['output_path']}")
            return
        if args.phase6_command == "run-live-query":
            question = args.question
            case_meta = None
            if args.case_id is not None:
                case_meta = resolve_question_from_case(
                    case_file=Path(args.query_case_file),
                    case_id=args.case_id,
                    prefer_language=args.case_language,
                )
                question = case_meta["question"]
            if not question:
                raise ValueError("Either --question or --case-id must be provided.")
            with Spinner("Running phase6 live retrieval"):
                result = run_live_query(
                    question=question,
                    query_case=case_meta,
                    provider=args.provider,
                    model=args.model,
                    neo4j_uri=args.neo4j_uri,
                    neo4j_user=args.neo4j_user,
                    neo4j_password=args.neo4j_password,
                )
            if args.format == "json":
                print(json.dumps(result, indent=2, ensure_ascii=False))
            else:
                _print_phase5_live_query_text(result)
            return

    summary = build_bootstrap_summary()

    if args.format == "json":
        print(json.dumps(summary.__dict__, indent=2, ensure_ascii=False))
        return

    print("omgs-nccn bootstrap-ready")
    print(f"data_dir={summary.repo_data_dir}")
    print(f"tmp_dir={summary.repo_tmp_dir}")


if __name__ == "__main__":
    main()
