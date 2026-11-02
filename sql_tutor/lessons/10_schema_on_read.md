# 10 - Reading JSON files: schema-on-read and hive partitioning

Level: intermediate | In this project: section 1 of `flatten_datasets_metadata.sql` (`read_json(... columns = {...})`) | Needs: nothing

## 1. Theory

**Schema-on-write** (a normal database table): the structure is enforced when data is stored.
**Schema-on-read** (files in a lake): anything can be stored, and the structure is applied when you read.
The benefit is that ingestion never fails because the source changed. The cost is that reading, typing and
validating are now your job.

DuckDB's `read_json` can work two ways:
- `read_json_auto(path)` samples the file and GUESSES the columns and types. Fast to start, but the guess
  can change when the data changes (a column that was always a number meets one text value).
- `read_json(path, columns = {name: 'TYPE', ...})` DECLARES the shape you depend on. Keys missing from a
  record become NULL, keys you did not list are ignored, and a value of the wrong type fails loudly.
  This is "schema on read" done deliberately.

Other options you will use: `format = 'newline_delimited'` (one JSON document per line, JSONL),
`.gz` files are decompressed automatically, a LIST of paths or a glob reads many files at once, and
`filename = true` adds a column with the source file.

**Hive partitioning**: folder names of the form `key=value` (`run_id=20261012T...`, `dataverse=orpertanian`)
become columns when you pass `hive_partitioning = true`. DuckDB guesses their types (a date-looking
folder becomes DATE; a number-looking one becomes a number), so check them.

## 2. Real-world case

The Dataverse API output is not uniform: a DRAFT version has no `versionNumber` or `releaseTime`, a
deaccessioned dataset has no `latestVersion` at all, and a server upgrade can add keys. The flatten
script therefore declares exactly the STRUCT it needs. Everything else in the raw files is kept in
bronze but ignored here. The run id, extract date and dataverse come from the folder names, so no
column has to be added to the JSON when it is stored.

## 3. Popular usage

- Querying a data lake of JSON / CSV / Parquet files directly with SQL, no load step.
- Landing zones where the source may change: store raw, declare the schema at read time.
- Partitioned layouts (`year=2026/month=10/...`) so engines can skip folders (lesson 23).

## 4. Example code

```sql
-- Auto-detect: DuckDB guesses the top-level columns. (Hive columns come from the folder names.)
SELECT column_name, column_type
FROM (DESCRIBE SELECT * FROM read_json_auto(
    'sample_data/extract_date=*/run_id=*/entity=dataset_metadata/dataverse=*/*.jsonl',
    hive_partitioning = true));
```

```sql
-- Declared schema (a short version of the project's): only the keys we depend on.
-- DRAFT and deaccessioned datasets simply produce NULLs.
SELECT run_id, extract_date, dataverse,
       data.identifier                          AS id,
       data.latestVersion.versionState          AS state,
       data.latestVersion.versionNumber         AS version
FROM read_json(
    'sample_data/extract_date=*/run_id=*/entity=dataset_metadata/dataverse=*/*.jsonl',
    format = 'newline_delimited',
    hive_partitioning = true,
    columns = {
        status: 'VARCHAR',
        data: 'STRUCT(id BIGINT, identifier VARCHAR, publicationDate VARCHAR,
                      latestVersion STRUCT(versionState VARCHAR, versionNumber INTEGER, lastUpdateTime VARCHAR))'
    })
ORDER BY run_id, id;
```

```sql
-- filename = true records which file each row came from (lineage). A list of files works like a glob;
-- silver.py passes exactly the files named by the run manifests.
SELECT DISTINCT regexp_extract(filename, 'run_id=[^/]+', 0) AS run_folder, count(*) AS records
FROM read_json(
    ['sample_data/extract_date=2026-10-12/run_id=20261012T000000Z_aaaaaa/entity=dataset_metadata/dataverse=orpertanian/part-00000.jsonl'],
    format = 'newline_delimited', filename = true,
    columns = {status: 'VARCHAR'})
GROUP BY ALL;
```

```python
# Drift: line 3 has text where a number is declared. A declared schema fails LOUDLY (good: you notice).
import pathlib, tempfile
tmp = pathlib.Path(tempfile.mkdtemp())
path = tmp / "drift.jsonl"
path.write_text('{"id": 1, "n": 5, "extra": "x"}\n{"id": 2}\n{"id": 3, "n": "oops"}\n', encoding="utf-8")

try:
    con.sql(f"SELECT * FROM read_json('{path.as_posix()}', format='newline_delimited', "
            f"columns={{id: 'BIGINT', n: 'INTEGER'}})").fetchall()
except duckdb.Error as e:
    print("typed read failed ->", str(e).splitlines()[0].split(", in ")[-1][:100])

# The tolerant alternative: land the risky column as text, then TRY_CAST and count what failed (lesson 18).
rel = con.sql(f"SELECT * FROM read_json('{path.as_posix()}', format='newline_delimited', "
              f"columns={{id: 'BIGINT', n: 'VARCHAR'}})")
print(rel.project("id, n, TRY_CAST(n AS INTEGER) AS n_int").fetchall())
```
