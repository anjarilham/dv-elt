# 25 - Dialect portability: DuckDB versus PostgreSQL (and the SQL standard)

Level: advanced | In this project: `QUALIFY`, `ANTI JOIN`, `GROUP BY ALL`, `TRY_CAST`, JSONPath `->>`, `CREATE OR REPLACE TABLE` | Needs: lessons 09, 12, 21

## 1. Theory

SQL is a standard with many dialects. The core (`SELECT`, joins, `GROUP BY`, CTEs, window functions, `UNION`, `CASE`,
`COALESCE`) is portable. Differences appear in: convenience syntax, JSON and array handling, date functions, DDL details,
and upsert syntax. Engines also differ in NULL ordering defaults and integer division.

Where you meet this: the project computes in DuckDB, and its `config.py` already lists PostgreSQL settings for a later stage.

| DuckDB (used here) | PostgreSQL | Portable choice |
|---|---|---|
| `QUALIFY row_number() OVER (...) = 1` | subquery with `WHERE rn = 1`, or `DISTINCT ON (key)` | subquery / CTE with `rn` |
| `a ANTI JOIN b USING (k)` | `NOT EXISTS (SELECT 1 FROM b WHERE ...)` | `NOT EXISTS` |
| `TRY_CAST(x AS DATE)` | no `TRY_CAST` (PostgreSQL 16+ has `pg_input_is_valid()` to test a cast first) | `CASE WHEN <valid pattern> THEN CAST(...) END` |
| `value ->> '$.a.b'` (JSONPath) | `jsonb ->> 'a'`, `#>> '{a,b}'`, `jsonb_path_query` | keep JSON parsing in the engine that owns it |
| `GROUP BY ALL`, `SELECT * EXCLUDE (c)`, `FROM t` | not available | list the columns explicitly |
| `list(x)` / `array_agg(x)` | `array_agg(x)` | `array_agg` (DuckDB accepts it) |
| `string_agg(x, ',' ORDER BY x)` | same | already portable |
| `x FILTER (WHERE ...)` | same | already portable (not in MySQL / SQL Server: use `CASE`) |
| `UNNEST(l) WITH ORDINALITY` | `unnest(arr) WITH ORDINALITY` (in a lateral `FROM`) | similar |
| `CREATE OR REPLACE TABLE t AS ...` | `DROP TABLE IF EXISTS t; CREATE TABLE t AS ...` | drop + create in a transaction |
| `strftime(ts, '%Y-%m')` | `to_char(ts, 'YYYY-MM')` | `date_trunc('month', ts)` |
| `7 / 2` is `3.5` | integer `7 / 2` is `3` | `CAST(7 AS DECIMAL) / 2` |

Strategies:
1. **Use the engine's strengths where they live.** Heavy flattening and parsing in DuckDB; tables for applications and
   upserts in PostgreSQL. Move typed data between them (Parquet, `COPY`), not DuckDB-specific SQL.
2. **Prefer standard forms when it costs nothing** (`NOT EXISTS`, CTEs, `CASE`, `array_agg`).
3. **Test on the target engine.** A query that parses in one engine can behave differently (NULL order, rounding).
4. **Tools**: `sqlglot` can translate between dialects, dbt adapters hide many differences, and ANSI-compliant subsets
   keep options open. Always review translated SQL.

## 2. Real-world case

The silver dedup uses `QUALIFY`, which PostgreSQL does not have. When the gold tables move to PostgreSQL, the same logic
must be rewritten as a subquery with `row_number()` (or `DISTINCT ON`). The orphan check uses `ANTI JOIN`, which becomes
`NOT EXISTS`. Because both forms return the same rows, the project can switch without changing results, as long as the
rewrite is tested. The portable forms below prove the equivalence on real sample data.

## 3. Popular usage

- Migrating a warehouse (Redshift to Snowflake, SQL Server to PostgreSQL) and rewriting dialect-specific SQL.
- Libraries and BI tools that generate SQL for many engines (they stick to the portable core).
- Prototyping in DuckDB (fast, local) and deploying the final logic on a production database.

