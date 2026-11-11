from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
import csv

from .storage import write_rows_csv
from .utils import safe_float, utc_now


@dataclass
class CalibrationBucket:
    bucket: str
    rows: int
    avg_predicted_prob: float
    observed_win_rate: float
    calibration_error: float
    avg_market_price: float
    avg_net_edge: float
    pnl_per_share_sum: float
    roi_on_cost: float


def calibrate_probability_dataset(*, snapshots_path: Path, out_dir: Path) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = load_rows(snapshots_path)
    rows = [
        row for row in rows
        if row.get("fair_prob") not in ("", None)
        and row.get("actual_win") in {"True", "False", True, False}
    ]
    probability_rows = summarize_groups(rows, group_probability_decile)
    edge_rows = summarize_groups(rows, group_edge_bucket)
    time_price_rows = summarize_groups(rows, group_time_price_bucket)
    write_rows_csv(out_dir / "calibration_probability_deciles.csv", [asdict(row) for row in probability_rows])
    write_rows_csv(out_dir / "calibration_edge_buckets.csv", [asdict(row) for row in edge_rows])
    write_rows_csv(out_dir / "calibration_time_price_buckets.csv", [asdict(row) for row in time_price_rows])
    report_path = out_dir / "calibration_report.md"
    write_report(report_path, snapshots_path, rows, probability_rows, edge_rows, time_price_rows)
    return {
        "rows": len(rows),
        "report": str(report_path),
        "probability_deciles": str(out_dir / "calibration_probability_deciles.csv"),
        "edge_buckets": str(out_dir / "calibration_edge_buckets.csv"),
        "time_price_buckets": str(out_dir / "calibration_time_price_buckets.csv"),
    }


def load_rows(path: Path) -> list[dict[str, Any]]:
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def summarize_groups(rows: list[dict[str, Any]], key_fn) -> list[CalibrationBucket]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        key = key_fn(row)
        if key:
            groups.setdefault(key, []).append(row)
    return [summarize_bucket(key, value) for key, value in sorted(groups.items())]


def summarize_bucket(bucket: str, rows: list[dict[str, Any]]) -> CalibrationBucket:
    predicted = [safe_float(row.get("fair_prob"), 0.0) or 0.0 for row in rows]
    wins = [1.0 if row.get("actual_win") in {"True", True} else 0.0 for row in rows]
    prices = [safe_float(row.get("polymarket_price"), 0.0) or 0.0 for row in rows]
    edges = [safe_float(row.get("net_edge"), 0.0) or 0.0 for row in rows]
    pnls = [safe_float(row.get("pnl_per_share"), 0.0) or 0.0 for row in rows]
    cost = sum(prices)
    avg_pred = sum(predicted) / len(predicted)
    winrate = sum(wins) / len(wins)
    return CalibrationBucket(
        bucket=bucket,
        rows=len(rows),
        avg_predicted_prob=avg_pred,
        observed_win_rate=winrate,
        calibration_error=winrate - avg_pred,
        avg_market_price=sum(prices) / len(prices),
        avg_net_edge=sum(edges) / len(edges),
        pnl_per_share_sum=sum(pnls),
        roi_on_cost=sum(pnls) / cost if cost > 0 else 0.0,
    )


def group_probability_decile(row: dict[str, Any]) -> str:
    prob = safe_float(row.get("fair_prob"))
    return probability_decile_bucket(prob)


def group_edge_bucket(row: dict[str, Any]) -> str:
    edge = safe_float(row.get("net_edge"))
    return edge_bucket(edge)


def group_time_price_bucket(row: dict[str, Any]) -> str:
    tte = safe_float(row.get("seconds_to_expiry"))
    price = safe_float(row.get("polymarket_price"))
    return time_price_bucket(tte, price)


def probability_decile_bucket(prob: float | None) -> str:
    if prob is None:
        return ""
    prob = max(0.0, min(0.999999, prob))
    bucket = min(int(prob * 10), 9)
    return f"p{bucket / 10:.1f}-{(bucket + 1) / 10:.1f}"


