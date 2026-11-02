# 03 - NULL semantics (three-valued logic)

Level: foundation | In this project: `has_custom_terms`, `dataset without a title` check, `NULLS LAST`, deaccessioned datasets | Needs: nothing

## 1. Theory

NULL means "unknown / missing", not zero and not empty text. Comparing anything with an unknown gives
an unknown, so SQL logic has THREE values: TRUE, FALSE and NULL (unknown).

- `x = NULL` and `x <> NULL` are never TRUE. Use `x IS NULL` / `x IS NOT NULL`.
- `WHERE` keeps only rows where the condition is TRUE. FALSE and NULL rows are both dropped.
- `NULL AND FALSE` is FALSE, `NULL AND TRUE` is NULL, `NULL OR TRUE` is TRUE, `NOT NULL` is NULL.
- Aggregates skip NULLs: `count(col)` counts non-NULL values, `count(*)` counts rows, `sum` and `avg` ignore NULLs.
- `NULL` in the list of `x NOT IN (...)` makes the whole condition NULL: no rows come back. Use `NOT EXISTS`.
- `IS DISTINCT FROM` / `IS NOT DISTINCT FROM` compare like `<>` / `=` but treat two NULLs as equal.
- Helpers: `COALESCE(a, b, ...)` first non-NULL; `NULLIF(a, b)` NULL if a = b; `ORDER BY ... NULLS FIRST|LAST`.

## 2. Real-world case

The Dataverse field `termsOfUse` is NULL for some datasets, `''` for some, and spaces for others.
The silver flag `has_custom_terms` must be TRUE only when real text exists. The naive
`terms <> ''` silently drops the NULL rows and wrongly keeps the whitespace ones. The project's
version is `COALESCE(trim(terms_of_use_raw), '') <> ''`.

The same care shows in the quality checks: "dataset without a title" must skip deaccessioned
datasets, whose title is legitimately NULL.

## 3. Popular usage

- Cleaning: `NULLIF(trim(col), '')` turns blank strings into NULL so later logic needs only `IS NULL`.
- Safe division: `x / NULLIF(y, 0)` returns NULL instead of an error.
- Change detection between two snapshots: `old IS DISTINCT FROM new`.
- Defaults in reports: `COALESCE(sum(x), 0)`.

## 4. Example code

```sql
CREATE TABLE terms AS
SELECT * FROM (VALUES
    ('DS1', ''),
    ('DS2', NULL),
    ('DS3', 'Cite the source.'),
    ('DS4', '   ')
) AS t(global_id, terms_of_use);
```

```sql
-- Naive: NULL (DS2) vanishes silently, and the whitespace-only DS4 is wrongly kept.
SELECT global_id FROM terms WHERE terms_of_use <> '' ORDER BY 1;
```

```sql
-- Correct: only DS3 has real terms.
SELECT global_id FROM terms WHERE COALESCE(trim(terms_of_use), '') <> '' ORDER BY 1;
```

```sql
-- The same idea with NULLIF: blank text becomes NULL, then one IS NOT NULL test is enough.
SELECT global_id, NULLIF(trim(terms_of_use), '') AS cleaned FROM terms ORDER BY 1;
```

```sql
-- count(*) counts rows, count(col) skips NULLs, count(DISTINCT col) counts distinct non-NULL values.
SELECT count(*) AS all_rows, count(terms_of_use) AS non_null, count(DISTINCT terms_of_use) AS distinct_values FROM terms;
```

```sql
-- Three-valued logic in one row (a-e are NULL, FALSE, TRUE, NULL, TRUE).
SELECT
    NULL AND TRUE              AS a,
    NULL AND FALSE             AS b,
    NULL OR TRUE               AS c,
    NULL = NULL                AS d,
    NULL IS NOT DISTINCT FROM NULL AS e;
```

```sql
CREATE TABLE blocked AS SELECT * FROM (VALUES ('DS2'), (NULL)) AS t(global_id);
```

```sql
-- NOT IN trap: one NULL in the subquery makes the answer empty.
SELECT global_id FROM terms WHERE global_id NOT IN (SELECT global_id FROM blocked);
```

```sql
-- NOT EXISTS gives the answer you wanted: everything except DS2.
SELECT global_id FROM terms t
WHERE NOT EXISTS (SELECT 1 FROM blocked b WHERE b.global_id = t.global_id)
ORDER BY 1;
```

```sql
-- Change detection: <> misses DS3 (NULL -> 'X'); IS DISTINCT FROM catches it and ignores DS2 (NULL -> NULL).
SELECT id, old_title, new_title,
       old_title <> new_title             AS changed_naive,
       old_title IS DISTINCT FROM new_title AS changed_correct
FROM (VALUES ('DS1', 'A', 'B'), ('DS2', NULL, NULL), ('DS3', NULL, 'X')) AS t(id, old_title, new_title)
ORDER BY id;
```
