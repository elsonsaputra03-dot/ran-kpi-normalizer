"""Pipeline DuckDB: ekspor vendor -> counter seragam per cell per jam -> KPI di level mana pun, plus laporan data quality.

Langkah per vendor:
 1. baca CSV apa adanya (nama kolom vendor, termasuk titik dan tanda hubung),
 2. buang baris duplikat (cell + waktu sama, mis. file terkirim ulang) dan hitung jumlahnya,
 3. waktu lokal -> UTC, nama cell vendor -> cell_id lewat inventory,
 4. counter negatif (reset) -> NULL untuk counter itu saja, dicatat,
 5. hitung counter seragam dari ekspresi mapping (SQL aman dari spec.to_sql),
 6. ringkas ke jam: counter kumulatif dijumlah, PRB dirata-rata berbobot detik, kelengkapan = detik tercakup / 3600.
KPI dihitung sebagai SUM(num) / SUM(den) di level yang diminta, tidak pernah sebagai rata-rata rasio.
"""
from __future__ import annotations

import json
from pathlib import Path

import duckdb

from . import spec as spec_mod

CUM = None  # diisi dari spesifikasi: counter bertipe CC


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def load_vendor(con, sp, vendor: str, path: Path, dq: dict) -> None:
    m, ex = sp.mappings[vendor], sp.mappings[vendor]["export"]
    tcol, ccol = ex["time_column"], ex["cell_column"]
    # kolom waktu & cell dibaca sebagai teks: format waktu ditentukan mapping vendor, bukan tebakan otomatis DuckDB
    con.execute(f"CREATE OR REPLACE TABLE raw_{vendor} AS SELECT * FROM read_csv_auto(?, header=true, types=?)",
                [str(path), {tcol: "VARCHAR", ccol: "VARCHAR"}])
    cols = {r[0] for r in con.execute(f"DESCRIBE raw_{vendor}").fetchall()}
    n_raw = con.execute(f"SELECT count(*) FROM raw_{vendor}").fetchone()[0]
    # 2) duplikat: pertahankan satu baris per (cell, waktu)
    con.execute(f"""CREATE OR REPLACE TABLE dedup_{vendor} AS
        SELECT * EXCLUDE (_rn) FROM (SELECT *, row_number() OVER (PARTITION BY {_q(ccol)}, {_q(tcol)}) AS _rn FROM raw_{vendor}) WHERE _rn = 1""")
    n = con.execute(f"SELECT count(*) FROM dedup_{vendor}").fetchone()[0]
    exprs, used_all, unavailable = {}, set(), []
    for canon, d in m["counters"].items():
        if not d.get("expr"):
            unavailable.append({"counter": canon, "status": d.get("status"), "note": d.get("note", "")}); continue
        try:
            sql, used = spec_mod.to_sql(d["expr"], known=cols)
        except spec_mod.SpecError as e:
            unavailable.append({"counter": canon, "status": "missing_in_export", "note": str(e)}); continue
        exprs[canon], used_all = sql, used_all | used
    # 4) counter reset: nilai negatif pada counter vendor yang dipakai -> NULL (hanya counter itu)
    resets = {}
    for c in sorted(used_all):
        k = con.execute(f"SELECT count(*) FROM dedup_{vendor} WHERE {_q(c)} < 0").fetchone()[0]
        if k:
            resets[c] = k
    clean = ", ".join(f"CASE WHEN {_q(c)} < 0 THEN NULL ELSE {_q(c)} END AS {_q(c)}" for c in sorted(used_all))
    canon_cols = ", ".join(f"{sql} AS {_q(k)}" for k, sql in exprs.items())
    period = int(ex["granularity_minutes"]) * 60
    con.execute(f"""CREATE OR REPLACE TABLE norm_{vendor} AS
        WITH c AS (SELECT {_q(ccol)} AS vcell, {_q(tcol)} AS vtime, {clean} FROM dedup_{vendor})
        SELECT i.cell_id, i.site_id, '{vendor}' AS vendor,
               strptime(CAST(c.vtime AS VARCHAR), '{ex["time_format"]}') - INTERVAL '{int(ex["timezone_offset_hours"])} hours' AS ts_utc,
               {period} AS period_s, {canon_cols}
        FROM c LEFT JOIN inventory i ON i.vendor = '{vendor}' AND i.vendor_cell_name = c.vcell""")
    unmatched = con.execute(f"SELECT count(*) FROM norm_{vendor} WHERE cell_id IS NULL").fetchone()[0]
    dq[vendor] = {"rows_raw": n_raw, "duplicates_removed": n_raw - n, "negative_counter_values_nulled": resets,
                  "unmatched_cells": unmatched, "unavailable_counters": unavailable, "granularity_minutes": ex["granularity_minutes"]}


