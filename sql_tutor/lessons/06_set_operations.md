# 06 - Set operations: UNION, INTERSECT, EXCEPT

Level: foundation | In this project: `qa_checks` is eight SELECTs joined by `UNION ALL` | Needs: nothing

## 1. Theory

Set operations combine the ROWS of two queries that have the same number of columns (matched by
position, with compatible types):

| Operator | Result |
|---|---|
| `UNION ALL` | all rows of both, duplicates kept (cheap: no sorting or hashing) |
| `UNION` | all rows of both, duplicates removed |
| `INTERSECT` | rows that appear in both |
| `EXCEPT` | rows of the first that are NOT in the second (order matters) |

Joins combine COLUMNS side by side. Set operations stack ROWS on top of each other.

Rules of thumb:
- Default to `UNION ALL`. Use `UNION` only when you really want de-duplication, because it hides
  repeated rows and costs more.
- `INTERSECT` and `EXCEPT` treat two NULLs as equal (unlike `=`).
- DuckDB adds `UNION ALL BY NAME`: columns are matched by name, missing ones become NULL. It is handy
  when files have drifted schemas.

## 2. Real-world case

1. **Many checks, one table.** `qa_checks` has the same three columns for every check, so each check is
   a SELECT and `UNION ALL` stacks them. `UNION` would be wrong: two checks with the same name, severity
   and count would be merged into one.
2. **What changed between two extraction runs?** Datasets in run A that are missing from run B
   (deleted or hidden) are `A EXCEPT B`. Datasets new in B are `B EXCEPT A`. Datasets in both are `A INTERSECT B`.

## 3. Popular usage

- "Stack monthly tables / yearly partitions into one" with `UNION ALL`.
- Reconciling two systems: `EXCEPT` both ways until both sides are empty.
- Combining files whose columns drifted over time (`UNION ALL BY NAME`).

## 4. Example code

```python
run_sql_file("setup_bronze.sql")   # bronze_meta: 3 datasets in run A, 3 in run B
```

```sql
-- In run A but not in run B: what disappeared between the runs?
WITH a AS (SELECT data.identifier AS id FROM bronze_meta WHERE run_id LIKE '%aaaaaa'),
     b AS (SELECT data.identifier AS id FROM bronze_meta WHERE run_id LIKE '%bbbbbb')
SELECT id FROM a EXCEPT SELECT id FROM b;
```

```sql
-- In run B but not in run A: what is new?
WITH a AS (SELECT data.identifier AS id FROM bronze_meta WHERE run_id LIKE '%aaaaaa'),
     b AS (SELECT data.identifier AS id FROM bronze_meta WHERE run_id LIKE '%bbbbbb')
SELECT id FROM b EXCEPT SELECT id FROM a;
```

```sql
-- In both runs.
WITH a AS (SELECT data.identifier AS id FROM bronze_meta WHERE run_id LIKE '%aaaaaa'),
     b AS (SELECT data.identifier AS id FROM bronze_meta WHERE run_id LIKE '%bbbbbb')
SELECT id FROM a INTERSECT SELECT id FROM b ORDER BY id;
```

```sql
-- UNION ALL keeps every row (6); UNION removes duplicates (4 distinct datasets).
WITH a AS (SELECT data.identifier AS id FROM bronze_meta WHERE run_id LIKE '%aaaaaa'),
     b AS (SELECT data.identifier AS id FROM bronze_meta WHERE run_id LIKE '%bbbbbb')
SELECT (SELECT count(*) FROM (SELECT id FROM a UNION ALL SELECT id FROM b)) AS union_all_rows,
       (SELECT count(*) FROM (SELECT id FROM a UNION     SELECT id FROM b)) AS union_rows;
```

```sql
-- The qa_checks pattern: each check is one SELECT with the same columns; UNION ALL stacks them.
SELECT 'released dataset without citation fields' AS check_name, 'error' AS severity, count(*) AS bad_rows
FROM bronze_meta WHERE data.latestVersion IS NOT NULL AND len(data.latestVersion.metadataBlocks.citation.fields) = 0
UNION ALL
SELECT 'deaccessioned dataset', 'warn', count(*) FROM bronze_meta WHERE data.latestVersion IS NULL
UNION ALL
SELECT 'draft version', 'warn', count(*) FROM bronze_meta WHERE data.latestVersion.versionState = 'DRAFT';
```

```sql
-- UNION ALL BY NAME: match columns by name, fill the missing ones with NULL (schema drift).
SELECT 1 AS id, 'old file' AS source, 'x' AS title
UNION ALL BY NAME
SELECT 2 AS id, 'new file' AS source, 3 AS version;
```
