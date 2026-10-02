"""Data sintetis: kebenaran dalam counter seragam, lalu dirender ke ekspor native tiap vendor, beserta "kerumitan" lapangan.

Urutan: (1) bangkitkan nilai counter seragam per cell per 15 menit (kebenaran), (2) pecah dan ubah satuannya ke counter vendor
(Ericsson: ROP 15 menit UTC; Huawei: 60 menit waktu lokal WIB, bit), (3) suntikkan kerumitan dan catat di manifest:
ROP hilang, baris terkirim ulang (duplikat), counter reset (nilai negatif), dan sukses > attempt.
Karena kebenarannya diketahui, test bisa memastikan pipeline mengembalikan angka yang sama setelah semua kerumitan itu.
"""
from __future__ import annotations

import csv
import json
import math
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

START = datetime(2026, 9, 1, tzinfo=timezone.utc)
BANDS = ("L18", "L21")


def _diurnal(h: float) -> float:
    return 0.25 + 0.9 * math.exp(-((h - 20.5) ** 2) / 10) + 0.45 * math.exp(-((h - 12) ** 2) / 8)


def inventory(sites_per_vendor: int = 8) -> list[dict]:
    rows, n = [], 0
    for vendor in ("ericsson", "huawei"):
        for s in range(sites_per_vendor):
            n += 1
            site = f"S{n:04d}"
            for band in BANDS:
                for sector in (1, 2, 3):
                    cell = f"{site}_{band}_{sector}"
                    native = f"ERBS_{site}_{band}_{sector}" if vendor == "ericsson" else f"{site}-{band}-{sector}"
                    rows.append({"cell_id": cell, "site_id": site, "vendor": vendor, "band": band, "vendor_cell_name": native})
    return rows


def truth(cells: list[dict], days: int, seed: int) -> list[dict]:
    """Counter seragam per cell per 15 menit (UTC). Nilai integer kecuali PRB (persen) dan period_s."""
    rnd, out = random.Random(seed), []
    for c in cells:
        load = math.exp(rnd.gauss(0, 0.4)) * (1.25 if c["band"] == "L18" else 1.0)
        weak = rnd.random() < 0.08                                # sebagian kecil cell bermasalah
        for q in range(days * 96):
            t = START + timedelta(minutes=15 * q)
            f = load * _diurnal(t.hour + 7 + t.minute / 60) * (1 + rnd.uniform(-0.1, 0.1))   # pola harian di jam lokal
            rrc_att = max(1, int(1200 * f)); rrc_ok = rrc_att - int(rrc_att * (rnd.uniform(0.01, 0.04) if weak else rnd.uniform(0.001, 0.006)))
            s1_att = rrc_ok; s1_ok = s1_att - int(s1_att * rnd.uniform(0, 0.002))
            e_att = s1_ok; e_ok = e_att - int(e_att * (rnd.uniform(0.005, 0.02) if weak else rnd.uniform(0, 0.003)))
            add_att = int(e_ok * 0.2); add_ok = add_att - int(add_att * rnd.uniform(0, 0.01))
            rel_att = e_ok + add_ok; rel_act = int(rel_att * (rnd.uniform(0.01, 0.03) if weak else rnd.uniform(0.001, 0.006)))
            ho_att = int(e_ok * 0.3); ho_ok = ho_att - int(ho_att * (rnd.uniform(0.02, 0.06) if weak else rnd.uniform(0.002, 0.01)))
            time_dl = int(e_ok * rnd.uniform(800, 1200)); thp_dl = rnd.uniform(4, 9) if weak else rnd.uniform(12, 30)  # Mbit/s = kbit/ms
            time_ul = int(time_dl * 0.6); thp_ul = thp_dl * rnd.uniform(0.15, 0.3)
            down = 900 if weak and rnd.random() < 0.01 else (rnd.choice([0, 0, 0, 30, 120]) if rnd.random() < 0.005 else 0)
            out.append({"cell_id": c["cell_id"], "vendor": c["vendor"], "ts": t,
                        "RRC.ConnEstabAtt.sum": rrc_att, "RRC.ConnEstabSucc.sum": rrc_ok, "S1SIG.ConnEstabAtt": s1_att,
                        "S1SIG.ConnEstabSucc": s1_ok, "ERAB.EstabInitAttNbr.sum": e_att, "ERAB.EstabInitSuccNbr.sum": e_ok,
                        "ERAB.EstabAddAttNbr.sum": add_att, "ERAB.EstabAddSuccNbr.sum": add_ok, "ERAB.RelActNbr.sum": rel_act,
                        "ERAB.RelAttNbr.sum": rel_att, "ERAB.SessionTimeUE": int(e_ok * rnd.uniform(25, 45)),
                        "HO.ExeAtt": ho_att, "HO.ExeSucc": ho_ok,
                        "DRB.IPVolDl.sum": int(time_dl * thp_dl), "DRB.IPTimeDl.sum": time_dl,
                        "DRB.IPVolUl.sum": int(time_ul * thp_ul), "DRB.IPTimeUl.sum": time_ul,
                        "RRU.PrbTotDl": round(min(98.0, 8 + 55 * f * (1.4 if weak else 1)), 2),
                        "RRU.CellUnavailableTime.sum": down, "period_s": 900})
    return out