def edge_bucket(edge: float | None) -> str:
    if edge is None:
        return ""
    if edge < 0:
        return "edge<0"
    if edge < 0.01:
        return "edge 0-1%"
    if edge < 0.02:
        return "edge 1-2%"
    if edge < 0.05:
        return "edge 2-5%"
    if edge < 0.10:
        return "edge 5-10%"
    return "edge >=10%"


def time_price_bucket(tte: float | None, price: float | None) -> str:
    if tte is None or price is None:
        return ""
    if tte <= 60:
        time_bucket = "tte<=60"
    elif tte <= 120:
        time_bucket = "tte<=120"
    elif tte <= 300:
        time_bucket = "tte<=300"
    elif tte <= 900:
        time_bucket = "tte<=900"
    else:
        time_bucket = "tte>900"
    if price <= 0.05:
        price_bucket = "px<=5c"
    elif price <= 0.25:
        price_bucket = "px<=25c"
    elif price <= 0.60:
        price_bucket = "px<=60c"
    elif price < 0.95:
        price_bucket = "px<95c"
    else:
        price_bucket = "px>=95c"
    return f"{time_bucket}|{price_bucket}"


def write_report(
    path: Path,
    snapshots_path: Path,
    rows: list[dict[str, Any]],
    probability_rows: list[CalibrationBucket],
    edge_rows: list[CalibrationBucket],
    time_price_rows: list[CalibrationBucket],
) -> None:
    high_edge = [row for row in rows if (safe_float(row.get("net_edge")) or -1) >= 0.01]
    high_edge_pnl = sum(safe_float(row.get("pnl_per_share"), 0.0) or 0.0 for row in high_edge)
    high_edge_cost = sum(safe_float(row.get("polymarket_price"), 0.0) or 0.0 for row in high_edge)
    lines = [
        "# Probability Calibration Report",
        "",
        f"Built at: {utc_now()}",
        f"Input: {snapshots_path}",
        f"Rows with labels and fair probabilities: {len(rows)}",
        f"Rows with net_edge >= 1%: {len(high_edge)}",
        f"Historical ROI for net_edge >= 1% snapshots: {(high_edge_pnl / high_edge_cost if high_edge_cost else 0):.4f}",
        "",
        "## Probability Deciles",
        "",
        "| bucket | rows | avg_pred | observed | observed-avg_pred | roi |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for row in probability_rows:
        lines.append(
            f"| {row.bucket} | {row.rows} | {row.avg_predicted_prob:.4f} | {row.observed_win_rate:.4f} | {row.calibration_error:.4f} | {row.roi_on_cost:.4f} |"
        )
    lines.extend(["", "## Edge Buckets", "", "| bucket | rows | avg_edge | observed | roi |", "|---|---:|---:|---:|---:|"])
    for row in edge_rows:
        lines.append(f"| {row.bucket} | {row.rows} | {row.avg_net_edge:.4f} | {row.observed_win_rate:.4f} | {row.roi_on_cost:.4f} |")
    lines.extend(["", "## Best Time/Price Buckets", "", "| bucket | rows | avg_pred | observed | roi |", "|---|---:|---:|---:|---:|"])
    for row in sorted(time_price_rows, key=lambda x: (x.roi_on_cost, x.rows), reverse=True)[:20]:
        lines.append(f"| {row.bucket} | {row.rows} | {row.avg_predicted_prob:.4f} | {row.observed_win_rate:.4f} | {row.roi_on_cost:.4f} |")
    lines.extend(
        [
            "",
            "## Interpretation",
            "",
            "- If observed win rate is materially below avg_predicted_prob, the model is overconfident.",
            "- Use buckets with enough rows and positive ROI as hypotheses for live paper filters.",
            "- Do not use this calibration alone for live execution; historical Polymarket price history is not full orderbook replay.",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")
