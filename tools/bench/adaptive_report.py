"""Summarize adaptive_ab.py raw trials; mismatched payload checksums are excluded from timing."""
import argparse
import json
from collections import defaultdict
from pathlib import Path

import numpy as np
from scipy.stats import wilcoxon

BOOT = 20_000
RNG = np.random.default_rng(20261120)


def paired_ratio_ci(values):
    x = np.asarray(values, dtype=float)
    if len(x) < 2:
        return [float(x[0]), float(x[0])] if len(x) else [None, None]
    samples = RNG.choice(x, (BOOT, len(x)), replace=True)
    med = np.median(samples, axis=1)
    return [float(np.percentile(med, 2.5)), float(np.percentile(med, 97.5))]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("run", type=Path)
    args = ap.parse_args()
    records = json.loads((args.run / "runs.json").read_text(encoding="utf-8"))
    manifest = json.loads((args.run / "environment.json").read_text(encoding="utf-8"))

    grouped = defaultdict(dict)
    for row in records:
        key = (row["scenario"], row["run"], row["warmup"])
        grouped[key][row["client"]] = row

    scenarios = defaultdict(list)
    for (sid, run, warmup), pair in grouped.items():
        if warmup:
            continue
        b, a = pair.get("baseline"), pair.get("adaptive")
        if b is None or a is None:
            scenarios[sid].append({"run": run, "valid": False, "reason": "missing paired trial"})
            continue
        if not b["ok"] or not a["ok"]:
            scenarios[sid].append({"run": run, "valid": False, "reason": "failed/incomplete transfer"})
            continue
        if not b.get("sha256") or b["sha256"] != a.get("sha256"):
            scenarios[sid].append({"run": run, "valid": False, "reason": "payload checksum mismatch"})
            continue
        ratio = b["e2e_ms"] / a["e2e_ms"] if a["e2e_ms"] else None
        scenarios[sid].append({
            "run": run, "valid": ratio is not None, "ratio_baseline_over_adaptive": ratio,
            "baseline_ms": b["e2e_ms"], "adaptive_ms": a["e2e_ms"],
            "baseline_cpu_ms": b["cpu_ms"], "adaptive_cpu_ms": a["cpu_ms"],
            "baseline_peak_ws_mb": b["peak_ws_mb"], "adaptive_peak_ws_mb": a["peak_ws_mb"],
            "sha256": b["sha256"],
        })

    lines = [
        "# DownloadX adaptive A/B benchmark", "",
        "Date: {}".format(manifest["date"]),
        "Machine: {}; {} GB RAM; {}".format(manifest["cpu"], manifest["ram_gb"], manifest["os"]),
        "Baseline executable SHA-256: {}".format(manifest["baseline_sha256"]),
        "Adaptive executable SHA-256: {}".format(manifest["adaptive_sha256"]), "",
        "Ratio is baseline completion time / adaptive completion time; above 1.0 means the adaptive build was faster.",
        "Only successful paired transfers with identical SHA-256 payloads are included in timing statistics.", "",
        "| Scenario | Valid pairs | Excluded | Baseline median ms | Adaptive median ms | Ratio (95% CI) | Wilcoxon p |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    output = {"environment": manifest, "scenarios": []}
    for sid in sorted(scenarios):
        rows = scenarios[sid]
        valid = [r for r in rows if r["valid"]]
        excluded = len(rows) - len(valid)
        b = [r["baseline_ms"] for r in valid]
        a = [r["adaptive_ms"] for r in valid]
        ratios = [r["ratio_baseline_over_adaptive"] for r in valid]
        if valid:
            p = float(wilcoxon(b, a, alternative="two-sided").pvalue) if len(valid) >= 2 and any(x != y for x, y in zip(b, a)) else (1.0 if len(valid) >= 2 else None)
            bmed, amed = float(np.median(b)), float(np.median(a))
            ratio_med = float(np.median(ratios))
            ci = paired_ratio_ci(ratios)
            ratio_text = "{:.3f} ({:.3f}–{:.3f})".format(ratio_med, ci[0], ci[1])
            p_text = "{:.4g}".format(p) if p is not None else "—"
            lines.append("| {} | {} | {} | {:.1f} | {:.1f} | {} | {} |".format(
                sid, len(valid), excluded, bmed, amed, ratio_text, p_text))
        else:
            bmed = amed = ratio_med = p = None
            ci = [None, None]
            lines.append("| {} | 0 | {} | — | — | — | — |".format(sid))
        output["scenarios"].append({
            "id": sid, "valid_pairs": len(valid), "excluded_pairs": excluded,
            "baseline_median_ms": bmed, "adaptive_median_ms": amed,
            "ratio_baseline_over_adaptive": ratio_med, "ratio_ci95": ci,
            "mann_whitney_p": p, "trials": rows,
        })

    report = "\n".join(lines) + "\n"
    (args.run / "REPORT.md").write_text(report, encoding="utf-8")
    (args.run / "analysis.json").write_text(json.dumps(output, indent=2), encoding="utf-8")
    print(report)


if __name__ == "__main__":
    main()