def unify(con, sp, vendors: list[str]) -> None:
    """Union semua vendor (kolom yang tidak dipetakan = NULL) lalu ringkas ke jam."""
    canon = [c for c in sp.counters if c != "period_s"]
    parts = []
    for v in vendors:
        have = {r[0] for r in con.execute(f"DESCRIBE norm_{v}").fetchall()}
        sel = ", ".join(f"{_q(c)}" if c in have else f"CAST(NULL AS DOUBLE) AS {_q(c)}" for c in canon)
        parts.append(f"SELECT cell_id, site_id, vendor, ts_utc, period_s, {sel} FROM norm_{v} WHERE cell_id IS NOT NULL")
    con.execute("CREATE OR REPLACE TABLE rop AS " + " UNION ALL ".join(parts))
    aggs = []
    for c in canon:
        if sp.counters[c]["type"] == "CC":
            aggs.append(f"sum({_q(c)}) AS {_q(c)}")
        else:   # SI/GAUGE (PRB %): rata-rata berbobot detik, hanya dari periode yang punya nilai
            aggs.append(f"sum({_q(c)} * period_s) / NULLIF(sum(CASE WHEN {_q(c)} IS NOT NULL THEN period_s END), 0) AS {_q(c)}")
    # Per KPI: pembilang dan penyebut dijumlah HANYA dari ROP yang keduanya valid. Tanpa ini, counter yang di-NULL-kan karena reset
    # menghapus satu sisi rasio dan diam-diam menurunkan KPI (terlihat di dashboard: RRC SR Huawei "turun" ke 97,5% pada jam reset).
    for k, (n, d) in kpi_parts(sp).items():
        aggs.append(f"sum(CASE WHEN ({n}) IS NOT NULL AND ({d}) IS NOT NULL THEN ({n}) END) AS {_q(k + '__num')}")
        aggs.append(f"sum(CASE WHEN ({n}) IS NOT NULL AND ({d}) IS NOT NULL THEN ({d}) END) AS {_q(k + '__den')}")
    con.execute(f"""CREATE OR REPLACE TABLE hourly AS
        SELECT cell_id, site_id, vendor, date_trunc('hour', ts_utc) AS hour, count(*) AS rops, sum(period_s) AS period_s,
               sum(period_s) / 3600.0 AS completeness, {", ".join(aggs)}
        FROM rop GROUP BY ALL""")


def kpi_parts(sp) -> dict[str, tuple[str, str]]:
    """SQL baris-ROP untuk pembilang & penyebut tiap KPI rasio sederhana (KPI produk dihitung dari komponennya)."""
    out = {}
    for k, d in sp.kpis.items():
        if "product_of" in d:
            continue
        side = lambda v: " + ".join(_q(x) for x in (v if isinstance(v, list) else [v]))
        num = spec_mod.to_sql(d["num_expr"])[0] if d.get("num_expr") else side(d["num"])
        out[k] = (num, side(d["den"]))
    return out


def kpi_sql(sp, name: str) -> str:
    k = sp.kpis[name]
    if "product_of" in k:
        parts = [f"({kpi_sql(sp, p)} / 100.0)" for p in k["product_of"]]
        return "(" + " * ".join(parts) + " * 100.0)"
    return f"sum({_q(name + '__num')}) / NULLIF(sum({_q(name + '__den')}), 0) * {float(k['scale'])}"


def kpis(con, sp, level: str, period: str = "day") -> list[dict]:
    """level: cell | site | vendor | network. period: hour | day."""
    keys = {"cell": "vendor, site_id, cell_id", "site": "vendor, site_id", "vendor": "vendor", "network": "'all' AS network"}[level]
    t = "hour" if period == "hour" else "CAST(hour AS DATE)"
    cols = ", ".join(f"{kpi_sql(sp, k)} AS {_q(k)}" for k in sp.kpis)
    group = {"cell": "vendor, site_id, cell_id", "site": "vendor, site_id", "vendor": "vendor", "network": "1"}[level]
    rows = con.execute(f"SELECT {keys}, {t} AS period, sum(rops) AS rops, avg(completeness) AS completeness, {cols} "
                       f"FROM hourly GROUP BY {group}, {t} ORDER BY ALL").fetchdf()
    return rows


def dq_checks(con, dq: dict) -> None:
    """Pemeriksaan logis setelah normalisasi: sukses > attempt, ROP yang hilang."""
    pairs = [("RRC.ConnEstabSucc.sum", "RRC.ConnEstabAtt.sum"), ("S1SIG.ConnEstabSucc", "S1SIG.ConnEstabAtt"),
             ("ERAB.EstabInitSuccNbr.sum", "ERAB.EstabInitAttNbr.sum"), ("HO.ExeSucc", "HO.ExeAtt")]
    for v in dq:
        viol = {}
        for s, a in pairs:
            k = con.execute(f"SELECT count(*) FROM rop WHERE vendor = ? AND {_q(s)} > {_q(a)}", [v]).fetchone()[0]
            if k:
                viol[f"{s} > {a}"] = k
        dq[v]["succ_greater_than_att_rops"] = viol
        dq[v]["incomplete_cell_hours"] = con.execute("SELECT count(*) FROM hourly WHERE vendor = ? AND completeness < 1", [v]).fetchone()[0]
        # ROP yang seharusnya ada vs yang diterima. Untuk ekspor per jam, jam yang hilang tidak meninggalkan baris "tidak lengkap",
        # jadi kelengkapan saja tidak cukup (celah ditemukan saat data Huawei ditampilkan di dashboard).
        gran = dq[v]["granularity_minutes"]
        exp, got = con.execute(f"""SELECT (SELECT count(*) FROM inventory WHERE vendor = ?) *
                                          (CAST(epoch(max(ts_utc)) - epoch(min(ts_utc)) AS BIGINT) / {gran * 60} + 1), count(*)
                                   FROM rop WHERE vendor = ?""", [v, v]).fetchone()
        dq[v]["expected_rops"], dq[v]["missing_rops"] = int(exp or 0), int((exp or 0) - got)