def _split(total: int, rnd: random.Random, parts: int = 2) -> list[int]:
    cuts = sorted(rnd.randint(0, total) for _ in range(parts - 1))
    return [b - a for a, b in zip([0] + cuts, cuts + [total])]


def render_ericsson(rows: list[dict], names: dict, rnd: random.Random) -> list[dict]:
    out = []
    for r in rows:
        if r["vendor"] != "ericsson":
            continue
        enb_act, mme_act = _split(r["ERAB.RelActNbr.sum"], rnd)
        abn_enb = enb_act + rnd.randint(0, 3); mme = mme_act + rnd.randint(0, 5)
        normal = max(0, r["ERAB.RelAttNbr.sum"] - abn_enb - mme)
        ho_a = _split(r["HO.ExeAtt"], rnd); ho_s1 = min(ho_a[0], rnd.randint(max(0, r["HO.ExeSucc"] - ho_a[1]), r["HO.ExeSucc"]))
        last_tti = rnd.randint(0, 2000); avail = 100 * 900 // 1                     # 100 PRB tersedia, dijumlah per ROP
        down_auto = _split(r["RRU.CellUnavailableTime.sum"], rnd)
        out.append({"ROP_START": r["ts"].strftime("%Y-%m-%d %H:%M"), "EUtranCellFDD": names[r["cell_id"]],
                    "pmRrcConnEstabAtt": r["RRC.ConnEstabAtt.sum"], "pmRrcConnEstabSucc": r["RRC.ConnEstabSucc.sum"],
                    "pmRrcConnEstabAttReatt": rnd.randint(0, 5),
                    "pmS1SigConnEstabAtt": r["S1SIG.ConnEstabAtt"], "pmS1SigConnEstabSucc": r["S1SIG.ConnEstabSucc"],
                    "pmErabEstabAttInit": r["ERAB.EstabInitAttNbr.sum"], "pmErabEstabSuccInit": r["ERAB.EstabInitSuccNbr.sum"],
                    "pmErabEstabAttAdded": r["ERAB.EstabAddAttNbr.sum"], "pmErabEstabSuccAdded": r["ERAB.EstabAddSuccNbr.sum"],
                    "pmErabRelAbnormalEnbAct": enb_act, "pmErabRelAbnormalMmeAct": mme_act,
                    "pmErabRelAbnormalEnb": abn_enb, "pmErabRelNormalEnb": normal, "pmErabRelMme": mme,
                    "pmSessionTimeUe": r["ERAB.SessionTimeUE"],
                    "pmHoExeAttLteIntraF": ho_a[0], "pmHoExeAttLteInterF": ho_a[1],
                    "pmHoExeSuccLteIntraF": ho_s1, "pmHoExeSuccLteInterF": r["HO.ExeSucc"] - ho_s1,
                    "pmPdcpVolDlDrb": r["DRB.IPVolDl.sum"] + last_tti, "pmPdcpVolDlDrbLastTTI": last_tti,
                    "pmUeThpTimeDl": r["DRB.IPTimeDl.sum"], "pmUeThpVolUl": r["DRB.IPVolUl.sum"], "pmUeThpTimeUl": r["DRB.IPTimeUl.sum"],
                    "pmPrbUsedDl": round(r["RRU.PrbTotDl"] * avail / 100, 3), "pmPrbAvailDl": avail,
                    "pmCellDowntimeAuto": down_auto[0], "pmCellDowntimeMan": down_auto[1]})
    return out


