"""ranorm: generate | run | coverage."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import generate, pipeline, spec


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="ranorm", description="Multi-vendor LTE counters -> 3GPP TS 32.425 measurements -> TS 32.450 KPIs")
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("generate", help="synthetic vendor exports with field quirks and a ground-truth manifest")
    g.add_argument("--out", default="data"); g.add_argument("--days", type=int, default=2); g.add_argument("--sites", type=int, default=8)
    g.add_argument("--seed", type=int, default=7)
    r = sub.add_parser("run", help="normalize exports and compute KPIs")
    r.add_argument("--data", default="data"); r.add_argument("--out", default="out")
    sub.add_parser("coverage", help="mapping status per vendor and which KPIs each vendor can produce")
    a = ap.parse_args(argv)
    if a.cmd == "generate":
        m = generate.generate(Path(a.out), a.days, a.sites, a.seed)
        print(f"ok: {m['cells']} cells x {m['days']} days -> {a.out}/ (ericsson 15-min UTC, huawei 60-min local time)"); return 0
    if a.cmd == "coverage":
        sp = spec.load()
        for v, m in sp.mappings.items():
            st = {}
            for d in m["counters"].values():
                st[d["status"]] = st.get(d["status"], 0) + 1
            missing = {c for c, d in m["counters"].items() if not d.get("expr")}
            ok = [k for k in sp.kpis if not (sp.required_by(k) & missing)]
            print(f"{v:9s} counters {st} | KPIs available: {len(ok)}/{len(sp.kpis)}")
        return 0
    res = pipeline.run(Path(a.data), Path(a.out))
    for v, d in res["dq"].items():
        if "skipped" in d:
            print(f"{v:9s} skipped: {d['skipped']}"); continue
        print(f"{v:9s} rows {d['rows_raw']}, duplicates {d['duplicates_removed']}, resets {sum(d['negative_counter_values_nulled'].values())}, "
              f"succ>att {sum(d['succ_greater_than_att_rops'].values())}, incomplete cell-hours {d['incomplete_cell_hours']}")
    print(f"outputs -> {a.out}/ (kpi_*_day.csv, kpi_vendor_hour.csv, unified_hourly.parquet, dq.json)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
