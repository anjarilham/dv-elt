# 21 - Incremental loads and upserts (MERGE, ON CONFLICT)

Level: advanced | In this project: not used yet (silver is a full rebuild); needed when loading to PostgreSQL (`pg_*` settings in `config.py`) | Needs: lessons 03, 19

## 1. Theory

**Full refresh** recomputes everything each time: simple, always correct, but cost grows with the data (the silver `--runs all`
mode re-reads every raw run). **Incremental** processes only what is new or changed, using a **watermark**: a value that says
"everything up to here is already done", typically `max(last_update_time)` of the target.

Writing the changes into a table that already has rows is an **upsert**: update the row if the key exists, insert it if not.
- `INSERT ... ON CONFLICT (key) DO UPDATE SET col = excluded.col [WHERE condition]` (PostgreSQL, DuckDB, SQLite). Needs a
  PRIMARY KEY or UNIQUE constraint on `key`. `excluded` is the row you tried to insert.
- `MERGE INTO target USING source ON condition WHEN MATCHED THEN UPDATE ... WHEN NOT MATCHED THEN INSERT ...`
  (SQL standard; PostgreSQL 15+, DuckDB, SQL Server, Snowflake, BigQuery). More flexible: it can also `DELETE`.

Rules for safe incremental loads:
1. A reliable key and a reliable "version" column (last update time).
2. The upsert must be idempotent: loading the same batch twice changes nothing.
3. Guard against **stale** data: only overwrite when the incoming row is newer (`WHERE excluded.last_update > target.last_update`).
4. Decide how **deletes** are signalled: the source may just stop returning a row. Use a soft-delete flag or a periodic full refresh.
5. Keep an occasional full rebuild as the safety net.

## 2. Real-world case

The silver stage is a full rebuild, which is fine at 370 datasets. The planned next stage loads gold data into PostgreSQL
(see the `pg_*` settings), where dropping and reloading a table is not acceptable if dashboards are reading it. There the
load must be an idempotent upsert: a changed dataset replaces its row, a new dataset is inserted, and a stale row from an
old run must NOT overwrite a newer one.

## 3. Popular usage

- Change data capture (CDC) feeds applied to a warehouse table with MERGE.
- dbt `incremental` models (`unique_key` + `is_incremental()` watermark filter).
- Syncing a CRM or catalogue where only a small share of rows change daily.

## 4. Example code

```sql
-- The target table. The PRIMARY KEY is what ON CONFLICT needs.
CREATE TABLE silver_ds (global_id VARCHAR PRIMARY KEY, title VARCHAR, last_update TIMESTAMP);
INSERT INTO silver_ds VALUES
    ('DS1', 'Rice yield survey',      TIMESTAMP '2025-01-01 00:00:00'),
    ('DS4', 'Soil moisture sensors',  TIMESTAMP '2025-02-10 09:00:00');
```

```sql
-- The next batch: DS1 changed, DS3 is new, DS4 arrives with an OLDER timestamp (a stale re-delivery).
CREATE TABLE incoming AS
SELECT * FROM (VALUES
    ('DS1', 'Rice yield survey 2025', TIMESTAMP '2025-06-01 00:00:00'),
    ('DS3', 'Fish landing records',   TIMESTAMP '2025-05-20 01:30:00'),
    ('DS4', 'OLD STALE TITLE',        TIMESTAMP '2025-01-01 00:00:00')
) AS t(global_id, title, last_update);
```

```sql
-- The watermark: what is the newest thing we already have? Only rows newer than this need to be fetched.
SELECT (SELECT max(last_update) FROM silver_ds) AS watermark,
       (SELECT count(*) FROM incoming WHERE last_update > (SELECT max(last_update) FROM silver_ds)) AS rows_worth_processing;
```

```sql
-- Upsert with a stale-data guard: DS1 updated, DS3 inserted, DS4 untouched because the incoming row is older.
INSERT INTO silver_ds
SELECT * FROM incoming
ON CONFLICT (global_id) DO UPDATE
    SET title = excluded.title, last_update = excluded.last_update
    WHERE excluded.last_update > silver_ds.last_update;
SELECT * FROM silver_ds ORDER BY global_id;
```

```python
# Idempotency test: apply the SAME batch again. Nothing may change.
upsert = """INSERT INTO silver_ds SELECT * FROM incoming
            ON CONFLICT (global_id) DO UPDATE SET title = excluded.title, last_update = excluded.last_update
            WHERE excluded.last_update > silver_ds.last_update"""
before = con.sql("SELECT * FROM silver_ds ORDER BY global_id").fetchall()
con.execute(upsert)
after = con.sql("SELECT * FROM silver_ds ORDER BY global_id").fetchall()
print("second run changed nothing:", before == after)
```

```sql
-- The same logic with MERGE (it can also delete). A copy of the original target is merged with the same batch.
CREATE TABLE silver_ds2 AS
SELECT * FROM (VALUES
    ('DS1', 'Rice yield survey',     TIMESTAMP '2025-01-01 00:00:00'),
    ('DS4', 'Soil moisture sensors', TIMESTAMP '2025-02-10 09:00:00')
) AS t(global_id, title, last_update);

MERGE INTO silver_ds2 AS t
USING incoming AS s ON t.global_id = s.global_id
WHEN MATCHED AND s.last_update > t.last_update THEN UPDATE SET title = s.title, last_update = s.last_update
WHEN NOT MATCHED THEN INSERT VALUES (s.global_id, s.title, s.last_update);

SELECT * FROM silver_ds2 ORDER BY global_id;
```

```postgres
-- PostgreSQL: the same two statements. ON CONFLICT works as in DuckDB; MERGE needs PostgreSQL 15 or newer. (Not run here.)
INSERT INTO silver.datasets (global_id, title, last_update)
SELECT global_id, title, last_update FROM staging.incoming
ON CONFLICT (global_id) DO UPDATE
    SET title = EXCLUDED.title, last_update = EXCLUDED.last_update
    WHERE EXCLUDED.last_update > silver.datasets.last_update;
```
