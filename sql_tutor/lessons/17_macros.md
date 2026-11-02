# 17 - Macros: reusable SQL expressions

Level: intermediate | In this project: `CREATE OR REPLACE TEMP MACRO parse_ts(x)` | Needs: lessons 16, 18

## 1. Theory

A **macro** gives a name to a SQL expression (scalar macro) or to a whole query (table macro). At
query time the engine substitutes the macro body, so there is no function-call overhead and the optimizer sees through it.

```sql
CREATE MACRO name(a, b, c := default) AS expression;          -- scalar macro
CREATE MACRO name(a) AS TABLE SELECT ... WHERE col = a;       -- table macro, used as: FROM name(x)
```
- `CREATE OR REPLACE` makes re-runs safe; `TEMP` limits it to the session (nothing is stored in a database file).
- Parameters can have defaults and be passed by name (`digits := 3`).
- A parameter name hides a column of the same name inside the body, so choose parameter names that no column uses.

Alternatives and when to prefer them:
- **View**: a saved query without parameters. Use it for a fixed dataset.
- **Python UDF** (`con.create_function`): needs logic SQL cannot express, but runs row by row in Python: much slower.
- **Copy-paste**: the macro's enemy. Three copies of the same date logic WILL diverge.

## 2. Real-world case

Three silver columns (`create_time`, `last_update_time`, `release_time`) all need the same tricky parsing:
timestamps with a zone are converted to UTC, timestamps without one keep their clock time, junk becomes NULL
(lesson 16). Writing that `CASE` three times, plus once more in the quality check that counts unparseable
values, would invite four slightly different versions. The macro `parse_ts(x)` is defined once and every use is one short call.
The quality check calls the SAME macro, so "what counts as valid" cannot drift apart from "what was parsed".

## 3. Popular usage

- Shared business rules: "is this a valid email", "normalize country code", "fiscal quarter of a date".
- dbt macros (Jinja, a different mechanism) serve the same purpose across a whole warehouse.
- Parameterized reports as table macros.

## 4. Example code

```sql
-- The project's macro: zone-aware text -> UTC; no zone -> keep the clock time; invalid -> NULL.
CREATE OR REPLACE TEMP MACRO parse_ts(x) AS
    CASE
        WHEN x IS NULL THEN NULL
        WHEN regexp_matches(x, '(Z|[+-][0-9]{2}:?[0-9]{2})$')
            THEN TRY_CAST(x AS TIMESTAMPTZ) AT TIME ZONE 'UTC'
        ELSE TRY_CAST(x AS TIMESTAMP)
    END;
SELECT x AS raw_text, parse_ts(x) AS parsed
FROM (VALUES ('2025-02-10 09:00:00'), ('2025-06-01T00:00:00Z'), ('2025-05-20T08:30:00+07:00'),
             ('garbage'), (NULL)) AS t(x);
```

```sql
-- The macro does not depend on the session zone: the same query in a Jakarta session gives the same answer.
SET TimeZone = 'Asia/Jakarta';
SELECT x AS raw_text, parse_ts(x) AS parsed
FROM (VALUES ('2025-02-10 09:00:00'), ('2025-06-01T00:00:00Z'), ('2025-05-20T08:30:00+07:00')) AS t(x);
```

```sql
SET TimeZone = 'UTC';
-- A scalar macro with a default parameter and a safe division.
CREATE OR REPLACE MACRO pct(part, whole, digits := 1) AS round(100.0 * part / NULLIF(whole, 0), digits);
SELECT pct(1, 3) AS default_digits, pct(1, 3, digits := 3) AS named_digits, pct(1, 0) AS zero_whole;
```

```python
run_sql_file("setup_bronze.sql")
```

```sql
-- A table macro: a parameterized query used like a table (files of one dataset in the newest run).
CREATE OR REPLACE MACRO files_of(dataset_ident) AS TABLE
    SELECT data.identifier AS id, f.dataFile.filename AS filename, f.restricted AS restricted
    FROM bronze_meta, UNNEST(data.latestVersion.files) AS t(f)
    WHERE data.identifier = dataset_ident AND run_id LIKE '%bbbbbb';
SELECT * FROM files_of('DS1') ORDER BY filename;
```

```sql
-- Macros compose: use parse_ts inside a query like any function, here to count the unparseable values
-- (this is the quality-check pattern: it uses the SAME macro as the parsing).
SELECT count(*) FILTER (WHERE data.latestVersion.lastUpdateTime IS NOT NULL
                          AND parse_ts(data.latestVersion.lastUpdateTime) IS NULL) AS unparseable_last_update,
       count(*) FILTER (WHERE parse_ts(data.latestVersion.lastUpdateTime) IS NOT NULL)  AS parsed_ok
FROM bronze_meta;
```
