from __future__ import annotations

from dataclasses import asdict
from dataclasses import dataclass
from dataclasses import field
from typing import Any


@dataclass(frozen=True)
class PageDimensions:
    width: float
    height: float


@dataclass(frozen=True)
class PageAsset:
    document_id: str
    page_index: int
    page_label: str | None
    image_path: str | None
    embedded_text: str
    extracted_text: str
    page_dimensions: PageDimensions | None
    source_pdf_path: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PageInventoryRecord:
    document_id: str
    page_index: int
    page_label: str | None
    page_type: str
    in_scope: bool
    inclusion_reason: str
    source_pdf_path: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PageNode:
    page_node_id: str
    bbox: list[float]
    text: str
    node_type: str
    source_span: str | None = None


@dataclass(frozen=True)
class PageEdge:
    page_edge_id: str
    source_page_node_id: str
    target_page_node_id: str
    edge_label: str | None
    provenance: str


@dataclass(frozen=True)
class JumpRef:
    jump_ref_id: str
    source_page_node_id: str | None
    raw_label: str
    normalized_label_candidate: str | None
    resolution_status: str


@dataclass(frozen=True)
class PageParse:
    document_id: str
    page_index: int
    page_label: str | None
    page_type: str
    parse_status: str
    failure_reason: str | None
    model_name: str | None
    model_run_id: str | None
    nodes: list[PageNode] = field(default_factory=list)
    edges: list[PageEdge] = field(default_factory=list)
    jump_refs: list[JumpRef] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CrossPageRef:
    jump_ref_id: str
    raw_label: str
    normalized_label: str | None
    target_page_label: str | None
    target_page_index: int | None
    target_node_id: str | None
    resolution_status: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ReviewRecord:
    review_target_id: str
    review_target_type: str
    status: str
    reviewer: str
    timestamp: str
    notes: str
    correction_refs: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ReviewCorrection:
    correction_id: str
    target_object_type: str
    target_object_id: str
    operation: str
    payload: dict[str, Any]
    reviewer: str
    timestamp: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

