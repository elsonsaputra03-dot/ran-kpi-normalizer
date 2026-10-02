"""Test: spesifikasi, keamanan ekspresi, round-trip kebenaran -> ekspor vendor -> pipeline, agregasi KPI, dan data quality."""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

from ranorm import generate, pipeline, spec

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def run(tmp_path_factory):
    data, out = tmp_path_factory.mktemp("data"), tmp_path_factory.mktemp("out")
    manifest = generate.generate(data, days=1, sites_per_vendor=3, seed=11)
    res = pipeline.run(data, out)
    truth = pd.DataFrame(json.loads((data / "truth.json").read_text()))
    truth["ts"] = pd.to_datetime(truth["ts"]).dt.tz_localize(None)
    return {"data": data, "out": out, "manifest": manifest, "res": res, "truth": truth, "con": res["con"]}


# --- spesifikasi & keamanan ekspresi -----------------------------------------------------------------------------
def test_spec_consistent():
    sp = spec.load(ROOT)
    assert set(sp.ready_vendors()) == {"ericsson", "huawei"}
    assert sp.required_by("erab_accessibility") == {"RRC.ConnEstabAtt.sum", "RRC.ConnEstabSucc.sum", "S1SIG.ConnEstabAtt",
                                                    "S1SIG.ConnEstabSucc", "ERAB.EstabInitAttNbr.sum", "ERAB.EstabInitSuccNbr.sum"}


@pytest.mark.parametrize("expr,sql", [
    ("L.E-RAB.AbnormRel + L.E-RAB.NormRel", '("L.E-RAB.AbnormRel" + "L.E-RAB.NormRel")'),
    ("100 * a / b", '((100 * "a") / NULLIF("b", 0))'),
    ("(x - y) / 1000", '((("x" - "y")) / NULLIF(1000, 0))'),
])
def test_expression_to_sql(expr, sql):
    assert spec.to_sql(expr)[0] == sql


@pytest.mark.parametrize("bad", ["pmA-pmB", "a + (b", "a; DROP TABLE t", "a ** b", "exec('x')"])
def test_expression_rejects_unsafe_or_ambiguous(bad):
    with pytest.raises(spec.SpecError):
        spec.to_sql(bad, known={"a", "b", "x", "y"})


# --- round trip: kebenaran harus kembali utuh setelah format vendor, satuan, zona waktu dan kerumitan lapangan -------
def _key_to_utc(vendor: str, cell_native: str, t: str) -> tuple:
    inv = {"ericsson": lambda c: c.removeprefix("ERBS_"), "huawei": lambda c: c.replace("-", "_")}[vendor](cell_native)
    ts = datetime.strptime(t, "%Y-%m-%d %H:%M" if vendor == "ericsson" else "%Y-%m-%d %H:%M:%S")
    return inv, ts - timedelta(hours=0 if vendor == "ericsson" else 7)


def expected_hourly(run, vendor: str) -> pd.DataFrame:
    m, t = run["manifest"][vendor], run["truth"]
    t = t[t["vendor"] == vendor].copy()
    t["hour"] = t["ts"].dt.floor("h")
    gran = "ts" if vendor == "ericsson" else "hour"
    dropped = {_key_to_utc(vendor, c, x) for c, x in m["dropped"]}
    t = t[[(c, k) not in dropped for c, k in zip(t["cell_id"], t[gran])]]
    bad = {_key_to_utc(vendor, c, x) for c, x in m["reset"] + m["succ_gt_att"]}   # baris yang sengaja dirusak
    t["_bad"] = [(c, k) in bad for c, k in zip(t["cell_id"], t[gran])]
    return t


CC = ["RRC.ConnEstabAtt.sum", "S1SIG.ConnEstabSucc", "ERAB.EstabInitSuccNbr.sum", "ERAB.RelActNbr.sum", "ERAB.RelAttNbr.sum",
      "DRB.IPVolDl.sum", "DRB.IPTimeDl.sum", "DRB.IPVolUl.sum", "DRB.IPTimeUl.sum", "RRU.CellUnavailableTime.sum"]


@pytest.mark.parametrize("vendor", ["ericsson", "huawei"])
def test_round_trip_counters(run, vendor):
    exp = expected_hourly(run, vendor).groupby(["cell_id", "hour"])[CC].sum()
    got = run["con"].execute("SELECT * FROM hourly WHERE vendor = ?", [vendor]).fetchdf()
    got["hour"] = pd.to_datetime(got["hour"]); got = got.set_index(["cell_id", "hour"])[CC]
    assert len(got) == len(exp)
    pd.testing.assert_frame_equal(got.sort_index().astype(float), exp.sort_index().astype(float), check_exact=False, rtol=1e-9)


@pytest.mark.parametrize("vendor", ["ericsson", "huawei"])
def test_round_trip_rrc_success_excludes_only_damaged_rops(run, vendor):
    t = expected_hourly(run, vendor)
    exp = t[~t["_bad"]]["RRC.ConnEstabSucc.sum"].sum() + 0
    got_all = run["con"].execute("SELECT sum(\"RRC.ConnEstabSucc.sum\") FROM rop WHERE vendor = ?", [vendor]).fetchone()[0]
    over = run["con"].execute('SELECT sum("RRC.ConnEstabSucc.sum") FROM rop WHERE vendor = ? AND "RRC.ConnEstabSucc.sum" > "RRC.ConnEstabAtt.sum"', [vendor]).fetchone()[0]
    assert got_all - over == pytest.approx(exp)        # reset -> NULL (tidak terhitung); anomali sukses > attempt dilaporkan terpisah