## 4. Example code

```python
run_sql_file("setup_bronze.sql")
```

```sql
-- Sample tables for the comparisons.
CREATE TEMP TABLE versions AS
SELECT data.identifier AS id, run_id, TRY_CAST(data.latestVersion.lastUpdateTime AS TIMESTAMPTZ) AS updated
FROM bronze_meta;
CREATE TEMP TABLE all_ids AS SELECT * FROM (VALUES ('DS1'), ('DS2'), ('DS3'), ('DS4'), ('DS9')) AS t(id);
SELECT count(*) AS version_rows FROM versions;
```

```python
# DuckDB's QUALIFY versus the portable subquery: they must return exactly the same rows.
duck = con.sql("""
    SELECT id, run_id FROM versions
    QUALIFY row_number() OVER (PARTITION BY id ORDER BY updated DESC NULLS LAST, run_id DESC) = 1
    ORDER BY id""").fetchall()
portable = con.sql("""
    SELECT id, run_id
    FROM (SELECT *, row_number() OVER (PARTITION BY id ORDER BY updated DESC NULLS LAST, run_id DESC) AS rn FROM versions)
    WHERE rn = 1
    ORDER BY id""").fetchall()
print("QUALIFY == subquery:", duck == portable, "| rows:", len(duck))
```

```python
# ANTI JOIN versus NOT EXISTS: ids that never appear in any run (here only DS9).
duck = con.sql("SELECT id FROM all_ids ANTI JOIN versions USING (id) ORDER BY id").fetchall()
portable = con.sql("""SELECT id FROM all_ids a
                      WHERE NOT EXISTS (SELECT 1 FROM versions v WHERE v.id = a.id) ORDER BY id""").fetchall()
print("ANTI JOIN == NOT EXISTS:", duck == portable, "| result:", duck)
```

```sql
-- TRY_CAST has no PostgreSQL twin. A portable pattern: validate with a pattern first, then CAST only valid text.
-- (DuckDB: regexp_matches; PostgreSQL: the ~ operator. The CASE shape is the same.)
SELECT x,
       CASE WHEN regexp_matches(x, '^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$') THEN CAST(x AS DATE) END AS safe_date
FROM (VALUES ('2025-03-01'), ('2025-13-45'), ('not a date')) AS t(x);
```

```sql
-- DuckDB "friendly SQL" shortcuts: handy while exploring, not portable. The explicit form works everywhere.
SELECT id, count(*) AS n FROM versions GROUP BY ALL ORDER BY id;
```

```sql
-- The portable equivalent of the line above, and a portable array aggregate (array_agg) instead of DuckDB's list().
SELECT id, count(*) AS n, array_agg(run_id ORDER BY run_id) AS runs FROM versions GROUP BY id ORDER BY id;
```

```postgres
-- PostgreSQL versions of the DuckDB-specific pieces used in this project. (Not run here.)

-- latest row per key (instead of QUALIFY)
SELECT DISTINCT ON (id) id, run_id, updated FROM versions ORDER BY id, updated DESC NULLS LAST, run_id DESC;

-- JSON text extraction from a jsonb column (instead of value ->> '$.authorName.value')
SELECT value #>> '{0,authorName,value}' AS first_author FROM stg_fields WHERE type_name = 'author';

-- unnest with position, in a lateral FROM
SELECT f.global_id, a.pos, a.item -> 'authorName' ->> 'value' AS author_name FROM stg_fields f
CROSS JOIN LATERAL jsonb_array_elements(f.value) WITH ORDINALITY AS a(item, pos);

-- replace a table inside one transaction (instead of CREATE OR REPLACE TABLE)
BEGIN;
DROP TABLE IF EXISTS gold.datasets;
CREATE TABLE gold.datasets AS SELECT * FROM staging.datasets;
COMMIT;
```
