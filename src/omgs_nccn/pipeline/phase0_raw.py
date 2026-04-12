from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path
from typing import Any

import pymupdf

from omgs_nccn.config.paths import ov_2025_roots


OV_2025_RELEASE_STEM = "22_nccn_ovarian_cancer_v3_2025"


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _reset_dir(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)


def _default_marker_device() -> str:
    import torch

    if torch.cuda.is_available():
        return "cuda"
    if torch.backends.mps.is_available():
        return "mps"
    return "cpu"


def _build_primary_markdown_from_page_payloads(
    page_payloads: list[dict[str, object]],
) -> str:
    parts: list[str] = []
    for payload in page_payloads:
        page_number = int(payload["page_number"])
        text = str(payload.get("text", "")).strip()
        if not text:
            continue
        parts.append(f"## Page {page_number}\n\n{text}")
    return "\n\n".join(parts).strip()


def render_page_images_from_pdf(
    pdf_path: Path,
    image_root: Path,
    *,
    scale: float = 2.0,
) -> dict[str, Any]:
    _reset_dir(image_root)
    doc = pymupdf.open(str(pdf_path))
    written: list[str] = []
    try:
        matrix = pymupdf.Matrix(scale, scale)
        for page_index, page in enumerate(doc, start=1):
            image_path = image_root / f"page_{page_index:03d}.png"
            pixmap = page.get_pixmap(matrix=matrix, alpha=False)
            pixmap.save(str(image_path))
            written.append(str(image_path))
    finally:
        doc.close()
    return {
        "page_count": len(written),
        "image_root": str(image_root),
        "image_paths": written,
        "render_scale": scale,
    }


def build_native_text_sidecars(
    pdf_path: Path,
    native_raw_dir: Path,
) -> dict[str, Any]:
    _reset_dir(native_raw_dir)
    doc = pymupdf.open(str(pdf_path))
    page_payloads: list[dict[str, object]] = []
    try:
        for page_index, page in enumerate(doc, start=1):
            page_payloads.append(
                {
                    "page_number": page_index,
                    "text": page.get_text("text").strip(),
                }
            )
    finally:
        doc.close()

    primary_md = native_raw_dir / "primary.md"
    primary_md.write_text(
        _build_primary_markdown_from_page_payloads(page_payloads),
        encoding="utf-8",
    )
    pages_json = native_raw_dir / "pages.json"
    _write_json(pages_json, {"pages": page_payloads})
    return {
        "primary_artifact": str(primary_md),
        "pages_json": str(pages_json),
        "page_count": len(page_payloads),
    }


def _run_marker(
    pdf_path: Path,
    *,
    output_dir: Path,
    device: str | None = None,
    disable_ocr: bool = True,
    disable_image_extraction: bool = True,
    disable_multiprocessing: bool = False,
) -> dict[str, Any]:
    from marker.config.parser import ConfigParser
    from marker.models import create_model_dict
    from marker.output import save_output

    output_dir.mkdir(parents=True, exist_ok=True)
    cli_options = {
        "output_format": "markdown",
        "output_dir": str(output_dir),
        "disable_multiprocessing": disable_multiprocessing,
        "disable_image_extraction": disable_image_extraction,
        "disable_ocr": disable_ocr,
    }
    config_parser = ConfigParser(cli_options)
    chosen_device = device or _default_marker_device()
    models = create_model_dict(device=chosen_device)
    converter_cls = config_parser.get_converter_cls()
    converter = converter_cls(
        config=config_parser.generate_config_dict(),
        artifact_dict=models,
        processor_list=config_parser.get_processors(),
        renderer=config_parser.get_renderer(),
        llm_service=config_parser.get_llm_service(),
    )
    rendered = converter(str(pdf_path))
    out_folder = Path(config_parser.get_output_folder(str(pdf_path)))
    base_filename = config_parser.get_base_filename(str(pdf_path))
    save_output(rendered, str(out_folder), base_filename)
    primary_md = out_folder / f"{base_filename}.md"
    meta_json = out_folder / f"{base_filename}_meta.json"
    if not primary_md.exists():
        raise RuntimeError(f"Marker produced no markdown for {pdf_path.name}")
    metadata: dict[str, Any] = {}
    if meta_json.exists():
        metadata = json.loads(meta_json.read_text(encoding="utf-8"))
    return {
        "device": chosen_device,
        "output_dir": str(out_folder),
        "primary_md": str(primary_md),
        "meta_json": str(meta_json) if meta_json.exists() else None,
        "metadata": metadata,
    }


def build_marker_text_sidecars(
    pdf_path: Path,
    marker_raw_dir: Path,
    *,
    device: str | None = None,
    disable_multiprocessing: bool = False,
    marker_runner: Any | None = None,
) -> dict[str, Any]:
    _reset_dir(marker_raw_dir)
    runner = marker_runner or _run_marker
    marker_tmp = marker_raw_dir / "_marker_tmp"
    marker_result = runner(
        pdf_path,
        output_dir=marker_tmp,
        device=device,
        disable_multiprocessing=disable_multiprocessing,
    )

    primary_md_src = Path(marker_result["primary_md"])
    primary_md_dst = marker_raw_dir / "primary.md"
    primary_md_dst.write_text(
        primary_md_src.read_text(encoding="utf-8", errors="ignore"),
        encoding="utf-8",
    )

    assets_dir = marker_raw_dir / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    output_dir = Path(marker_result["output_dir"])
    for child in output_dir.iterdir():
        if child == primary_md_src:
            continue
        target = assets_dir / child.name
        if child.is_dir():
            shutil.copytree(child, target, dirs_exist_ok=True)
        else:
            shutil.copy2(child, target)

    if marker_tmp.exists():
        shutil.rmtree(marker_tmp, ignore_errors=True)

    return {
        "device": marker_result["device"],
        "primary_artifact": str(primary_md_dst),
        "assets_dir": str(assets_dir),
        "metadata": marker_result.get("metadata", {}),
    }