def test_prb_time_weighted_not_summed(run):
    t = expected_hourly(run, "ericsson")
    h = t.groupby(["cell_id", "hour"]).apply(lambda g: (g["RRU.PrbTotDl"] * g["period_s"]).sum() / g["period_s"].sum(), include_groups=False)
    got = run["con"].execute('SELECT cell_id, hour, "RRU.PrbTotDl" FROM hourly WHERE vendor = \'ericsson\'').fetchdf()
    got["hour"] = pd.to_datetime(got["hour"])
    got = got.set_index(["cell_id", "hour"])["RRU.PrbTotDl"]
    assert (got.sort_index() - h.sort_index()).abs().max() < 1e-3
    assert got.max() <= 100


# --- agregasi KPI ------------------------------------------------------------------------------------------------
def test_kpi_is_ratio_of_sums_not_mean_of_ratios(run):
    sp = spec.load(ROOT)
    v = pipeline.kpis(run["con"], sp, "vendor").set_index("vendor")
    h = run["con"].execute("SELECT * FROM hourly WHERE vendor = 'ericsson'").fetchdf()
    ratio_of_sums = h["DRB.IPVolDl.sum"].sum() / h["DRB.IPTimeDl.sum"].sum()
    mean_of_ratios = (h["DRB.IPVolDl.sum"] / h["DRB.IPTimeDl.sum"]).mean()
    assert v.loc["ericsson", "ip_thp_dl"] == pytest.approx(ratio_of_sums)
    assert abs(ratio_of_sums - mean_of_ratios) > 0.01          # rata-rata rasio memberi angka lain (dan salah)


def test_accessibility_is_product_of_three_ratios(run):
    sp = spec.load(ROOT)
    v = pipeline.kpis(run["con"], sp, "vendor").set_index("vendor").loc["ericsson"]
    assert v["erab_accessibility"] == pytest.approx(v["rrc_setup_sr"] * v["s1_setup_sr"] * v["erab_init_setup_sr"] / 1e4)


def test_unmapped_counters_give_unavailable_kpis_not_guesses(run):
    cov = run["res"]["kpi_coverage"]
    assert cov["huawei"]["erab_retainability_r2"].startswith("unavailable") and cov["huawei"]["ho_exec_sr"].startswith("unavailable")
    assert all(s == "ok" for s in cov["ericsson"].values())
    v = pd.read_csv(run["out"] / "kpi_vendor_day.csv").set_index("vendor")
    assert pd.isna(v.loc["huawei", "erab_retainability_r2"]) and not pd.isna(v.loc["ericsson", "erab_retainability_r2"])
    assert run["res"]["dq"]["nokia"]["skipped"] and run["res"]["dq"]["zte"]["skipped"]


# --- data quality: semua kerumitan yang disuntikkan terdeteksi ------------------------------------------------------
@pytest.mark.parametrize("vendor", ["ericsson", "huawei"])
def test_dq_detects_every_injected_quirk(run, vendor):
    m, d = run["manifest"][vendor], run["res"]["dq"][vendor]
    assert d["duplicates_removed"] == m["duplicates"]
    assert d["negative_counter_values_nulled"] == {m["reset_counter"]: len(m["reset"])}
    assert sum(d["succ_greater_than_att_rops"].values()) == len(m["succ_gt_att"])
    assert d["unmatched_cells"] == 0
    if vendor == "ericsson":
        assert d["incomplete_cell_hours"] == len({_key_to_utc(vendor, c, t)[0] + str(_key_to_utc(vendor, c, t)[1].replace(minute=0)) for c, t in m["dropped"]})


def test_division_by_zero_is_null_not_inf(tmp_path):
    data = tmp_path / "d"; generate.generate(data, days=1, sites_per_vendor=1, seed=3)
    df = pd.read_csv(data / "huawei_lte.csv"); df.loc[0, "L.ChMeas.PRB.DL.Avail"] = 0; df.to_csv(data / "huawei_lte.csv", index=False)
    res = pipeline.run(data, tmp_path / "o")
    vals = res["con"].execute('SELECT "RRU.PrbTotDl" FROM rop WHERE vendor = \'huawei\'').fetchdf()["RRU.PrbTotDl"]
    assert vals.isna().sum() == 1 and (vals.dropna() <= 100).all()


def test_missing_vendor_column_reported(tmp_path):
    data = tmp_path / "d"; generate.generate(data, days=1, sites_per_vendor=1, seed=3)
    df = pd.read_csv(data / "ericsson_lte.csv").drop(columns=["pmSessionTimeUe"]); df.to_csv(data / "ericsson_lte.csv", index=False)
    res = pipeline.run(data, tmp_path / "o")
    miss = {c["counter"]: c["status"] for c in res["dq"]["ericsson"]["unavailable_counters"]}
    assert miss.get("ERAB.SessionTimeUE") == "missing_in_export"
    assert res["kpi_coverage"]["ericsson"]["erab_retainability_r2"].startswith("unavailable")
