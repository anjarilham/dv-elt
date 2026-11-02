# 22 - Data quality as SQL: checks, severity and gates

Level: advanced | In this project: `qa_checks` (section 6 of the flatten SQL) and `check_quality()` in `silver.py` | Needs: lessons 02, 03, 18

## 1. Theory

A data quality check is a query that COUNTS BAD ROWS. Zero means pass. That one convention makes checks easy to
stack (`UNION ALL`), report and automate. Dimensions to cover:

| Dimension | Question | Typical SQL |
|---|---|---|
| Uniqueness | is the key unique? | `GROUP BY key HAVING count(*) > 1` |
| Completeness | are required values present? | `WHERE col IS NULL` |
| Validity | does the value parse / fit the rule? | raw NOT NULL and `TRY_CAST` IS NULL; `NOT IN (accepted list)` |
| Referential integrity | does every child have a parent? | anti join child to parent |
| Reconciliation | do counts match between layers and the manifest? | compare `count(*)` of two tables |
| Consistency | do related values agree? | one entity under two parents; `end < start` |
| Freshness | is the data recent enough? | `max(updated_at)` vs the reference date |

**Severity** separates two different situations:
- `error`: the OUTPUT is wrong (duplicate keys, orphans, missing rows). A **gate** stops the build and nothing is published.
- `warn`: the SOURCE data is messy (an invalid date typed by a depositor). Log it, store it in the manifest, still publish,
  and send it to the data owner.

Good checks are **debuggable**: besides the count, you can list the failing keys. And they run **before** publishing,
not after users have seen the data.

## 2. Real-world case

`silver.py` publishes Parquet files that other people will read. Two gates run before any upload: (1) the number of rows
DuckDB loaded must equal the number the raw manifests promise (catches a truncated file), (2) every `error` check must be
0. The sample data contains one real source problem: DS3's publication date `2025-13-45`. It must NOT block the build (it is
the depositor's mistake), but it must be reported with the dataset id so someone can fix it. Hence severity `warn`.

## 3. Popular usage

- dbt tests (`unique`, `not_null`, `relationships`, `accepted_values`) and Great Expectations / Soda suites are this same
  idea with more tooling.
- Row-count reconciliation between source system and warehouse after every load.
- Freshness alerts ("no new rows since yesterday").

## 4. Example code

```python
build_silver()   # runs the project's real flatten SQL, including its 8 qa_checks, on the sample data
```

```sql
-- The project's own checks: all 0 except one warning (DS3's invalid publication date).
SELECT check_name, severity, bad_rows FROM qa_checks ORDER BY severity, check_name;
```

```sql
-- Debuggable: not just "1 bad row" but WHICH row, and the raw text that failed.
SELECT global_id, publication_date_raw
FROM stg_datasets_all
WHERE publication_date_raw IS NOT NULL AND TRY_CAST(publication_date_raw AS DATE) IS NULL;
```

```sql
-- Writing more checks: accepted values (error), a range rule (warn), and freshness against a reference date (warn).
SELECT 'version_state outside the accepted list' AS check_name, 'error' AS severity, count(*) AS bad_rows
FROM silver_datasets
WHERE version_state IS NOT NULL AND version_state NOT IN ('RELEASED', 'DRAFT', 'DEACCESSIONED')
UNION ALL
SELECT 'publication_date later than the reference date', 'warn', count(*)
FROM silver_datasets WHERE publication_date > DATE '2026-10-13'
UNION ALL
SELECT 'newest update is older than 365 days (stale source)', 'warn',
       CASE WHEN max(last_update_time) < TIMESTAMP '2026-10-13 00:00:00' - INTERVAL 365 DAY THEN 1 ELSE 0 END
FROM silver_datasets;
```

```python
# The gate, in the same shape as silver.check_quality(): reconcile counts, then fail on any error-severity check.
class QualityGateError(Exception):
    pass

def gate(expected_rows):
    loaded = con.sql("SELECT count(*) FROM bronze_meta").fetchone()[0]
    if loaded != expected_rows:
        raise QualityGateError(f"loaded {loaded} rows but the manifests promise {expected_rows}")
    checks = con.sql("SELECT check_name, severity, bad_rows FROM qa_checks ORDER BY severity, check_name").fetchall()
    for name, severity, bad in checks:
        status = "ok  " if bad == 0 else ("WARN" if severity == "warn" else "FAIL")
        print(f"  {status} {name}" + (f" ({bad})" if bad else ""))
    failed = [name for name, severity, bad in checks if severity == "error" and bad > 0]
    if failed:
        raise QualityGateError("quality gate failed, nothing published: " + "; ".join(failed))

print("1) normal data:")
gate(expected_rows=6)
print("2) the manifest says 7 rows but only 6 were loaded:")
try:
    gate(expected_rows=7)
except QualityGateError as e:
    print("  stopped ->", e)
print("3) an error-severity check finds bad rows:")
con.execute("INSERT INTO qa_checks VALUES ('simulated duplicate keys', 'error', 3)")
try:
    gate(expected_rows=6)
except QualityGateError as e:
    print("  stopped ->", e)
```
