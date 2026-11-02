# 08 - Window functions

Level: intermediate | In this project: `ROW_NUMBER() OVER (PARTITION BY global_id ORDER BY ...)` for de-duplication | Needs: lesson 04

## 1. Theory

A window function computes a value for each row using a set of related rows (its "window"), WITHOUT
collapsing them the way `GROUP BY` does. Every input row stays in the output.

```text
function(...) OVER (PARTITION BY <group>  ORDER BY <order>  <frame>)
```
- `PARTITION BY` splits rows into independent groups (omit it: one group for the whole table).
- `ORDER BY` orders the rows inside each group (needed by ranking and offset functions).
- The **frame** says which rows around the current one are included, e.g. `ROWS BETWEEN 2 PRECEDING AND CURRENT ROW`.
  With `ORDER BY` and no frame, an aggregate window is a running total from the start to the current row.

Families:
- **Ranking**: `row_number()` (always unique 1,2,3), `rank()` (ties share a number, then a gap: 1,1,3),
  `dense_rank()` (ties share, no gap: 1,1,2), `ntile(n)` (buckets).
- **Offset**: `lag(x, n)` / `lead(x, n)` read the previous / next row; `first_value`, `last_value`.
- **Aggregate as window**: `sum(x) OVER (...)`, `avg`, `count`, `min`, `max`.
- `WINDOW w AS (...)` names a window so several functions can share it.

Windows run after `WHERE`/`GROUP BY`/`HAVING`, so filter on them with `QUALIFY` or a subquery (lesson 04).

## 2. Real-world case

Every extraction run contains a full copy of the catalogue, so the same dataset appears once per run.
Questions that need "the other rows of the same dataset":
- "Did the number of files change since the previous run?" -> `lag()` over the dataset's runs.
- "Keep only the newest copy" -> `row_number()` and keep 1 (lesson 09; this is the silver dedup).
- "Running total of deposits per month, with a 3-month moving average" -> aggregate windows.
- "The two biggest datasets of each dataverse" -> `rank()` with `QUALIFY`.

## 3. Popular usage

- Dedup: "latest row per key" (the single most common use in data engineering).
- Month-over-month change (`lag`), running balances (`sum() OVER`), moving averages.
- Top-N per group, percentiles (`ntile`), sessionization (gaps between events with `lag`).
- Share of total: `x / sum(x) OVER ()`.

## 4. Example code

```python
run_sql_file("setup_bronze.sql")   # bronze_meta: 6 dataset records over 2 runs
```

```sql
-- Compare each dataset's file count with the same dataset in the previous run (lag).
WITH v AS (
    SELECT data.identifier AS id, run_id, COALESCE(len(data.latestVersion.files), 0) AS n_files
    FROM bronze_meta
)
SELECT id, run_id, n_files,
       lag(n_files) OVER w              AS prev_files,
       n_files - lag(n_files) OVER w    AS change
FROM v
WINDOW w AS (PARTITION BY id ORDER BY run_id)
ORDER BY id, run_id;
```

```sql
-- Ranking functions differ only on ties (DS1 and DS2 both have 10).
SELECT id, size,
       row_number() OVER (ORDER BY size DESC, id) AS row_number,
       rank()       OVER (ORDER BY size DESC)     AS rank,
       dense_rank() OVER (ORDER BY size DESC)     AS dense_rank
FROM (VALUES ('DS1', 10), ('DS2', 10), ('DS3', 7), ('DS4', 5)) AS t(id, size)
ORDER BY size DESC, id;
```

```sql
-- Aggregate windows: running total, 3-month moving average, share of the total.
SELECT month, deposits,
       sum(deposits) OVER (ORDER BY month)                                              AS running_total,
       round(avg(deposits) OVER (ORDER BY month ROWS BETWEEN 2 PRECEDING AND CURRENT ROW), 1) AS moving_avg_3,
       round(100.0 * deposits / sum(deposits) OVER (), 1)                               AS pct_of_total
FROM (VALUES ('2025-01', 4), ('2025-02', 6), ('2025-03', 5), ('2025-04', 9)) AS t(month, deposits)
ORDER BY month;
```

```sql
-- Top 2 datasets per dataverse: rank inside each partition, keep rank <= 2 with QUALIFY.
SELECT dataverse, id, n_files
FROM (VALUES ('orpertanian', 'DS1', 3), ('orpertanian', 'DS3', 1), ('orpertanian', 'DS2', 0),
             ('perikanan', 'DS6', 10), ('perikanan', 'DS7', 4)) AS t(dataverse, id, n_files)
QUALIFY rank() OVER (PARTITION BY dataverse ORDER BY n_files DESC) <= 2
ORDER BY dataverse, n_files DESC;
```
