# 04 - Logical order of execution

Level: foundation | In this project: why the dedup uses `QUALIFY`, why checks use `HAVING` | Needs: nothing

## 1. Theory

You WRITE `SELECT ... FROM ... WHERE ...`, but the database THINKS in this order:

```text
FROM / JOIN  ->  WHERE  ->  GROUP BY  ->  aggregates  ->  HAVING  ->  window functions
             ->  QUALIFY  ->  SELECT (expressions, aliases)  ->  DISTINCT  ->  ORDER BY  ->  LIMIT
```

Consequences you meet daily:
- `WHERE` runs before grouping, so it cannot contain aggregates. Filter groups with `HAVING`.
- `WHERE` runs before window functions, so it cannot filter on them. Use `QUALIFY` (DuckDB, Snowflake,
  BigQuery) or wrap the query in a subquery / CTE and filter outside.
- `WHERE` filters ROWS before they are counted, `HAVING` filters GROUPS after. Same condition, different meaning.
- Standard SQL does not let `WHERE` use a `SELECT` alias (it does not exist yet). DuckDB is lenient and
  allows it, but code that relies on it will not port to Postgres.
- `ORDER BY` runs last, so it can use aliases, and `LIMIT` applies after sorting.

## 2. Real-world case

Three questions about the dataset catalogue, one table, three different clauses:
1. "Which datasets appear in at least two runs?" is a condition on a group, so `HAVING count(*) >= 2`.
2. "How many versions had files?" filters rows first, so `WHERE n_files > 0`, then counts.
3. "Newest version of each dataset" filters on a window function, so `QUALIFY`. This is the dedup in
   `silver_datasets`.

Putting a condition in the wrong clause either fails with an error (good) or quietly returns a
different number (bad).

## 3. Popular usage

- Interview classic: explain why `WHERE count(*) > 1` fails.
- "Top N per group" in every analytics stack: window function in a CTE or `QUALIFY`.
- Reading query plans and optimizer behaviour starts from this logical order.

## 4. Example code

```sql
CREATE TABLE versions AS
SELECT * FROM (VALUES
    ('DS1', 'A', 2, '2025-01-01'),
    ('DS1', 'B', 3, '2025-06-01'),
    ('DS2', 'A', 0, NULL),
    ('DS3', 'B', 1, '2025-05-20'),
    ('DS3', 'A', 0, '2025-05-01')
) AS t(global_id, run_id, n_files, updated);
```

```sql
-- HAVING filters groups: datasets that appear in 2 or more runs.
SELECT global_id, count(*) AS n_runs FROM versions GROUP BY global_id HAVING count(*) >= 2 ORDER BY 1;
```

```sql
-- WHERE filters rows BEFORE they are counted: only versions that had files. Compare DS3: 1, not 2.
SELECT global_id, count(*) AS versions_with_files FROM versions WHERE n_files > 0 GROUP BY global_id ORDER BY 1;
```

```sql
-- QUALIFY filters on a window function: the newest version per dataset.
SELECT global_id, run_id, updated
FROM versions
QUALIFY row_number() OVER (PARTITION BY global_id ORDER BY run_id DESC) = 1
ORDER BY global_id;
```

```python
# The wrong placements fail loudly. Read the messages: they tell you which clause is wrong.
bad_queries = [
    "SELECT global_id FROM versions WHERE count(*) > 1 GROUP BY global_id",
    "SELECT global_id FROM versions WHERE row_number() OVER (PARTITION BY global_id ORDER BY run_id) = 1",
]
for sql in bad_queries:
    try:
        con.execute(sql)
    except duckdb.Error as e:
        print(str(e).splitlines()[0])
```

```sql
-- The portable way to filter on a window function: compute it in a subquery, filter outside.
SELECT global_id, run_id, updated
FROM (SELECT *, row_number() OVER (PARTITION BY global_id ORDER BY run_id DESC) AS rn FROM versions)
WHERE rn = 1
ORDER BY global_id;
```
