# 18 - Safe casting: CAST, TRY_CAST and validating raw values

Level: intermediate | In this project: `TRY_CAST(publication_date_raw AS DATE)` and the `_raw` columns in `stg_datasets_all` | Needs: lesson 03

## 1. Theory

Converting text to dates and numbers is where dirty data bites.
- `CAST(x AS type)` raises an error on the first value that does not fit, and the whole query fails.
- `TRY_CAST(x AS type)` returns NULL for values that do not fit. The query completes.
- `try(expr)` (DuckDB) wraps ANY expression: if it raises an error, the result is NULL.

Neither is "better": choose by what you want to happen to a bad value.
- Strict stage (you want to be stopped): `CAST`.
- Reporting stage (you want to survive and count): `TRY_CAST`.

**The raw / typed twin pattern.** Never throw the original text away. Keep the text (`publication_date_raw`) AND the
typed column (`publication_date`). Then "bad values" are exactly the rows where raw is NOT NULL and typed IS NULL, and a
quality check can count them. If you only keep the typed column, a NULL could mean "missing at the source" or "failed to parse"
and you cannot tell which.

Casting also has silent surprises: decimal text cast to an integer type is ROUNDED (`'12.5'` becomes 13) instead of
failing. Cast to `DECIMAL(p, s)` when the exact value matters.

Validation can be layered: a regular expression for the format (`^\d{4}-\d{2}-\d{2}$`), then `TRY_CAST` for real validity
(the format of `2025-13-45` is fine, but there is no month 13).

## 2. Real-world case

The sample run contains a dataset (DS3) whose `publicationDate` is `2025-13-45`. A plain `CAST` would have stopped the whole
silver build for one bad record. The project uses `TRY_CAST`, keeps `publication_date_raw` in the staging table, and a quality
check (severity "warn") reports `publication_date text that is not a valid date: 1`. The build still publishes, and the data
owner gets a precise list of what to fix at the source.

## 3. Popular usage

- Loading CSV and JSON where numbers arrive as text with stray characters.
- Data quality dashboards: "N values failed to parse in column X".
- Staging layers in dbt: `safe_cast` / `try_cast` macros.

## 4. Example code

```python
run_sql_file("setup_bronze.sql")
```

```sql
-- Raw text next to the typed value. DS3's date is invalid: the typed column becomes NULL, the raw text is kept.
SELECT data.identifier AS id,
       data.publicationDate                         AS publication_date_raw,
       TRY_CAST(data.publicationDate AS DATE)       AS publication_date
FROM bronze_meta WHERE run_id LIKE '%bbbbbb' ORDER BY id;
```

```sql
-- The quality check: raw present but typed NULL. Missing (NULL raw) is NOT counted as bad.
SELECT count(*) FILTER (WHERE data.publicationDate IS NOT NULL
                          AND TRY_CAST(data.publicationDate AS DATE) IS NULL)  AS bad_dates,
       count(*) FILTER (WHERE data.publicationDate IS NULL)                    AS missing_dates
FROM bronze_meta;
```

```python
# CAST stops at the first bad value; TRY_CAST and try() turn it into NULL.
try:
    con.execute("SELECT CAST('2025-13-45' AS DATE)")
except duckdb.Error as e:
    print("CAST      ->", str(e).splitlines()[0])
print("TRY_CAST  ->", con.sql("SELECT TRY_CAST('2025-13-45' AS DATE)").fetchone()[0])
print("try()     ->", con.sql("SELECT try(strptime('13/45/2025', '%m/%d/%Y'))").fetchone()[0])
```

```sql
-- Layered validation: the format check passes for 2025-13-45, only the real cast catches it.
SELECT x,
       regexp_matches(x, '^\d{4}-\d{2}-\d{2}$')                    AS format_ok,
       TRY_CAST(x AS DATE) IS NOT NULL                              AS really_a_date,
       CASE WHEN x IS NULL                               THEN 'missing'
            WHEN TRY_CAST(x AS DATE) IS NULL             THEN 'unparseable'
            ELSE 'ok' END                                          AS status
FROM (VALUES ('2025-03-01'), ('2025-13-45'), ('March 2025'), (NULL)) AS t(x);
```

```sql
-- Numbers: junk and '' become NULL. Careful: '12.5' as INTEGER is silently ROUNDED to 13, not rejected.
-- If the exact value matters, cast to DECIMAL.
SELECT x,
       TRY_CAST(x AS INTEGER)         AS as_integer,
       TRY_CAST(x AS DECIMAL(10, 2))  AS as_decimal
FROM (VALUES ('12'), ('12.5'), ('abc'), ('')) AS t(x);
```
