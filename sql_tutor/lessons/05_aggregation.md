# 05 - Aggregation: GROUP BY, FILTER, ROLLUP

Level: foundation to intermediate | In this project: `max(...) FILTER (WHERE ...)`, `count(DISTINCT ...)`, `HAVING count(*) > 1` | Needs: lesson 04

## 1. Theory

An aggregate collapses many rows into one value per group: `count`, `sum`, `avg`, `min`, `max`,
`string_agg`, `list`. `GROUP BY` defines the groups. Every column in `SELECT` must be either in
`GROUP BY` or inside an aggregate (DuckDB offers `GROUP BY ALL` to group by every non-aggregate column).

Useful details:
- Aggregates ignore NULLs (lesson 03). `count(*)` counts rows.
- `count(DISTINCT x)` counts different values. It is slower than `count(x)`.
- **Conditional aggregation**: `agg(...) FILTER (WHERE cond)` (standard SQL) or `agg(CASE WHEN cond THEN x END)`
  (works everywhere). It computes several differently-filtered numbers in ONE pass.
- **Ordered aggregates**: `string_agg(x, ', ' ORDER BY x)`, `list(x ORDER BY y)`.
- **Subtotals**: `GROUP BY ROLLUP (a, b)` adds subtotal rows (by a, then grand total). `GROUPING SETS` and
  `CUBE` choose other combinations. `GROUPING(col)` is 1 on rows where `col` was rolled up.
- **Safe ratios**: divide by `NULLIF(denominator, 0)`.

## 2. Real-world case

A data steward asks for a monthly report: per dataverse, how many datasets, how many files, how
many files are restricted, what share that is, with a total line. One query, no loops:
conditional counts for each restriction class, `NULLIF` so an empty dataverse does not divide by
zero, `ROLLUP` for the totals. The project uses the same building blocks: `FILTER` pivots fields into
columns (lesson 14), `HAVING count(*) > 1` finds duplicate keys (lesson 22).

## 3. Popular usage

- KPI dashboards: one row per day or category with several conditional counts.
- Funnel reports: `count(*) FILTER (WHERE step >= 2) / count(*)`.
- Finance-style reports with subtotals and grand totals (`ROLLUP`).
- `string_agg` / `list` to build comma-separated tag lists or arrays per entity.

## 4. Example code

```sql
CREATE TABLE dataset_summary AS
SELECT * FROM (VALUES
    ('DS1', 'orpertanian', 3, 1),
    ('DS2', 'orpertanian', 0, 0),
    ('DS3', 'orpertanian', 1, 0),
    ('DS6', 'perikanan',  10, 0),
    ('DS7', 'perikanan',   4, 4)
) AS t(global_id, dataverse, n_files, n_restricted);
```

```sql
-- Basic GROUP BY with several aggregates.
SELECT dataverse, count(*) AS n_datasets, sum(n_files) AS n_files, sum(n_restricted) AS n_restricted
FROM dataset_summary GROUP BY dataverse ORDER BY dataverse;
```

```sql
-- Conditional aggregation with FILTER, and the portable CASE form next to it (same numbers).
SELECT dataverse,
       count(*) FILTER (WHERE n_restricted > 0)                    AS datasets_with_restricted,
       sum(CASE WHEN n_restricted > 0 THEN 1 ELSE 0 END)           AS same_with_case,
       count(*) FILTER (WHERE n_files = 0)                         AS datasets_without_files
FROM dataset_summary GROUP BY dataverse ORDER BY dataverse;
```

```sql
-- A ratio that survives an empty group: NULLIF turns a zero denominator into NULL.
SELECT dataverse, round(100.0 * sum(n_restricted) / NULLIF(sum(n_files), 0), 1) AS pct_restricted
FROM dataset_summary GROUP BY dataverse ORDER BY dataverse;
```

```sql
-- HAVING keeps groups: only dataverses with 3 or more datasets (WHERE could not do this, lesson 04).
SELECT dataverse, count(*) AS n_datasets
FROM dataset_summary GROUP BY dataverse HAVING count(*) >= 3 ORDER BY dataverse;
```

```sql
-- ROLLUP adds a subtotal per dataverse and a grand total. GROUPING() tells total rows from data rows.
SELECT CASE WHEN grouping(dataverse) = 1 THEN '(all)' ELSE dataverse END AS dataverse,
       CASE WHEN grouping(dataverse) = 1 THEN '(grand total)'
            WHEN grouping(global_id) = 1 THEN '(subtotal)'
            ELSE global_id END AS dataset,
       sum(n_files) AS n_files
FROM dataset_summary
GROUP BY ROLLUP (dataverse, global_id)
ORDER BY grouping(dataverse), dataverse, grouping(global_id), dataset;
```

```sql
-- Ordered aggregates: a readable list of dataset ids per dataverse, and the same as a real list value.
SELECT dataverse,
       string_agg(global_id, ', ' ORDER BY global_id)       AS ids_text,
       list(global_id ORDER BY n_files DESC)                AS ids_by_size
FROM dataset_summary GROUP BY dataverse ORDER BY dataverse;
```
