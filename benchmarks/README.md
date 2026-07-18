# Frozen benchmarks

Files in this directory are immutable, content-addressed evaluation contracts.
They bind one benchmark ID to:

- the historical sample, Gold, Omni cache, and D10-B evidence fingerprints;
- the source files that define ranking and resolver behavior;
- the account scope, label version, time split, K value, and strategy set;
- one reference backtest report and its comparable metrics.

`cross_entry` manifests additionally bind G1 precut batches, G2 generated
candidates, their shared `standard_candidate.v1` contract, source media content
identities, and the adopted production ranking policy. A frozen baseline may
still report product gaps; immutability is not the same as promotion readiness.

Never edit an existing JSON manifest. When any fingerprint or evaluation policy
changes, freeze a new benchmark ID instead.

```bash
dso benchmark-verify --benchmark-id dso-v1-beta-d10-ab-20260715-r1
dso benchmark-run --benchmark-id dso-v1-beta-d10-ab-20260715-r1
dso benchmark-verify --benchmark-id dso-v1-cross-entry-20260718-r2
```

Create a new immutable manifest only after running the intended reference
backtest:

```bash
dso benchmark-freeze \
  --benchmark-id dso-v1-beta-d10-ab-YYYYMMDD-rN \
  --reference-report-id bt_reference_id

dso benchmark-freeze \
  --benchmark-id dso-v1-cross-entry-YYYYMMDD-rN \
  --benchmark-kind cross_entry \
  --reference-report-id bt_reference_id
```
