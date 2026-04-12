from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
DATA_DIR = REPO_ROOT / "data"
TMP_DIR = REPO_ROOT / "tmp"
RAW_DIR = DATA_DIR / "raw"
PROCESSED_DIR = DATA_DIR / "processed"


def ov_2025_roots() -> dict[str, Path]:
    raw_root = RAW_DIR / "ov_2025"
    processed_root = PROCESSED_DIR / "ov_2025"
    tmp_root = TMP_DIR / "ov_2025"
    qc_root = tmp_root / "qc"
    fixtures_root = tmp_root / "fixtures"
    return {
        "raw_root": raw_root,
        "page_assets": raw_root / "page_assets",
        "processed_root": processed_root,
        "page_parse_ir": processed_root / "page_parse_ir",
        "reference_resolution": processed_root / "reference_resolution",
        "graph_ir": processed_root / "graph_ir",
        "reviewed_graph": processed_root / "reviewed_graph",
        "rule_graph": processed_root / "rule_graph",
        "adu": processed_root / "adu",
        "markdown": processed_root / "markdown",
        "tmp_root": tmp_root,
        "qc_root": qc_root,
        "review_packs": qc_root / "review_packs",
        "reports": qc_root / "reports",
        "live_runs": tmp_root / "live_runs",
        "freeze_root": tmp_root / "freeze",
        "fixtures": fixtures_root,
    }
