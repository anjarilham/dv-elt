# 02 - Joins and cardinality

Level: foundation | In this project: child tables joined to `silver_datasets`, `LEFT JOIN pivoted`, `ANTI JOIN` checks | Needs: lesson 01

## 1. Theory

A join combines rows of two tables whose key columns match.

| Join | Keeps | Typical use |
|---|---|---|
| INNER | rows with a match on both sides | attach details to rows that must have them |
| LEFT | every left row, NULLs where there is no match | "keep all datasets even without authors" |
| RIGHT / FULL | every right row / every row of both | reconciling two lists |
| CROSS | every left row paired with every right row | calendars, grids, combinations |
| SEMI (`EXISTS`) | left rows that have a match, each once | "datasets that have at least one author" |
| ANTI (`NOT EXISTS`) | left rows that have NO match | orphans, missing data |

`ON a.k = b.k` states the condition. `USING (k)` is shorthand when both columns have the same name
and keeps only one copy of `k` in `SELECT *`.

**Cardinality** is how many rows match on each side: 1:1, 1:N, N:N. Joining to the "many" side
multiplies the "one" side's rows. This is called **fan-out**. If the join key is not unique on a
side, rows multiply silently and every `sum()` and `count(*)` after the join is inflated.

Before any join ask: "is my key unique on each side?" If not, add the missing column to the key.

## 2. Real-world case

`stg_fields` holds the fields of EVERY run, so dataset DS1 appears once per run. `silver_datasets`
keeps one version per dataset (the newest run). The first draft of the author table joined on
`global_id` only: each author appeared once per run (the old version AND the kept version). The fix
is to join on the whole version key, `global_id` AND `source_run_id`. It is the `ON` clause in every
child table of `flatten_datasets_metadata.sql`.

## 3. Popular usage

- LEFT JOIN from a "calendar spine" (CROSS JOIN of all months and all categories) so a report shows
  zeros instead of missing rows.
- Anti join for data quality: orphans, missing dimension members, "customers without orders".
- Semi join (`EXISTS`) to filter without multiplying rows.

## 4. Example code

```sql
-- Which version of each dataset was kept (run B), and every author row of every run.
CREATE TABLE kept_versions AS
SELECT * FROM (VALUES ('DS1', 'B'), ('DS3', 'B')) AS t(global_id, source_run_id);

CREATE TABLE author_rows AS
SELECT * FROM (VALUES
    ('DS1', 'A', 'Doe, Jane'), ('DS1', 'A', 'Roe, Rick'),   -- old version of DS1
    ('DS1', 'B', 'Doe, Jane'), ('DS1', 'B', 'Roe, Rick'),   -- kept version of DS1
    ('DS3', 'B', 'Kim, Ana')
) AS t(global_id, run_id, author_name);
```

```sql
-- WRONG: join on global_id only. DS1 has 2 authors but shows 4 rows (fan-out).
SELECT k.global_id, count(*) AS author_rows
FROM kept_versions k
JOIN author_rows a ON a.global_id = k.global_id
GROUP BY k.global_id ORDER BY 1;
```

```sql
-- RIGHT: join on the full version key. DS1 -> 2 authors.
SELECT k.global_id, count(*) AS author_rows
FROM kept_versions k
JOIN author_rows a ON a.global_id = k.global_id AND a.run_id = k.source_run_id
GROUP BY k.global_id ORDER BY 1;
```

```sql
CREATE TABLE ds AS SELECT * FROM (VALUES ('DS1'), ('DS2'), ('DS3')) AS t(global_id);
```

```sql
-- LEFT JOIN keeps DS2 even though it has no authors (its author_name is NULL).
SELECT d.global_id, a.author_name
FROM ds d
LEFT JOIN author_rows a ON a.global_id = d.global_id AND a.run_id = 'B'
ORDER BY 1, 2;
```

```sql
-- ANTI JOIN (DuckDB syntax): datasets with no author row at all. Portable form: NOT EXISTS.
SELECT global_id FROM ds ANTI JOIN author_rows USING (global_id);
```

```sql
-- SEMI JOIN via EXISTS: each dataset once, even though DS1 matches 4 author rows.
SELECT global_id FROM ds d
WHERE EXISTS (SELECT 1 FROM author_rows a WHERE a.global_id = d.global_id)
ORDER BY 1;
```

```sql
-- CROSS JOIN as a calendar spine: a zero-filled "files uploaded per dataverse per month" report.
CREATE TABLE months AS SELECT unnest(['2025-01', '2025-02', '2025-03']) AS month;
CREATE TABLE uploads AS
SELECT * FROM (VALUES ('orpertanian', '2025-01', 5), ('orpertanian', '2025-03', 2), ('perikanan', '2025-02', 7))
       AS t(dataverse, month, n_files);
```

```sql
SELECT dv.dataverse, m.month, COALESCE(u.n_files, 0) AS n_files
FROM (SELECT DISTINCT dataverse FROM uploads) dv
CROSS JOIN months m
LEFT JOIN uploads u ON u.dataverse = dv.dataverse AND u.month = m.month
ORDER BY 1, 2;
```
