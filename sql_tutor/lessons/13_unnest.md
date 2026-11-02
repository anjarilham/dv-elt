# 13 - UNNEST: turning a list into rows

Level: intermediate | In this project: `UNNEST(...fields) AS t(f)` and `CROSS JOIN UNNEST(from_json(...)) WITH ORDINALITY` | Needs: lessons 11, 12

## 1. Theory

`UNNEST(list)` produces one output row per list element. It is how nested data becomes relational:
a dataset with 7 fields becomes 7 rows.

Forms:
- In `SELECT`: `SELECT unnest([10, 20, 30])` gives three rows.
- In `FROM`, correlated with an earlier table: `FROM t, UNNEST(t.list_col) AS u(item)`. The `UNNEST` is evaluated
  once per row of `t` (a *lateral* join, with no `LATERAL` keyword needed in DuckDB).
- `WITH ORDINALITY` adds the 1-based position of each element as an extra column: `UNNEST(l) WITH ORDINALITY AS u(item, pos)`.
- Unnesting a LIST of STRUCTs gives a struct per row; read its fields with dots: `item.field`.

Two properties you must know:
1. **The grain changes.** After the `UNNEST` one row means "one element", not "one parent". Parent columns repeat
   on every element row, so summing a parent column now over-counts (lesson 02, fan-out).
2. **A NULL or empty list produces ZERO rows, and the parent row disappears.** To keep parents without
   elements use `LEFT JOIN UNNEST(...) AS u(item) ON true`; the element columns are then NULL.

The reverse operation is the aggregate `list(x)` (or `array_agg`), which collects rows back into a list.

## 2. Real-world case

`metadataBlocks.citation.fields` is a LIST of {typeName, value} entries, and some values are themselves
lists (authors, subjects, keywords). Two `UNNEST` steps produce `stg_fields` (one row per field) and then the child tables
(one row per author, with `WITH ORDINALITY` supplying `author_position` so the order from the source is kept).

The same property explains an observation in the project: deaccessioned datasets have no `latestVersion`, so their field
list is NULL, so they produce no rows in `stg_fields` and no children. That is intended, but you have to know
it is happening. `LEFT JOIN UNNEST` is the tool when you must not lose the parent.

## 3. Popular usage

- Exploding tags, line items, events or API arrays into rows for filtering and joining.
- Splitting delimited text: `UNNEST(string_split(tags, ';'))`.
- Recreating a "numbers table" or date series: `UNNEST(generate_series(...))`.
- PostgreSQL has the same idea (`unnest(array)`, also `WITH ORDINALITY`); Spark calls it `explode`, BigQuery `UNNEST`.

## 4. Example code

```python
run_sql_file("setup_bronze.sql")   # bronze_meta: raw records, nested lists inside
```

```sql
-- Smallest example: one list becomes three rows.
SELECT unnest([10, 20, 30]) AS x;
```

```sql
-- Explode the field list: one row per (dataset, field). Note that DS2 (deaccessioned) is missing from the result.
SELECT data.identifier AS id, count(*) AS n_fields
FROM bronze_meta, UNNEST(data.latestVersion.metadataBlocks.citation.fields) AS t(f)
WHERE run_id LIKE '%bbbbbb'
GROUP BY id ORDER BY id;
```

```sql
-- LEFT JOIN UNNEST ... ON true keeps the parent: DS2 now appears with 0 fields.
SELECT data.identifier AS id, count(f.typeName) AS n_fields
FROM bronze_meta
LEFT JOIN UNNEST(data.latestVersion.metadataBlocks.citation.fields) AS t(f) ON true
WHERE run_id LIKE '%bbbbbb'
GROUP BY id ORDER BY id;
```

```sql
-- This is how the project builds stg_fields: one row per field, with the JSON value kept as JSON.
CREATE TEMP TABLE stg_fields AS
SELECT run_id,
       data.latestVersion.datasetPersistentId AS global_id,
       f.typeName AS type_name,
       f."value"  AS value
FROM bronze_meta, UNNEST(data.latestVersion.metadataBlocks.citation.fields) AS t(f);
SELECT type_name, count(*) AS n FROM stg_fields GROUP BY type_name ORDER BY type_name;
```

```sql
-- Second level: the "author" value is a JSON array. UNNEST it WITH ORDINALITY to keep the source order.
SELECT f.global_id,
       a.pos                                   AS author_position,
       a.item ->> '$.authorName.value'         AS author_name,
       a.item ->> '$.authorAffiliation.value'  AS affiliation
FROM stg_fields f
CROSS JOIN UNNEST(from_json(f.value, '["JSON"]')) WITH ORDINALITY AS a(item, pos)
WHERE f.type_name = 'author' AND f.run_id LIKE '%bbbbbb'
ORDER BY f.global_id, a.pos;
```

```sql
-- A list of STRUCTs: one row per file, struct fields read with dots.
SELECT data.identifier AS id, f.dataFile.filename AS filename, f.restricted, f.dataFile.filesize AS bytes
FROM bronze_meta, UNNEST(data.latestVersion.files) AS t(f)
WHERE run_id LIKE '%bbbbbb'
ORDER BY id, filename;
```

```sql
-- Delimited text to rows, and back to a list with list().
WITH tags AS (
    SELECT id, trim(tag) AS tag
    FROM (VALUES ('DS1', 'rice; yield'), ('DS3', 'fish')) AS t(id, tags),
         UNNEST(string_split(tags, ';')) AS u(tag)
)
SELECT id, count(*) AS n_tags, list(tag ORDER BY tag) AS tag_list FROM tags GROUP BY id ORDER BY id;
```

```sql
-- Grain check after an UNNEST: rows produced must equal the total of the list lengths.
SELECT (SELECT count(*) FROM bronze_meta, UNNEST(data.latestVersion.files) AS t(f)) AS rows_after_unnest,
       (SELECT sum(len(data.latestVersion.files)) FROM bronze_meta)                  AS sum_of_list_lengths;
```
