# 11 - Nested types: STRUCT, LIST, MAP and JSON

Level: intermediate | In this project: the `data STRUCT(...)` column in `bronze_meta`; `files STRUCT(...)[]` | Needs: lesson 10

## 1. Theory

Modern analytic engines store nested data natively, so you do not have to flatten everything at load time.

| Type | What it is | Access | Literal |
|---|---|---|---|
| `STRUCT` | a record: fixed named fields, each with its own type | `s.field` (dot) | `{'a': 1, 'b': 'x'}` |
| `LIST` (array) | any number of values of ONE type | `l[1]` (1-based!), `l[-1]` (last), `l[1:2]` (slice), `len(l)` | `[1, 2, 3]` |
| `MAP` | key/value pairs with any keys | `m['key']` | `MAP {'k': 'v'}` |
| `JSON` | text that is valid JSON; shape is free | `->`, `->>` (lesson 12) | `'{"a":1}'::JSON` |

Rules of thumb:
- A STRUCT or LIST column can contain other STRUCTs and LISTs: `STRUCT(...)[]` is a list of records.
- Reaching into a NULL struct gives NULL (no error). Reaching past the end of a list gives NULL too.
- Use STRUCT / LIST when the shape is known and stable (fast, typed). Use JSON when the shape varies
  per row or you do not control it.
- Lists are 1-based in DuckDB (and in standard SQL arrays), unlike most programming languages.

## 2. Real-world case

One dataset record is a document: `data.latestVersion.license.name`, `data.latestVersion.files[...]`,
`data.latestVersion.metadataBlocks.citation.fields[...]`. Questions like "license name", "number of
files" or "name of the first file" do not need any join if you can walk into the structure with dots and
`len()`. The project keeps the nesting in `bronze_meta` and only flattens when a question needs rows
(lesson 13). The field `value` is JSON because in the API it is sometimes text, sometimes a list,
sometimes an object.

## 3. Popular usage

- Event and log data (nested payloads), API responses, Parquet files written by Spark / BigQuery.
- Returning several related values from one expression (`struct_pack`) without a helper table.
- Pre-aggregating a child table into a LIST column to avoid a join in dashboards.

## 4. Example code

```python
run_sql_file("setup_bronze.sql")   # bronze_meta.data is a STRUCT with nested STRUCTs and LISTs
```

```sql
-- Walk into the structure with dots; len() counts list items. DS2 (deaccessioned) has no latestVersion: all NULL.
SELECT data.identifier                                 AS id,
       data.latestVersion.license.name                 AS license,
       len(data.latestVersion.files)                   AS n_files,
       data.latestVersion.files[1].dataFile.filename   AS first_file,
       data.latestVersion.files[-1].dataFile.filename  AS last_file
FROM bronze_meta
WHERE run_id LIKE '%bbbbbb'
ORDER BY id;
```

```sql
-- typeof() shows the type of an expression: handy to learn an unfamiliar structure.
SELECT typeof(data.latestVersion.license)                       AS license_type,
       typeof(data.latestVersion.files) LIKE '%[]'               AS files_is_a_list,
       typeof(data.latestVersion.files[1].dataFile)              AS one_file_type
FROM bronze_meta LIMIT 1;
```

```sql
-- A STRUCT column can be expanded into one column per field with ".*" (name the struct first).
SELECT id, license.*
FROM (SELECT data.identifier AS id, data.latestVersion.license AS license
      FROM bronze_meta WHERE run_id LIKE '%bbbbbb')
ORDER BY id;
```

```sql
-- Literals: build STRUCT, LIST and MAP values by hand, and index into them.
SELECT s,
       s.a            AS struct_field,
       l[2]           AS second_item,
       l[2:3]         AS slice,
       m['k']         AS map_value
FROM (SELECT {'a': 1, 'b': 'x'} AS s, [10, 20, 30] AS l, MAP {'k': 'v'} AS m);
```

```sql
-- Build a STRUCT from columns, and a LIST from rows (list() is an aggregate).
SELECT run_id,
       list(data.identifier ORDER BY data.identifier)                                  AS ids,
       struct_pack(n_records := count(*), first_id := min(data.identifier))            AS summary
FROM bronze_meta GROUP BY run_id ORDER BY run_id;
```

```sql
-- JSON is for free-shaped values. The title field arrives as JSON text; casting shows the difference.
SELECT '"Rice yield"'::JSON AS json_value, ('"Rice yield"'::JSON)::VARCHAR AS as_text_with_quotes,
       json_type('"Rice yield"'::JSON) AS json_type;
```
