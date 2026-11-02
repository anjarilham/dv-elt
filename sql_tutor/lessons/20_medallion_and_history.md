# 20 - Medallion layers and keeping history (SCD type 1 and 2)

Level: advanced | In this project: bronze (raw bucket) -> silver (`silver_*`), `silver_datasets` keeps the current version only | Needs: lessons 08, 09

## 1. Theory

**Medallion architecture** organizes data by quality:
- **Bronze**: raw, append-only, exactly as received. Never edited. The source of truth you can always rebuild from.
- **Silver**: cleaned, typed, de-duplicated, conformed. One row per real-world thing, ready to join.
- **Gold**: business-level tables for one use case: aggregates, star schemas (fact + dimension tables), feature tables.

**Slowly changing dimensions (SCD)** are about what happens when an attribute changes:
- **Type 1**: overwrite. Only the current value exists. Simple and small. (`silver_datasets` is type 1.)
- **Type 2**: keep every version with a validity range: `valid_from`, `valid_to`, and often an `is_current` flag.
  Lets you answer "what was it on date X?". Built with `lag()` to detect a change and `lead()` to close the range.
- Type 3 keeps just the previous value in an extra column. Rarely used.

Type 2 recipe:
1. Take the snapshots over time (here: one per extraction run).
2. `lag(attr) OVER (PARTITION BY key ORDER BY snapshot_date)` gives the previous value.
3. Keep only the rows where `attr IS DISTINCT FROM prev` (a change, or the first sighting).
4. `valid_to = lead(snapshot_date) OVER (PARTITION BY key ORDER BY snapshot_date)`; NULL means "still current".
5. Point-in-time query: `WHERE d >= valid_from AND (valid_to IS NULL OR d < valid_to)`.

Limitation: a key that simply stops appearing is not "changed", so a naive type 2 keeps showing it as current. Handle
deletes explicitly (a `deleted_at` flag, or compare with the latest snapshot).

## 2. Real-world case

Silver keeps the NEWEST title of each dataset, which is what most users want. A data steward then asks:
"What was the title of DS1 when the 12 October report was produced?" Silver cannot answer, because it overwrote the
old title. But bronze still has every run, so a history table can be built from it without touching the extraction.
This is the benefit of keeping bronze immutable.

## 3. Popular usage

- Customer / product / employee dimensions in a warehouse (addresses, prices and job titles change).
- Audit trails and "as of" reporting in finance and compliance.
- Gold-layer star schemas: a fact table joins to the dimension row that was valid at the event date.

## 4. Example code

```python
build_silver()   # runs the project's real flatten_datasets_metadata.sql on the sample files
```

```sql
-- The layers by size: bronze has one row per record per run, silver one row per dataset.
SELECT 'bronze_meta           (1 row per record per run)' AS layer_table, count(*) AS n FROM bronze_meta
UNION ALL SELECT 'silver_datasets        (1 row per dataset)',            count(*) FROM silver_datasets
UNION ALL SELECT 'silver_dataset_authors (1 row per dataset+author)',     count(*) FROM silver_dataset_authors
UNION ALL SELECT 'silver_dataset_subjects(1 row per dataset+subject)',    count(*) FROM silver_dataset_subjects;
```

```sql
-- SCD type 1: only the current title exists. DS1 shows the run-B title; the old one is gone from silver.
SELECT global_id, source_run_id, title FROM silver_datasets ORDER BY global_id;
```

```sql
-- SCD type 2 built from bronze: one row per distinct title, with the date range it was valid.
CREATE TEMP TABLE dataset_history AS
WITH seen AS (
    SELECT f.global_id, r.extract_date, f.value ->> '$' AS title
    FROM stg_fields f
    JOIN (SELECT DISTINCT run_id, extract_date FROM bronze_meta) r USING (run_id)
    WHERE f.type_name = 'title'
),
changes AS (
    SELECT *, lag(title) OVER (PARTITION BY global_id ORDER BY extract_date) AS prev_title FROM seen
),
versions AS (SELECT * FROM changes WHERE title IS DISTINCT FROM prev_title)
SELECT global_id, title, extract_date AS valid_from,
       lead(extract_date) OVER (PARTITION BY global_id ORDER BY extract_date) AS valid_to
FROM versions;

SELECT *, valid_to IS NULL AS is_current FROM dataset_history ORDER BY global_id, valid_from;
```

```sql
-- Point-in-time: the catalogue as it looked on 12 October.
SELECT global_id, title FROM dataset_history
WHERE DATE '2026-10-12' >= valid_from AND (valid_to IS NULL OR DATE '2026-10-12' < valid_to)
ORDER BY global_id;
```

```sql
-- ...and on 13 October. DS1 now has its new title. But DS4 is listed although it was NOT in the 13 October run.
SELECT global_id, title FROM dataset_history
WHERE DATE '2026-10-13' >= valid_from AND (valid_to IS NULL OR DATE '2026-10-13' < valid_to)
ORDER BY global_id;
```

```sql
-- The limitation made visible: "current" in the history but absent from the newest run (a deleted or hidden dataset).
SELECT h.global_id, h.title
FROM dataset_history h
WHERE h.valid_to IS NULL
  AND NOT EXISTS (SELECT 1 FROM stg_fields f WHERE f.global_id = h.global_id AND f.run_id LIKE '%bbbbbb');
```
