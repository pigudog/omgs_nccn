from dataclasses import dataclass

from omgs_nccn.config.paths import DATA_DIR, TMP_DIR


@dataclass(frozen=True)
class BootstrapSummary:
    repo_data_dir: str
    repo_tmp_dir: str
    status: str = "bootstrap-ready"


def build_bootstrap_summary() -> BootstrapSummary:
    return BootstrapSummary(
        repo_data_dir=str(DATA_DIR),
        repo_tmp_dir=str(TMP_DIR),
    )

