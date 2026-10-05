from __future__ import annotations

from pathlib import Path
import pandas as pd


def _normalize(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy().reindex(sorted(df.columns), axis=1)
    for col in out.columns:
        if out[col].dtype == object:
            out[col] = out[col].fillna("").astype(str)
    if len(out.columns):
        out = out.sort_values(list(out.columns), kind="mergesort").reset_index(drop=True)
    return out


def validate_against_v5(reference_dir: Path | None, output_dir: Path) -> list[dict]:
    checks = []
    for label, v5_name, v6_name in [
        ("final_candidates", "V5_final_candidates.csv", "V6_final_candidates.csv"),
        ("direct_eoa", "V5_direct_eoa_candidates.csv", "V6_direct_eoa_candidates.csv"),
    ]:
        if reference_dir is None:
            checks.append({"check": label, "status": "NO_REFERENCE"})
            continue
        p5, p6 = reference_dir / v5_name, output_dir / v6_name
        if not p5.is_file() or not p6.is_file():
            checks.append({"check": label, "status": "MISSING", "v5": str(p5), "v6": str(p6)})
            continue
        d5, d6 = pd.read_csv(p5), pd.read_csv(p6)
        same_cols = set(d5.columns) == set(d6.columns)
        same_data = same_cols and _normalize(d5).equals(_normalize(d6))
        checks.append({
            "check": label,
            "status": "MATCH" if same_data else "DIFF",
            "rows_v5": len(d5), "rows_v6": len(d6), "same_columns": bool(same_cols),
        })
    return checks
