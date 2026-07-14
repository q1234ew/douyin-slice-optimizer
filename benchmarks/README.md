# Frozen benchmarks

Files in this directory are immutable, content-addressed evaluation contracts.
They bind one benchmark ID to:

- the historical sample, Gold, Omni cache, and D10-B evidence fingerprints;
- the source files that define ranking and resolver behavior;
- the account scope, label version, time split, K value, and strategy set;
- one reference backtest report and its comparable metrics.

Never edit an existing JSON manifest. When any fingerprint or evaluation policy
changes, freeze a new benchmark ID instead.

```bash
dso benchmark-verify --benchmark-id dso-v1-beta-d10-ab-20260715-r1
dso benchmark-run --benchmark-id dso-v1-beta-d10-ab-20260715-r1
```

Create a new immutable manifest only after running the intended reference
backtest:

```bash
dso benchmark-freeze \
  --benchmark-id dso-v1-beta-d10-ab-YYYYMMDD-rN \
  --reference-report-id bt_reference_id
```
