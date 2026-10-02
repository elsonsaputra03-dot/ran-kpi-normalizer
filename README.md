# RAN KPI Normalizer

Turn LTE performance counters from different vendors into one model, then compute KPIs once.

```
vendor PM exports            mappings/<vendor>.yaml          spec/canonical_counters.yaml     spec/kpis.yaml
Ericsson  15-min, UTC, kbit ─┐  vendor counter expressions  ─►  3GPP TS 32.425 measurements ─►  TS 32.450 KPIs
Huawei    60-min, WIB, bit  ─┼─ (names, units, splits)          per cell per hour               at cell, site, vendor, network
Nokia, ZTE  (templates)     ─┘                                  + data quality report
```

Every vendor names, splits and scales its counters differently: Ericsson reports throughput volume in kbit per 15-minute ROP in UTC,
Huawei in bits per hour in local time, and they split handovers differently. Comparing vendors, or rolling a region up across them,
needs one model in the middle. This project uses the public 3GPP one:

- **Unified counters** are the measurement names of **3GPP TS 32.425** (E-UTRAN performance measurements), with the clause cited for
  each one in [`spec/canonical_counters.yaml`](spec/canonical_counters.yaml).
- **KPIs** follow **3GPP TS 32.450** (E-UTRAN KPI definitions). KPIs that operators use but 3GPP does not define are marked
  `standard: convention` with the reason, in [`spec/kpis.yaml`](spec/kpis.yaml).
- **Vendor mappings** are YAML, one file per vendor. Adding a vendor is adding a file, not changing code.

## What it does

| Step | Detail |
|---|---|
| Read | Vendor CSV exports as-is, including counter names with dots and hyphens (`L.E-RAB.AbnormRel`) |
| Clean | Drop re-delivered duplicate rows; convert local time to UTC; map vendor cell names to one cell id |
| Guard | A negative counter value (counter reset) is set to NULL for that counter and period only, and reported |
| Map | Evaluate each mapping expression into a unified counter, including unit conversion |
| Roll up | Per cell per hour: cumulative counters summed; PRB usage averaged by seconds covered, never summed; completeness recorded |
| KPIs | `SUM(numerator) / SUM(denominator)` at cell, site, vendor or network level, per hour or day |
| Report | Duplicates, resets, success greater than attempts, incomplete hours, unmapped counters, and which KPIs each vendor cannot produce |

```bash
pip install -e ".[dev]"
ranorm coverage                                   # mapping status per vendor and which KPIs each one can produce
ranorm generate --out data                        # synthetic exports with field quirks and a ground-truth manifest
ranorm run --data data --out out                  # kpi_{cell,site,vendor,network}_day.csv, kpi_vendor_hour.csv, unified_hourly.parquet, dq.json
pytest -q
```

## Design decisions from the specifications

1. **3GPP retainability is not a percentage.** TS 32.450 6.2.1 defines E-RAB retainability as abnormal releases per second of session
   time (`ERAB.RelActNbr / ERAB.SessionTimeUE`). The familiar "service drop rate %" is provided too, labelled as a convention.
2. **Units are checked against the text.** `ERAB.SessionTimeUE` counts seconds (TS 32.425 4.2.4.1); a wrong unit moves retainability
   by a factor of 1000.
3. **PRB usage is a percentage per period** (`RRU.PrbTotDl`, TS 32.425 4.5.3), so it is time-weighted, not summed.
4. **The specs name handovers differently.** TS 32.450 uses `HO.ExeAtt`; TS 32.425 splits by intra/inter-eNB and by frequency; vendors
   split by frequency at cell-relation level. The model keeps the total.
5. **Missing is reported, not estimated.** No public Huawei equivalent of `ERAB.SessionTimeUE` was identified, so R2 retainability is
   "unavailable" for Huawei, with the missing counter named, instead of an approximation.

## Correctness

The generator first creates the truth as unified counters, then renders it into each vendor's export format with the quirks found in
real exports: missing ROPs, re-delivered files, counter resets and success counts above attempts. The tests check that:

- every unified counter comes back **identical** to the truth after vendor naming, unit conversion, time zones and roll-up;
- every injected quirk is detected and counted in the data quality report;
- a KPI equals the ratio of sums and differs from the mean of per-cell ratios, the classic aggregation mistake;
- division by zero yields NULL, not `inf` (DuckDB returns `inf`, which would corrupt any network average);
- mapping expressions are parsed, not pasted into SQL: identifiers are quoted, every divisor is wrapped in `NULLIF(x, 0)`, and anything
  that is not a counter name, a number, `+ - * /` or brackets is rejected.

## Vendor mapping status

`ranorm coverage` prints the current state. Each counter carries a status, and a counter name is only filled in when it appears in at
least one independent public source (engineering blogs, forums, open training material), not only in documents marked confidential by
a vendor or an operator.

| Vendor | Counters filled in | KPIs available |
|---|---|---|
| Ericsson | 18 of 19 (PRB used DL is a release-dependent sum, left to verify) | 11 of 11 |
| Huawei | 15 of 17 (no public equivalent of `ERAB.SessionTimeUE`; manual unavailability to verify) | 10 of 11 |
| Nokia | 4 of 19 (RRC and E-RAB setup) | 2 of 11 |
| ZTE | 0 of 19: ZTE counters are numeric IDs that change between software versions and only appear in vendor-confidential documents | 0 of 11 |

Counter names are vendor PM counter names. Check them against the vendor documentation for your software release before relying on
the output; releases rename and split counters. The pipeline skips a vendor whose export format is not defined and reports, per vendor,
which KPIs cannot be produced and which counters are missing.

## Sources

- 3GPP TS 32.425 (ETSI TS 132 425 V14.1.0), *Performance measurements, E-UTRAN*
- 3GPP TS 32.450 (ETSI TS 132 450 V17.0.0), *Key Performance Indicators for E-UTRAN: Definitions*
- No operator or employer documents, formulas or data are used, including copies of such documents found online. Mapping expressions
  come from independent public references, and the data is synthetic.

## Limitations and next steps

- LTE only. GSM (3GPP TS 52.402) and 5G NR (TS 28.552 / TS 28.554) are the natural next layers.
- Counters are summed across causes and QCIs (`.sum`); per-QCI KPIs (for example VoLTE on QCI 1) are not modelled yet.
- The synthetic data has realistic structure, not realistic radio behaviour.

## License

MIT
