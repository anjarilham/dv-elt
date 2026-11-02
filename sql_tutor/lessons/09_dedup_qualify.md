# 09 - Latest-version dedup with ROW_NUMBER and QUALIFY

Level: intermediate | In this project: `silver_datasets` (section 4 of `flatten_datasets_metadata.sql`) | Needs: lessons 04, 08

## 1. Theory

**De-duplication pattern**: number the copies of each key with `row_number()`, ordered so the copy you
want is first, then keep number 1.

```text
row_number() OVER (PARTITION BY <business key> ORDER BY <which copy wins> DESC, <tie-breakers>) = 1
```

Design decisions:
- **The key** (`PARTITION BY`): what makes two rows "the same thing" (here `global_id`).
- **Which copy wins**: prefer a business timestamp ("last updated at the source") over a load time, when you have one.
- **NULLs in the sort key**: say `NULLS LAST` explicitly so the result does not depend on an engine default.
- **Tie-breakers**: if two rows tie, `row_number()` picks one at random. Add more sort columns until the order is total,
  so every re-run gives the same answer.
- **Where to filter**: `QUALIFY` (DuckDB, Snowflake, BigQuery) or a subquery with `WHERE rn = 1` (everywhere).
  PostgreSQL also has `DISTINCT ON (key)`. DuckDB has `arg_max(value, order_col)` for "the value from the newest row".

## 2. Real-world case

Each extraction run exports the whole catalogue, so every dataset is repeated once per run. Counting
rows overstates the catalogue; DS1 even has a different title in each run. `silver_datasets` must have
ONE row per `global_id`, holding the newest version: newest `last_update_time`, then newest `run_id`,
then `dataverse` as a last tie-breaker. The quality check `duplicate global_id in silver_datasets`
proves it worked.

## 3. Popular usage

- "Latest record per customer / device / entity" from an append-only event or snapshot table (CDC feeds).
- Staging models in dbt that deduplicate a raw source.
- "First purchase per customer" (same pattern, ascending order).

## 4. Example code

```python
run_sql_file("setup_bronze.sql")   # bronze_meta: 6 dataset records over 2 runs
```

```sql
-- Six rows for four datasets. updated is an instant (TRY_CAST to TIMESTAMPTZ handles all three formats).
CREATE TEMP TABLE versions AS
SELECT data.identifier AS id, run_id,
       TRY_CAST(data.latestVersion.lastUpdateTime AS TIMESTAMPTZ) AS updated
FROM bronze_meta;
SELECT * FROM versions ORDER BY id, run_id;
```

```sql
-- QUALIFY: keep the newest copy per dataset. DS2 has no timestamp at all; the run_id tie-breaker still picks B.
SELECT * FROM versions
QUALIFY row_number() OVER (PARTITION BY id ORDER BY updated DESC NULLS LAST, run_id DESC) = 1
ORDER BY id;
```

```sql
-- The portable form (works in PostgreSQL, SQL Server, ...): number in a subquery, filter outside.
SELECT id, run_id, updated
FROM (SELECT *, row_number() OVER (PARTITION BY id ORDER BY updated DESC NULLS LAST, run_id DESC) AS rn
      FROM versions)
WHERE rn = 1
ORDER BY id;
```

```sql
-- arg_max(value, order_col) = "value from the row with the biggest order_col". Short, but it skips rows whose
-- order_col is NULL: DS2 becomes NULL here. Use it only when the order column is never NULL.
SELECT id, arg_max(run_id, updated) AS newest_run
FROM versions GROUP BY id ORDER BY id;
```

```sql
-- Why tie-breakers matter: both rows have the same date. With run_id DESC added, the answer is always B.
SELECT id, run_id, updated
FROM (VALUES ('DS9', 'A', DATE '2025-01-01'), ('DS9', 'B', DATE '2025-01-01')) AS t(id, run_id, updated)
QUALIFY row_number() OVER (PARTITION BY id ORDER BY updated DESC, run_id DESC) = 1;
```

```sql
-- The proof, as the project's quality check does it: no key may appear twice after the dedup.
WITH kept AS (
    SELECT * FROM versions
    QUALIFY row_number() OVER (PARTITION BY id ORDER BY updated DESC NULLS LAST, run_id DESC) = 1
)
SELECT (SELECT count(*) FROM versions)                  AS rows_before,
       (SELECT count(*) FROM kept)                      AS rows_after,
       (SELECT count(*) FROM (SELECT id FROM kept GROUP BY id HAVING count(*) > 1)) AS duplicate_keys;
```

```postgres
-- PostgreSQL only: DISTINCT ON keeps the first row of each key in the ORDER BY order. (Not run here.)
SELECT DISTINCT ON (id) id, run_id, updated
FROM versions
ORDER BY id, updated DESC NULLS LAST, run_id DESC;
```
