# 07 - CTEs, subqueries and recursion

Level: intermediate | In this project: `WITH scalars AS (...), pivoted AS (...)`, scalar subqueries in the last quality check | Needs: lessons 02, 04

## 1. Theory

A **CTE** (common table expression) names a query so later parts can use it like a table:
`WITH a AS (...), b AS (SELECT ... FROM a) SELECT ... FROM b`. It makes long queries read top to
bottom instead of inside out. Most engines (DuckDB included) inline a CTE, so it costs nothing extra
compared with a subquery; the point is readability and reuse.

Subquery kinds:
- **Scalar subquery**: returns exactly one value; usable wherever a value is: `(SELECT max(x) FROM t)`.
- **Derived table**: `FROM (SELECT ...) AS name`.
- **IN / EXISTS subquery**: membership tests (`EXISTS` is safer with NULLs, lesson 03).
- **Correlated subquery**: refers to a column of the outer query, so it is evaluated "per outer row"
  (the optimizer usually turns it into a join).

A **recursive CTE** walks hierarchies and graphs: an *anchor* query gives the starting rows, a
*recursive member* joins the CTE to itself to find the next level, and `UNION ALL` stacks the levels
until no new rows appear. Always make sure the recursion can end (no cycles, or add a depth limit).

## 2. Real-world case

1. The flatten SQL builds `scalars` (columns straight from the JSON) and `pivoted` (fields turned into
   columns) as two CTEs, then joins them. Each piece can be tested on its own.
2. The last quality check compares two numbers from different tables with two scalar subqueries.
3. Dataverse collections form a tree (root > institution > unit > sub-unit). The extractor takes a flat
   comma-separated `ALL_DATAVERSE_LIST`. A recursive CTE turns "this collection and everything below it"
   into that list, so a new sub-collection is picked up without editing the list by hand.

## 3. Popular usage

- Step-by-step data cleaning pipelines in dbt models (`with source as (...), renamed as (...)`).
- Organisation charts, category trees, bill-of-materials, folder paths (recursive CTE).
- "Compare to the overall number" with a scalar subquery or a one-row CTE.

## 4. Example code

```python
run_sql_file("setup_bronze.sql")   # bronze_meta: the raw sample records
```

```sql
-- A CTE chain: files per run, then each run's share of all files (the second CTE uses the first).
WITH per_run AS (
    SELECT run_id, sum(COALESCE(len(data.latestVersion.files), 0)) AS n_files
    FROM bronze_meta GROUP BY run_id
),
total AS (SELECT sum(n_files) AS all_files FROM per_run)
SELECT p.run_id, p.n_files, round(100.0 * p.n_files / t.all_files, 1) AS pct_of_all
FROM per_run p CROSS JOIN total t
ORDER BY p.run_id;
```

```sql
-- Scalar subqueries: reconcile two counts from the same table.
SELECT (SELECT count(*) FROM bronze_meta)                                              AS records,
       (SELECT count(DISTINCT data.identifier) FROM bronze_meta)                       AS distinct_datasets,
       (SELECT count(*) FROM bronze_meta) - (SELECT count(DISTINCT data.identifier) FROM bronze_meta) AS repeated;
```

```sql
-- Correlated subquery: datasets with more files than the average of THEIR OWN run.
SELECT b.run_id, b.data.identifier AS id, len(b.data.latestVersion.files) AS n_files
FROM bronze_meta b
WHERE len(b.data.latestVersion.files) >
      (SELECT avg(len(x.data.latestVersion.files)) FROM bronze_meta x WHERE x.run_id = b.run_id)
ORDER BY 1, 2;
```

```sql
-- A hierarchy: each collection points at its parent.
CREATE TABLE dataverses AS
SELECT * FROM (VALUES
    ('root', NULL), ('brin', 'root'),
    ('orpertanian', 'brin'), ('perikanan', 'brin'), ('kehutanan', 'brin'),
    ('hutan_mangrove', 'kehutanan')
) AS t(alias, parent);
```

```sql
-- Recursive CTE: "brin and everything below it", with the depth and the path.
WITH RECURSIVE tree AS (
    SELECT alias, 0 AS depth, alias AS path FROM dataverses WHERE alias = 'brin'       -- anchor
    UNION ALL
    SELECT d.alias, t.depth + 1, t.path || ' > ' || d.alias                              -- next level
    FROM dataverses d JOIN tree t ON d.parent = t.alias
)
SELECT depth, path FROM tree ORDER BY path;
```

```sql
-- The same tree as a flat list, ready for the ALL_DATAVERSE_LIST setting.
WITH RECURSIVE tree AS (
    SELECT alias FROM dataverses WHERE alias = 'brin'
    UNION ALL
    SELECT d.alias FROM dataverses d JOIN tree t ON d.parent = t.alias
)
SELECT string_agg(alias, ',' ORDER BY alias) AS all_dataverse_list FROM tree;
```