def render_huawei(rows: list[dict], names: dict, rnd: random.Random) -> list[dict]:
    """Agregasi kebenaran 15 menit ke jam, lalu ke counter Huawei (bit, waktu lokal +7)."""
    hours: dict[tuple, dict] = {}
    for r in rows:
        if r["vendor"] != "huawei":
            continue
        k = (r["cell_id"], r["ts"].replace(minute=0))
        h = hours.setdefault(k, {"prb_w": 0.0, "secs": 0})
        for c, v in r.items():
            if c in ("cell_id", "vendor", "ts", "RRU.PrbTotDl", "period_s"):
                continue
            h[c] = h.get(c, 0) + v
        h["prb_w"] += r["RRU.PrbTotDl"] * r["period_s"]; h["secs"] += r["period_s"]
    out = []
    for (cell, ts), h in hours.items():
        last_dl, last_ul = rnd.randint(0, 2_000_000), rnd.randint(0, 500_000)
        avail = 100.0; used = round(h["prb_w"] / h["secs"] * avail / 100, 4)
        out.append({"Start Time": (ts + timedelta(hours=7)).strftime("%Y-%m-%d %H:%M:%S"), "eNodeB Name": cell.split("_")[0],
                    "Cell Name": names[cell],
                    "L.RRC.ConnReq.Att": h["RRC.ConnEstabAtt.sum"], "L.RRC.ConnReq.Succ": h["RRC.ConnEstabSucc.sum"],
                    "L.S1Sig.ConnEst.Att": h["S1SIG.ConnEstabAtt"], "L.S1Sig.ConnEst.Succ": h["S1SIG.ConnEstabSucc"],
                    "L.E-RAB.AttEst": h["ERAB.EstabInitAttNbr.sum"], "L.E-RAB.SuccEst": h["ERAB.EstabInitSuccNbr.sum"],
                    "L.E-RAB.AbnormRel": h["ERAB.RelActNbr.sum"], "L.E-RAB.NormRel": h["ERAB.RelAttNbr.sum"] - h["ERAB.RelActNbr.sum"],
                    "L.Thrp.bits.DL": h["DRB.IPVolDl.sum"] * 1000 + last_dl, "L.Thrp.bits.DL.LastTTI": last_dl,
                    "L.Thrp.Time.DL.RmvLastTTI": h["DRB.IPTimeDl.sum"],
                    "L.Thrp.bits.UL": h["DRB.IPVolUl.sum"] * 1000 + last_ul, "L.Thrp.bits.UE.UL.LastTTI": last_ul,
                    "L.Thrp.Time.UE.UL.RmvLastTTI": h["DRB.IPTimeUl.sum"],
                    "L.ChMeas.PRB.DL.Used.Avg": used, "L.ChMeas.PRB.DL.Avail": avail,
                    "L.Cell.Unavail.Dur.Sys": h["RRU.CellUnavailableTime.sum"], "L.Cell.Unavail.Dur.Manual": 0})
    return out


def inject(rows: list[dict], vendor: str, rnd: random.Random, time_col: str, cell_col: str, att: str, succ: str) -> tuple[list, dict]:
    """Kerumitan lapangan. Manifest mencatat baris terdampak supaya test bisa menghitung nilai yang diharapkan."""
    n = len(rows)
    drop = set(rnd.sample(range(n), max(1, n // 200)))                      # ~0,5% ROP tidak pernah datang
    reset = sorted(set(rnd.sample(range(n), 2)) - drop)                       # counter reset -> nilai negatif
    over = sorted(set(rnd.sample(range(n), 2)) - drop - set(reset))           # sukses > attempt (anomali penghitungan)
    key = lambda r: (r[cell_col], r[time_col])
    manifest = {"dropped": [key(rows[i]) for i in drop], "reset": [key(rows[i]) for i in reset], "reset_counter": succ,
                "succ_gt_att": [key(rows[i]) for i in over], "att": att, "succ": succ}
    for i in reset:
        rows[i][succ] = -abs(rows[i][succ]) - 1
    for i in over:
        rows[i][succ] = rows[i][att] + 5
    kept = [r for i, r in enumerate(rows) if i not in drop]
    dups = [dict(kept[i]) for i in rnd.sample(range(len(kept)), max(1, len(kept) // 200))]   # file terkirim ulang
    manifest["duplicates"] = len(dups)
    return kept + dups, manifest


def write_csv(path: Path, rows: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0])); w.writeheader(); w.writerows(rows)


def generate(out: Path, days: int = 2, sites_per_vendor: int = 8, seed: int = 7) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    rnd = random.Random(seed)
    cells = inventory(sites_per_vendor)
    names = {c["cell_id"]: c["vendor_cell_name"] for c in cells}
    t = truth(cells, days, seed)
    eri, eri_m = inject(render_ericsson(t, names, rnd), "ericsson", rnd, "ROP_START", "EUtranCellFDD", "pmRrcConnEstabAtt", "pmRrcConnEstabSucc")
    hua, hua_m = inject(render_huawei(t, names, rnd), "huawei", rnd, "Start Time", "Cell Name", "L.RRC.ConnReq.Att", "L.RRC.ConnReq.Succ")
    write_csv(out / "inventory.csv", cells)
    write_csv(out / "ericsson_lte.csv", eri)
    write_csv(out / "huawei_lte.csv", hua)
    tr = [{**r, "ts": r["ts"].isoformat()} for r in t]
    (out / "truth.json").write_text(json.dumps(tr))
    manifest = {"ericsson": eri_m, "huawei": hua_m, "days": days, "cells": len(cells), "seed": seed}
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1))
    return manifest
