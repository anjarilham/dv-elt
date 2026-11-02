# 16 - Types, time zones and number pitfalls

Level: intermediate | In this project: `parse_ts()` macro, `SET TimeZone = 'UTC'` in `silver.py`, `TIMESTAMP` columns in silver | Needs: nothing

## 1. Theory

Date and time types:

| Type | Holds | Notes |
|---|---|---|
| `DATE` | a calendar day | no time, no zone |
| `TIMESTAMP` | a clock reading, e.g. `2025-05-20 08:30:00` | "naive": says nothing about WHICH moment it is |
| `TIMESTAMPTZ` | an exact instant (stored as UTC) | shown in the session `TimeZone`; the same instant looks different per zone |
| `INTERVAL` | a duration (`INTERVAL 30 DAY`) | used in date arithmetic |

Conversion rules (the source of most bugs):
- Text WITH a zone (`...Z`, `...+07:00`) can become a `TIMESTAMPTZ` exactly.
- Text WITHOUT a zone cast to `TIMESTAMPTZ` is interpreted in the SESSION time zone: the same text means a different
  instant on a laptop in Jakarta than on a server in UTC.
- `ts AT TIME ZONE 'UTC'` converts between the two kinds: `TIMESTAMPTZ -> TIMESTAMP` gives the clock reading in UTC;
  `TIMESTAMP -> TIMESTAMPTZ` says "this clock reading was in UTC".
- Safe habits: store instants in UTC, set the session zone explicitly, and never let the machine's zone decide a result.

Date helpers: `date_trunc('month', x)`, `date_diff('day', a, b)`, `x + INTERVAL 30 DAY`, `strftime(x, '%Y-%m')`,
`strptime(text, '%d/%m/%Y')`.

Numbers:
- `DOUBLE` / `FLOAT` are approximate binary numbers: `0.1 + 0.2` is not exactly `0.3`. Use `DECIMAL(p, s)` for money and
  anything that must add up exactly.
- In DuckDB `/` is true division (`7 / 2 = 3.5`) and `//` is integer division (`7 // 2 = 3`). Other databases differ
  (PostgreSQL's `7 / 2` is 3 for integers).
- Use `BIGINT` for ids and counts.

## 2. Real-world case

Dataverse sends `lastUpdateTime` in THREE formats: `2025-02-10 09:00:00` (no zone), `2025-06-01T00:00:00Z` (UTC) and
`2025-05-20T08:30:00+07:00` (an offset). The silver table needs one comparable timestamp, because the dedup
(lesson 09) orders by it. If the naive text were cast to `TIMESTAMPTZ`, a nightly job on a UTC server and a developer
laptop in Jakarta would produce different data, and monthly counts would change near month ends. The project defends
itself twice: `silver.py` sets `TimeZone = 'UTC'`, and the `parse_ts()` macro (lesson 17) never lets the session zone
decide.

## 3. Popular usage

- Reporting by day or month (`date_trunc`), cohort and retention analysis, SLA calculations.
- Storing event times from many regions as UTC and converting only for display.
- Money columns as `DECIMAL`, never `DOUBLE`.

## 4. Example code

```sql
-- The same TEXT, two session zones: the instants differ by 7 hours (shown here in UTC).
SET TimeZone = 'UTC';
SELECT '2025-02-01 03:00:00'::TIMESTAMPTZ AS instant, ('2025-02-01 03:00:00'::TIMESTAMPTZ) AT TIME ZONE 'UTC' AS as_utc_clock;
```

```sql
SET TimeZone = 'Asia/Jakarta';
SELECT '2025-02-01 03:00:00'::TIMESTAMPTZ AS instant, ('2025-02-01 03:00:00'::TIMESTAMPTZ) AT TIME ZONE 'UTC' AS as_utc_clock;
```

```sql
-- Consequence (the session is still Jakarta): the monthly bucket of that one value is JANUARY here,
-- but it would be February on a UTC server. The same data gives different monthly counts.
SELECT date_trunc('month', ('2025-02-01 03:00:00'::TIMESTAMPTZ) AT TIME ZONE 'UTC') AS month_bucket_in_jakarta_session;
```

```sql
-- Text WITH a zone is unambiguous in any session: both values below are fixed moments in time,
-- and +07:00 is converted to UTC (08:30 at +07:00 is 01:30 UTC).
SELECT x, (x::TIMESTAMPTZ) AT TIME ZONE 'UTC' AS utc_clock
FROM (VALUES ('2025-06-01T00:00:00Z'), ('2025-05-20T08:30:00+07:00')) AS t(x);
```

```sql
-- Back to a deterministic session for the rest of the lesson.
SET TimeZone = 'UTC';
SELECT current_setting('TimeZone') AS session_zone;
```

```sql
-- Everyday date helpers.
SELECT date_trunc('month', DATE '2025-05-20')                       AS month_start,
       DATE '2025-05-20' + INTERVAL 30 DAY                          AS plus_30_days,
       date_diff('day', DATE '2025-01-01', DATE '2025-05-20')       AS days_between,
       strftime(TIMESTAMP '2025-05-20 08:30:00', '%Y-%m')           AS year_month,
       strptime('20/05/2025', '%d/%m/%Y')                           AS parsed_text;
```

```sql
-- Numbers: DOUBLE is approximate, DECIMAL is exact; / is true division in DuckDB, // is integer division.
SELECT (0.1::DOUBLE + 0.2::DOUBLE) = 0.3::DOUBLE                                  AS double_equal,
       (0.1::DECIMAL(10, 2) + 0.2::DECIMAL(10, 2)) = 0.3::DECIMAL(10, 2)          AS decimal_equal,
       7 / 2 AS slash, 7 // 2 AS integer_division;
```
