# 15 - Lambdas and list functions

Level: intermediate | In this project: `len(list_filter(data.latestVersion.files, f -> f.restricted))` | Needs: lesson 11

## 1. Theory

A **lambda** is a tiny unnamed function you pass to another function: `x -> x * 2` means "given x, return x times 2".
DuckDB list functions accept lambdas to work on every element of a LIST without turning it into rows:

| Function | Does |
|---|---|
| `list_transform(l, x -> expr)` | map: a new list with `expr` applied to each element |
| `list_filter(l, x -> cond)` | keep the elements where `cond` is true |
| `list_reduce(l, (a, b) -> expr)` | fold the list into one value |
| `list_sum`, `list_min`, `list_max`, `list_avg`, `list_aggregate(l, 'name')` | aggregate a list directly |
| `list_sort`, `list_distinct`, `list_contains(l, v)`, `list_concat`, `len(l)` | the everyday helpers |
| `array_to_string(l, sep)`, `string_split(s, sep)` | list to text and back |

Syntax: the arrow form `x -> expr` is the long-standing one (this project uses it). Recent DuckDB versions
also accept the Python-style `lambda x: expr`, which avoids confusion with the JSON `->` operator. Both work in
the DuckDB installed here.

When to use which approach:
- **Lambda / list function**: the answer is one value per parent row (a count, a sum, a flag). It keeps the grain.
  No fan-out, no GROUP BY.
- **UNNEST + GROUP BY** (lesson 13): you need to filter, join or group the elements, or add them to other tables.

## 2. Real-world case

For each dataset the silver table needs `n_files` and `n_restricted_files`. The files are a LIST of structs
inside the record. Instead of exploding all files into rows and grouping back (which loses datasets with no
files unless you use a LEFT JOIN, lesson 13), the SQL counts with
`len(list_filter(files, f -> f.restricted))`: one expression, one row per dataset, empty lists give 0.

## 3. Popular usage

- Counting or summing inside nested arrays (items in an order, tags on a post).
- Cleaning lists: `list_distinct(list_sort(...))`, removing NULLs with `list_filter(l, x -> x IS NOT NULL)`.
- Building display strings: `array_to_string(list_transform(...), ', ')`.
- The same idea exists as `transform`, `filter`, `aggregate` in Spark / Trino / Presto SQL.

## 4. Example code

```python
run_sql_file("setup_bronze.sql")
```

```sql
-- Literals first: map, filter and fold on plain lists.
SELECT list_transform([1, 2, 3], x -> x * 10)         AS times_ten,
       list_filter([1, 2, 3, 4], x -> x % 2 = 0)      AS evens,
       list_reduce([1, 2, 3, 4], (a, b) -> a * b)     AS product;
```

```sql
-- The project's pattern: count restricted files without exploding. DS2 (no latestVersion) gives NULL.
SELECT data.identifier AS id,
       len(data.latestVersion.files)                                     AS n_files,
       len(list_filter(data.latestVersion.files, f -> f.restricted))     AS n_restricted
FROM bronze_meta WHERE run_id LIKE '%bbbbbb' ORDER BY id;
```

```sql
-- The same with the Python-style lambda syntax (recent DuckDB versions): identical result.
SELECT data.identifier AS id,
       len(list_filter(data.latestVersion.files, lambda f: f.restricted)) AS n_restricted
FROM bronze_meta WHERE run_id LIKE '%bbbbbb' ORDER BY id;
```

```sql
-- Transform: a list of file names, total size, and the distinct content types, still one row per dataset.
SELECT data.identifier AS id,
       list_transform(data.latestVersion.files, f -> f.dataFile.filename)                          AS file_names,
       list_sum(list_transform(data.latestVersion.files, f -> f.dataFile.filesize))                AS total_bytes,
       list_sort(list_distinct(list_transform(data.latestVersion.files, f -> f.dataFile.contentType))) AS content_types
FROM bronze_meta WHERE run_id LIKE '%bbbbbb' ORDER BY id;
```

```sql
-- Test membership and build text: does the dataset ship a codebook? Names as one comma-separated string.
SELECT data.identifier AS id,
       list_contains(list_transform(data.latestVersion.files, f -> f.dataFile.filename), 'codebook.pdf') AS has_codebook,
       array_to_string(list_transform(data.latestVersion.files, f -> f.dataFile.filename), ', ')         AS file_list
FROM bronze_meta WHERE run_id LIKE '%bbbbbb' ORDER BY id;
```

```python
# Both approaches must agree. The UNNEST way needs a LEFT JOIN, otherwise datasets without files vanish.
lambda_way = con.sql("""
    SELECT data.identifier AS id, run_id,
           COALESCE(len(list_filter(data.latestVersion.files, f -> f.restricted)), 0) AS n
    FROM bronze_meta ORDER BY run_id, id""").fetchall()
unnest_way = con.sql("""
    SELECT data.identifier AS id, run_id, count(*) FILTER (WHERE f.restricted) AS n
    FROM bronze_meta LEFT JOIN UNNEST(data.latestVersion.files) AS t(f) ON true
    GROUP BY data.identifier, run_id ORDER BY run_id, id""").fetchall()
print("same answer:", lambda_way == unnest_way, "| rows:", len(lambda_way))
```
