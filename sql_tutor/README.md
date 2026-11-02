# SQL tutor for the Dataverse ELT project

A self-study module with 25 lessons on the SQL used in this project (DuckDB dialect) and the theory around it.
Every lesson has the same four parts:

1. **Theory**: the base concepts and rules.
2. **Real-world case**: a problem from THIS project (or one it will meet next) that needed the technique.
3. **Popular usage**: where the technique shows up in industry.
4. **Example code**: runnable SQL / Python, executed live against a small sample shaped like your Dataverse records.

## Quick start

Run from the project folder, with the project's virtual environment (DuckDB is already installed there).

List the lessons:

```bash
.venv/Scripts/python sql_tutor/tutor.py
```

Read and run one lesson, by number or by part of its name (the code is executed and its result printed under each block):

```bash
.venv/Scripts/python sql_tutor/tutor.py 9
```

```bash
.venv/Scripts/python sql_tutor/tutor.py dedup
```

Read the text only, without running the code:

```bash
.venv/Scripts/python sql_tutor/tutor.py 9 --no-run
```

Check that every example in every lesson still runs (useful after editing a lesson or the project's SQL):

```bash
.venv/Scripts/python sql_tutor/tutor.py --check
```

You can also just open `lessons/*.md` in your editor; they are plain Markdown.

## PDF version

`SQL_Tutor.pdf` is the whole module in one file: cover, clickable contents with page numbers, PDF bookmarks, all 25 lessons
and the real result of every example (84 pages, A4). It is generated from the lessons, so rebuild it after editing one:

```bash
.venv/Scripts/python sql_tutor/make_pdf.py
```

`make_pdf.py` runs every example, converts the lessons to HTML and prints them to PDF with the Chrome or Edge already installed on the
machine. It needs no extra Python packages.

## Lessons

| # | Lesson | Level | Where it appears in this project |
|---|---|---|---|
| 01 | Grain, keys and normalization | foundation | child tables `silver_dataset_*` |
| 02 | Joins and cardinality | foundation | join on `global_id` + `source_run_id`, `ANTI JOIN` |
| 03 | NULL semantics | foundation | `has_custom_terms`, `NULLS LAST` |
| 04 | Logical order of execution | foundation | why `QUALIFY` and `HAVING` exist |
| 05 | Aggregation: GROUP BY, FILTER, ROLLUP | foundation | `max(...) FILTER`, duplicate checks |
| 06 | Set operations | foundation | `qa_checks` built with `UNION ALL` |
| 07 | CTEs, subqueries and recursion | intermediate | `scalars` / `pivoted` CTEs |
| 08 | Window functions | intermediate | `ROW_NUMBER() OVER (...)` |
| 09 | Latest-version dedup with QUALIFY | intermediate | `silver_datasets` |
| 10 | Reading JSON: schema-on-read, hive partitioning | intermediate | `read_json(... columns = {...})` |
| 11 | Nested types: STRUCT, LIST, MAP, JSON | intermediate | `bronze_meta.data` |
| 12 | JSON functions and JSONPath | intermediate | `->>`, `from_json` |
| 13 | UNNEST | intermediate | `stg_fields`, author table |
| 14 | EAV, pivot and unpivot | intermediate | the `pivoted` CTE |
| 15 | Lambdas and list functions | intermediate | `list_filter(files, f -> f.restricted)` |
| 16 | Types, time zones, number pitfalls | intermediate | `parse_ts`, `SET TimeZone = 'UTC'` |
| 17 | Macros | intermediate | `CREATE MACRO parse_ts` |
| 18 | Safe casting | intermediate | `TRY_CAST`, `_raw` columns |
| 19 | Tables, views, idempotent rebuilds | intermediate | `CREATE OR REPLACE`, `_SUCCESS` marker |
| 20 | Medallion layers and history (SCD 1 and 2) | advanced | bronze to silver, current-version-only silver |
| 21 | Incremental loads and upserts | advanced | next stage: loading PostgreSQL |
| 22 | Data quality as SQL | advanced | `qa_checks`, `check_quality()` |
| 23 | Performance and storage: Parquet, pushdown | advanced | Parquet export, `--runs latest` |
| 24 | Running SQL safely from Python | intermediate | `_sql_string`, `{{RAW_FILES}}` |
| 25 | Dialect portability: DuckDB vs PostgreSQL | advanced | `QUALIFY`, `ANTI JOIN`, JSONPath |

## Suggested path

1. **Foundations**: 01, 02, 03, 04, 05, 06. Everything else builds on these.
2. **Reshaping**: 07, 08, 09, then 10 to 15 (the semi-structured JSON toolbox), then 16 to 18 (types and casting).
3. **Engineering**: 19, 20, 21, 22 (how pipelines stay correct), 23 (how they stay fast), 24, 25 (safety and portability).

The lessons are independent: each runs in a fresh in-memory database, so you can jump to any of them.

## Map from the topic list to lessons

| Topic | Lesson(s) |
|---|---|
| CTAS and temp tables | 19 |
| Schema-on-read, hive partitioning | 10 |
| STRUCT / LIST / JSON types | 11 |
| UNNEST and ORDINALITY | 13 |
| JSON extraction | 12 |
| EAV and pivot with FILTER | 14, 05 |
| CTEs and recursion | 07 |
| Lambdas and list functions | 15 |
| Macros, time-zone handling | 17, 16 |
| Safe casting | 18 |
| Window functions, QUALIFY dedup | 08, 09 |
| Joining on the full version key | 02 |
| Anti join, HAVING, UNION ALL checks | 02, 05, 06, 22 |
| SQL from Python | 24 |
| Grain, keys, normalization | 01 |
| NULL semantics | 03 |
| Order of execution | 04 |
| Medallion, SCD, idempotency, incremental | 20, 19, 21 |
| Data quality | 22 |
| Performance and storage | 23 |
| Portability and PostgreSQL | 25 |

## The sample data

`sample_data/` holds two extraction runs of one dataverse (`orpertanian`) in the same folder layout as the raw bucket
(`extract_date=.../run_id=.../entity=dataset_metadata/dataverse=.../part-00000.jsonl`). Four datasets are chosen so each lesson has something to show:

| Dataset | What is special about it |
|---|---|
| DS1 | in both runs; title and file list changed in run B (dedup, history) |
| DS2 | deaccessioned: no `latestVersion` (NULL handling, the UNNEST gotcha) |
| DS3 | new in run B; invalid publication date `2025-13-45`; timestamp with a `+07:00` offset (safe casting, time zones) |
| DS4 | a DRAFT, only in run A (what disappeared between runs) |

`sample_data.py` regenerates the files if you delete the folder.

## How the code blocks run

Only the blocks under "## 4. Example code" are executed:
- ` ```sql ` blocks run in DuckDB; the result of the last statement is printed.
- ` ```python ` blocks run with these names available: `con` (the DuckDB connection), `duckdb`, `run_sql_file(name)`
  (runs a helper such as `setup_bronze.sql`), `build_silver()` (runs the project's real `flatten_datasets_metadata.sql`
  on the sample files) and `HERE` (this folder).
- Any other fence (` ```postgres `, ` ```text `) is shown but never run.

Helper files: `setup_bronze.sql` builds `bronze_meta`; `setup_fields.sql` builds `stg_fields` (needs `bronze_meta` first).

## Notes

- Lessons 20 and 22 run the project's actual `flatten_datasets_metadata.sql`. If you change that file, run `--check`: a failure there means the
  lesson text needs updating (or the SQL change broke something).
- Lesson 23 writes about 55 MB of temporary files (CSV, JSON, Parquet) and removes them when it finishes.
- All SQL targets the DuckDB version in your venv. Lesson 25 lists what changes for PostgreSQL.

## Adding a lesson

Create `lessons/NN_name.md` with a first line `# NN - Title`, the four `##` sections (the fourth must start with `## 4`),
and runnable blocks. Then run `--check`.
