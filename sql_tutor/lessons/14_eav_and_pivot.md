# 14 - EAV, pivot and unpivot

Level: intermediate | In this project: the `pivoted` CTE (`max(...) FILTER (WHERE type_name = 'title')`) | Needs: lessons 05, 12

## 1. Theory

Two shapes for the same information:

```text
WIDE (one column per attribute)            LONG / EAV (one row per attribute)
global_id | title | depositor               global_id | attribute | value
DS1       | ...   | ...                     DS1       | title     | ...
                                            DS1       | depositor | ...
```
**EAV** (entity-attribute-value) is flexible: new attributes need no schema change. It is hard to query:
filtering on two attributes needs two lookups. So analysis tables are usually WIDE.

**Pivot** (long to wide): group by the entity and take, for each attribute, the value of the matching row.
Three ways to write it:
1. **Conditional aggregation** (portable): `max(value) FILTER (WHERE attribute = 'title') AS title`,
   or `max(CASE WHEN attribute = 'title' THEN value END)`. `max()` is just a way to pick the single matching value.
2. **`PIVOT` statement** (DuckDB, Snowflake, Oracle, SQL Server): `PIVOT t ON attribute USING max(value) GROUP BY entity`.
   Convenient, but the output columns come from the data.
3. **Join the table to itself** once per attribute: works everywhere but is long and slow. Avoid.

**Unpivot** (wide to long) does the reverse: `UNPIVOT wide ON col1, col2 INTO NAME attribute VALUE value`.

Pitfall: a pivot silently hides duplicates. If an entity has TWO rows for `title`, `max()` quietly picks one.
Check the grain first: (entity, attribute) must be unique, otherwise the pivot is wrong.

## 2. Real-world case

The Dataverse citation block is EAV: a list of {typeName, value}. The silver table needs `title`,
`depositor` and `description` as columns so analysts can filter and group on them. The flatten script
pivots only the SINGLE-valued fields into columns, and sends the MULTI-valued ones (authors, subjects, ...)
to child tables (lesson 01). Rule of thumb: single-valued attribute -> column, multi-valued -> child table.

## 3. Popular usage

- Survey and form answers stored as rows, pivoted into one row per respondent.
- Key/value "settings" or "custom fields" tables turned into wide reporting tables.
- Month columns for finance-style reports (`PIVOT ... ON month`), and UNPIVOT to feed charts that want long data.

## 4. Example code

```python
run_sql_file("setup_bronze.sql")
run_sql_file("setup_fields.sql")   # stg_fields: the long (EAV) table, one row per dataset field
```

```sql
-- The long shape for one dataset in run B: one row per field.
SELECT type_name, type_class, left(value::VARCHAR, 45) AS value_preview
FROM stg_fields
WHERE global_id = 'doi:10.99999/DS1' AND run_id LIKE '%bbbbbb'
ORDER BY type_name;
```

```sql
-- Grain check BEFORE pivoting: (run, dataset, field) must be unique. Zero rows = safe to pivot.
SELECT run_id, global_id, type_name, count(*) AS n
FROM stg_fields GROUP BY ALL HAVING count(*) > 1;
```

```sql
-- Pivot with conditional aggregation (the project's way): one row per (run, dataset).
SELECT run_id, global_id,
       max(value ->> '$') FILTER (WHERE type_name = 'title')         AS title,
       max(value ->> '$') FILTER (WHERE type_name = 'depositor')     AS depositor,
       max(value ->> '$') FILTER (WHERE type_name = 'dateOfDeposit') AS date_of_deposit
FROM stg_fields
GROUP BY run_id, global_id
ORDER BY global_id, run_id;
```

```sql
-- The same result with DuckDB's PIVOT statement. The attribute values become the column names.
PIVOT (SELECT run_id, global_id, type_name, value ->> '$' AS v
       FROM stg_fields WHERE type_name IN ('title', 'depositor'))
ON type_name USING max(v)
GROUP BY run_id, global_id
ORDER BY global_id, run_id;
```

```sql
-- UNPIVOT: wide back to long (for example to feed a chart or to store as EAV).
UNPIVOT (SELECT 'DS1' AS id, 'Rice yield survey' AS title, 'Doe, Jane' AS depositor)
ON title, depositor
INTO NAME attribute VALUE value;
```

```sql
-- Why the grain check matters: an entity with TWO titles. max() hides one of them without any warning.
SELECT id,
       max(v) FILTER (WHERE attribute = 'title')            AS title_shown,
       count(*) FILTER (WHERE attribute = 'title')          AS titles_found
FROM (VALUES ('DS1', 'title', 'Alpha'), ('DS1', 'title', 'Beta'), ('DS1', 'depositor', 'Doe')) AS t(id, attribute, v)
GROUP BY id;
```