def build_local_nccn_inputs(
    pdf_path: Path,
    *,
    marker_device: str | None = None,
    marker_disable_multiprocessing: bool = False,
    render_scale: float = 2.0,
    marker_runner: Any | None = None,
) -> dict[str, Any]:
    from omgs_nccn.pipeline.phase1 import ensure_ov_2025_layout
    from omgs_nccn.pipeline.phase1 import initialize_phase1_ov_2025

    roots = ensure_ov_2025_layout()
    image_summary = render_page_images_from_pdf(
        pdf_path,
        roots["page_assets"],
        scale=render_scale,
    )
    inventory_summary = initialize_phase1_ov_2025(pdf_path)

    extraction_root = (
        roots["raw_root"] / "text_extraction" / OV_2025_RELEASE_STEM
    )
    _reset_dir(extraction_root)
    raw_dir = extraction_root / "raw"
    marker_raw_dir = raw_dir / "marker_native"
    native_raw_dir = raw_dir / "native"

    marker_summary = build_marker_text_sidecars(
        pdf_path,
        marker_raw_dir,
        device=marker_device,
        disable_multiprocessing=marker_disable_multiprocessing,
        marker_runner=marker_runner,
    )
    native_summary = build_native_text_sidecars(pdf_path, native_raw_dir)

    primary_md = raw_dir / "primary.md"
    primary_md.write_text(
        Path(marker_summary["primary_artifact"]).read_text(
            encoding="utf-8",
            errors="ignore",
        ),
        encoding="utf-8",
    )

    metadata = {
        "source_key": "nccn_ovarian_cancer_v3_2025",
        "release_stem": OV_2025_RELEASE_STEM,
        "release_filename": f"{OV_2025_RELEASE_STEM}.pdf",
        "input_pdf": str(pdf_path),
        "extractor": "omgs_nccn_hybrid",
        "route": "omgs_nccn_hybrid",
        "note": (
            "Repo-owned NCCN bootstrap. Use Marker native markdown as the prose base "
            "and PyMuPDF native text as the page-level sidecar."
        ),
        "subset_pdf_page_ranges": [],
        "build_mode": "repo_owned_nccn_bootstrap",
        "artifacts": {
            "mode": "hybrid_marker_native_plus_pymupdf_native",
            "primary_artifact": str(primary_md),
            "primary_source": "marker_native",
            "marker_native": marker_summary,
            "native_sidecar": native_summary,
            "page_assets": image_summary,
            "page_inventory": inventory_summary,
        },
    }
    _write_json(extraction_root / "metadata.json", metadata)

    summary = {
        "pdf_path": str(pdf_path),
        "page_count": inventory_summary["page_count"],
        "in_scope_page_count": inventory_summary["in_scope_page_count"],
        "page_assets_root": str(roots["page_assets"]),
        "page_assets_manifest": inventory_summary["page_assets_manifest"],
        "page_inventory_manifest": inventory_summary["page_inventory_manifest"],
        "marker_primary_md": marker_summary["primary_artifact"],
        "native_primary_md": native_summary["primary_artifact"],
        "pages_json": native_summary["pages_json"],
        "metadata_path": str(extraction_root / "metadata.json"),
    }
    _write_json(roots["reports"] / "phase0_local_inputs_summary.json", summary)
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m omgs_nccn.pipeline.phase0_raw",
        description="Build repo-owned NCCN local raw inputs from the licensed PDF.",
    )
    parser.add_argument(
        "--pdf",
        default="data/ref/nccn_ovarian_cancer_v3_2025.pdf",
        help="Path to the licensed NCCN PDF.",
    )
    parser.add_argument(
        "--marker-device",
        default=None,
        help="Optional Marker device override such as cuda, mps, or cpu.",
    )
    parser.add_argument(
        "--marker-disable-multiprocessing",
        action="store_true",
        help="Disable multiprocessing in the Marker bootstrap run.",
    )
    parser.add_argument(
        "--render-scale",
        type=float,
        default=2.0,
        help="Page PNG render scale used for page-level image assets.",
    )
    parser.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="Output format.",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    result = build_local_nccn_inputs(
        Path(args.pdf),
        marker_device=args.marker_device,
        marker_disable_multiprocessing=args.marker_disable_multiprocessing,
        render_scale=args.render_scale,
    )
    if args.format == "json":
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return
    print("phase0 local NCCN inputs ready")
    print(f"page_count={result['page_count']}")
    print(f"page_assets_root={result['page_assets_root']}")
    print(f"marker_primary_md={result['marker_primary_md']}")
    print(f"pages_json={result['pages_json']}")


if __name__ == "__main__":
    main()
