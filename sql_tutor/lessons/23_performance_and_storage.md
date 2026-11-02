# 23 - Performance and storage: Parquet, pushdown and partitioning

Level: advanced | In this project: `COPY ... (FORMAT parquet, COMPRESSION zstd)`, `extract_date=/run_id=` folders, `--runs latest` | Needs: lesson 10

## 1. Theory

**Row stores versus column stores.** A transactional database (PostgreSQL) keeps whole rows together: good for "read or update
one record". An analytic engine (DuckDB, Parquet, BigQuery) keeps each COLUMN together: a query that needs 2 of 40 columns reads
about 2/40 of the data, and similar values next to each other compress very well.

**Parquet** is the standard columnar file format. Inside a file: rows are grouped into **row groups**; each column chunk has
min/max **statistics**; values are encoded and compressed (zstd here). Types are stored, so no re-parsing or guessing.

**Pushdown**: the engine pushes work down into the file scan.
- *Projection pushdown*: read only the columns the query uses.
- *Filter pushdown*: use the min/max statistics to skip row groups that cannot match `WHERE id = 42`.

**Partitioning** splits data into folders by a column (`dataverse_id=3/`, `extract_date=2026-10-13/`). A filter on the partition
column lets the engine skip whole folders without opening them. Choose columns that queries filter on and that have few distinct
values (date, region), not ids.

Other principles:
- Parsing JSON or CSV is expensive; do it ONCE, store typed Parquet, and let later stages read that.
- `SELECT *` defeats projection pushdown. Name the columns you need.
- The biggest wins come from reading less data, then from joining smaller inputs, then from tuning settings.
- Look at the plan with `EXPLAIN` (and `EXPLAIN ANALYZE` for timings and row counts) before guessing.
- Indexes matter less in analytic engines: scans over columnar data are the default path.

## 2. Real-world case

The sample extraction stores 370 dataset records as one 0.5 MB gzipped JSONL file per dataverse. That is tiny today, but
`python silver.py` defaults to `--runs all`, so every build re-reads and re-parses EVERY raw run ever made: the cost grows with
each new run. Silver exists precisely to pay the parsing cost once and publish typed, compressed Parquet files that gold and
dashboards can query quickly. `--runs latest` is the lever when a full rebuild gets slow.

## 3. Popular usage

- Data lakes and lakehouses: Parquet (or Iceberg / Delta on top) partitioned by date.
- Converting CSV / JSON exports to Parquet as the first step of any analytics pipeline.
- Reading only the needed columns and date range from huge files with DuckDB, Spark, Athena or BigQuery.

## 4. Example code

```python
# Build 300,000 rows and write them as CSV, JSON lines and Parquet. Compare the sizes.
import os, pathlib, tempfile
tmp = tempfile.TemporaryDirectory()          # removed automatically when the tutor exits
root = pathlib.Path(tmp.name)
con.execute("""
    CREATE TABLE big AS
    SELECT i AS id, 'DS' || (i % 5000) AS global_id, (i % 7) AS dataverse_id,
           DATE '2025-01-01' + CAST(i % 365 AS INTEGER) AS day, repeat('x', 40) AS note
    FROM range(300000) t(i)""")
paths = {name: (root / f"big.{name}").as_posix() for name in ("csv", "json", "parquet")}
con.execute(f"COPY big TO '{paths['csv']}' (FORMAT csv, HEADER)")
con.execute(f"COPY big TO '{paths['json']}' (FORMAT json)")
con.execute(f"COPY big TO '{paths['parquet']}' (FORMAT parquet, COMPRESSION zstd)")
for name, p in paths.items():
    print(f"{name:8} {os.path.getsize(p) / 1024:9,.0f} KB")
con.execute(f"CREATE VIEW big_parquet AS SELECT * FROM read_parquet('{paths['parquet']}')")
con.execute(f"CREATE VIEW big_parquet_meta AS SELECT * FROM parquet_metadata('{paths['parquet']}')")
```

```sql
-- Projection pushdown in action: this query needs 1 of 5 columns, so only that column is read from the Parquet file.
SELECT dataverse_id, count(*) AS n FROM big_parquet GROUP BY dataverse_id ORDER BY dataverse_id;
```

```sql
-- Statistics for the id column: min/max per row group. A filter like id = 42 can skip every group whose range excludes 42.
SELECT row_group_id, stats_min, stats_max, num_values
FROM big_parquet_meta
WHERE path_in_schema = 'id'
ORDER BY row_group_id;
```

```python
# Filter pushdown: the WHERE condition appears INSIDE the Parquet scan node of the plan, not as a separate step afterwards.
plan = "\n".join(str(row[1]) for row in con.execute("EXPLAIN SELECT * FROM big_parquet WHERE id = 42").fetchall())
print("filter is part of the Parquet scan:", "id=42" in plan.replace(" ", ""))
```

```python
# Partitioning: one folder per dataverse_id. A filter on the partition column only needs to open ONE folder.
by_dv = root / "by_dataverse"
con.execute(f"COPY big TO '{by_dv.as_posix()}' (FORMAT parquet, PARTITION_BY (dataverse_id))")
print("folders:", sorted(p.name for p in by_dv.iterdir()))
n = con.sql(f"""SELECT count(*) FROM read_parquet('{by_dv.as_posix()}/*/*.parquet', hive_partitioning = true)
                WHERE dataverse_id = 3""").fetchone()[0]
print("rows with dataverse_id = 3:", n)
```

```sql
-- Engine settings exist but come last: first read less data. Example: cap the worker threads.
SET threads = 2;
SELECT current_setting('threads') AS threads;
```