def run(data: Path, out: Path, root: Path = spec_mod.ROOT) -> dict:
    sp = spec_mod.load(root)
    out.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute("CREATE TABLE inventory AS SELECT * FROM read_csv_auto(?, header=true)", [str(data / "inventory.csv")])
    dq, done = {}, []
    for v in sp.mappings:
        f = data / f"{v}_lte.csv"
        if v not in sp.ready_vendors():
            reason = "export format not defined" if sp.mapped(v) else "mapping not filled in (all counters to_verify)"
            dq[v] = {"skipped": f"{reason}; {len(sp.mapped(v))} of {len(sp.mappings[v]['counters'])} counters mapped"}; continue
        if not f.exists():
            dq[v] = {"skipped": f"no export file {f.name}"}; continue
        load_vendor(con, sp, v, f, dq); done.append(v)
    unify(con, sp, done)
    dq_checks(con, {v: dq[v] for v in done})
    result = {"vendors": done, "dq": dq}
    for level in ("cell", "site", "vendor", "network"):
        df = kpis(con, sp, level)
        df.to_csv(out / f"kpi_{level}_day.csv", index=False)
    kpis(con, sp, "vendor", "hour").to_csv(out / "kpi_vendor_hour.csv", index=False)
    con.execute(f"COPY hourly TO '{out / 'unified_hourly.parquet'}' (FORMAT parquet)")
    # KPI yang tidak bisa dihitung untuk suatu vendor karena counter tidak terpetakan: dilaporkan, bukan ditebak
    coverage = {}
    for v in done:
        missing = {c["counter"] for c in dq[v]["unavailable_counters"]}
        coverage[v] = {k: ("ok" if not (sp.required_by(k) & missing) else "unavailable: needs " + ", ".join(sorted(sp.required_by(k) & missing)))
                       for k in sp.kpis}
    result["kpi_coverage"] = coverage
    (out / "dq.json").write_text(json.dumps(result, indent=1, default=str))
    result["con"] = con
    return result


def publish(res: dict, data: Path, path: Path, root: Path = spec_mod.ROOT) -> dict:
    """Satu file JSON ringkas untuk dashboard: spesifikasi, mapping, cakupan KPI, KPI vendor (hari & jam), KPI site (hari), DQ."""
    import math
    from datetime import datetime, timezone
    sp, con = spec_mod.load(root), res["con"]

    def rows(df):
        out = []
        for r in df.to_dict("records"):
            out.append({k: (None if isinstance(v, float) and (math.isnan(v) or math.isinf(v)) else (str(v) if hasattr(v, "isoformat") else v))
                        for k, v in r.items()})
        return out

    manifest = json.loads((data / "manifest.json").read_text()) if (data / "manifest.json").exists() else {}
    doc = {
        "meta": {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "days": manifest.get("days"),
                 "cells": manifest.get("cells"), "vendors_run": res["vendors"],
                 "sources": ["3GPP TS 32.425 (ETSI TS 132 425 V14.1.0)", "3GPP TS 32.450 (ETSI TS 132 450 V17.0.0)"]},
        "counters": sp.counters,
        "kpis": {k: {f: v.get(f) for f in ("name", "standard", "clause", "note", "unit", "num", "den", "num_expr", "product_of", "scale")}
                 for k, v in sp.kpis.items()},
        "mappings": {v: {"export": m["export"], "counters": m["counters"]} for v, m in sp.mappings.items()},
        "kpi_coverage": res["kpi_coverage"],
        "vendor_day": rows(kpis(con, sp, "vendor", "day")),
        "vendor_hour": rows(kpis(con, sp, "vendor", "hour")),
        "site_day": rows(kpis(con, sp, "site", "day")),
        "dq": {v: {k: x for k, x in d.items()} for v, d in res["dq"].items()},
        "injected": {v: {"dropped_rops": len(m["dropped"]), "duplicates": m["duplicates"], "resets": len(m["reset"]),
                         "succ_gt_att": len(m["succ_gt_att"])} for v, m in manifest.items() if isinstance(m, dict) and "dropped" in m},
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, separators=(",", ":"), default=str))
    return doc
