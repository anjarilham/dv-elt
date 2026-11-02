# 12 - JSON functions: -> ->> JSONPath, json_type, from_json

Level: intermediate | In this project: `value ->> '$'`, `item ->> '$.authorName.value'`, `from_json(f.value, '["JSON"]')` | Needs: lesson 11

## 1. Theory

A JSON value has six possible shapes: object `{}`, array `[]`, string, number, boolean and `null`.
You navigate it with a path written in JSONPath notation:

| Path | Means |
|---|---|
| `$` | the whole value |
| `$.key` | field `key` of an object |
| `$[0]` | first element of an array (JSON arrays are 0-based, unlike SQL lists) |
| `$.a.b[1].c` | chain them |
| `$[*]` | every element (returns a list) |

Operators and functions:
- `j -> path` returns JSON (keeps quotes around strings). `j ->> path` returns plain text (VARCHAR).
  `->>` `'$'` on a string unwraps it: `'"abc"'::JSON ->> '$'` is `abc`.
- `json_extract(j, path)` and `json_extract_string(j, path)` are the function forms.
- `json_type(j)` says what it is (`OBJECT`, `ARRAY`, `VARCHAR`, `BIGINT`, `NULL`, ...). Use it to branch on shape.
- `json_array_length(j)`, `json_keys(j)` inspect arrays and objects.
- `from_json(j, 'TYPE')` converts JSON into a typed value: `from_json(j, '["VARCHAR"]')` gives a `VARCHAR[]`;
  `'["JSON"]'` gives a list of JSON values you can `UNNEST` (lesson 13).
- A missing key does not fail: it returns SQL NULL. A JSON `null` is a real value, NOT a SQL NULL (see below).

## 2. Real-world case

The Dataverse field `value` has three shapes depending on the field:
- `title`, `depositor`: a plain string.
- `subject`: a list of strings.
- `author`, `keyword`, `grantNumber`, `dsDescription`: a list of objects whose real text sits one level
  deeper, in `.authorName.value`.

The project reads each shape with the matching call: `->> '$'` for strings, `from_json(..., '["VARCHAR"]')`
for string lists, `->> '$.authorName.value'` inside each object of a list. Checking `json_type` first
tells you which one applies.

## 3. Popular usage

- Semi-structured event payloads, API logs, webhook bodies stored in a JSON column (Postgres `jsonb`, BigQuery, Snowflake `VARIANT`).
- Pulling a few attributes out of a JSON blob into real columns during staging.
- Schema discovery: `json_type` and `json_keys` on unknown data.

## 4. Example code

```python
run_sql_file("setup_bronze.sql")
run_sql_file("setup_fields.sql")   # stg_fields: one row per dataset field, with a JSON "value" column
```

```sql
-- What shape does each field have? (Run it before writing extraction code.)
SELECT type_name, json_type(value) AS shape, count(*) AS n
FROM stg_fields GROUP BY ALL ORDER BY type_name, shape;
```

```sql
-- -> keeps JSON (strings keep their quotes). ->> returns text. ->> '$' unwraps a JSON string.
SELECT value                  AS as_json,
       value::VARCHAR         AS cast_text,
       value ->> '$'          AS unwrapped
FROM stg_fields
WHERE type_name = 'title' AND run_id LIKE '%bbbbbb'
ORDER BY global_id;
```

```sql
-- Paths into an array of objects: JSONPath arrays are 0-based. [*] returns all elements as a list.
SELECT global_id,
       json_array_length(value)                         AS n_authors,
       value ->> '$[0].authorName.value'                AS first_author,
       value ->> '$[1].authorName.value'                AS second_author,
       json_extract_string(value, '$[*].authorName.value') AS all_authors
FROM stg_fields
WHERE type_name = 'author' AND run_id LIKE '%bbbbbb'
ORDER BY global_id;
```

```sql
-- from_json turns a JSON array of strings into a real typed list.
SELECT global_id, from_json(value, '["VARCHAR"]') AS subjects, len(from_json(value, '["VARCHAR"]')) AS n_subjects
FROM stg_fields
WHERE type_name = 'subject' AND run_id LIKE '%bbbbbb'
ORDER BY global_id;
```

```sql
-- A missing key gives SQL NULL; a JSON null is a value of its own.
SELECT ('{"a": 1}'::JSON ->> '$.b') IS NULL      AS missing_key_is_sql_null,
       ('null'::JSON) IS NULL                    AS json_null_is_sql_null,
       '[' || json_type('null'::JSON) || ']'     AS json_null_type_as_text,
       ('{"a": null}'::JSON ->> '$.a') IS NULL   AS extracted_json_null_is_sql_null;
```
