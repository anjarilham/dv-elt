# 19 - Tables, views, temp tables and idempotent rebuilds

Level: intermediate | In this project: `CREATE OR REPLACE TEMP TABLE`, full rebuild per build id, `_SUCCESS` commit marker | Needs: nothing

## 1. Theory

Ways to store a query result:

| Object | Stores | Lives | Use for |
|---|---|---|---|
| `CREATE TABLE t AS SELECT ...` (CTAS) | the rows | until dropped | results other jobs read |
| `CREATE TEMP TABLE` | the rows | this connection only | working steps inside one job |
| `CREATE VIEW v AS SELECT ...` | only the query (re-run on every read) | until dropped | a named, always-fresh query |
| `CREATE OR REPLACE ...` | same as above, replacing any old one | | re-runnable scripts |

**Idempotent** means: running the job twice (or ten times) leaves the SAME result as running it once. Jobs get retried
(a crash at 03:00, a scheduler re-run), so a job that is not idempotent eventually corrupts data.
- NOT idempotent: a plain `INSERT` of the day's rows (a retry duplicates them).
- Idempotent: `CREATE OR REPLACE TABLE ... AS`, "delete the partition then insert it again", upsert (lesson 21).

**Transactions** make several statements all-or-nothing: `BEGIN; ...; COMMIT;` (or `ROLLBACK;` to undo). A common safe
pattern is the **atomic swap**: build `report_new` next to the live `report`, then in one transaction drop `report` and rename
`report_new`, so readers never see a half-built table.

**Files have no transactions.** A pipeline that writes many files cannot be atomic, so it uses a **commit marker**: write
all data files first, then the manifest, then an empty `_SUCCESS` file last. Readers only trust folders that have it.

## 2. Real-world case

The silver build always rebuilds everything from the raw layer under a fresh `build=<id>` folder, with `CREATE OR REPLACE`
for every working table. A retry cannot duplicate anything. And because upload of ~7 files is not atomic, `_SUCCESS` is written
last: a build that died half way has no `_SUCCESS`, so readers ignore it. The same reasoning appears in the raw layer, where
a run only gets `_SUCCESS` if EVERY dataverse succeeded.

## 3. Popular usage

- dbt materializations: `view`, `table`, `incremental`: the same menu as above.
- Airflow / cron retries: tasks must be safe to run again.
- Blue/green table swaps for dashboards that must never show a half-loaded table.

## 4. Example code

```sql
CREATE TABLE catalogue_log (run_id VARCHAR, n_datasets INTEGER);
```

```python
# A NON-idempotent job: it appends. The scheduler retries it after a failure, so the run is stored twice.
def nightly_job_with_insert():
    con.execute("INSERT INTO catalogue_log VALUES ('run_B', 4)")

nightly_job_with_insert()
nightly_job_with_insert()
print("append twice  ->", con.sql("SELECT run_id, count(*) AS copies FROM catalogue_log GROUP BY run_id").fetchall())
```

```python
# An idempotent job: it replaces. Run it twice, get the same single row.
def nightly_job_with_replace():
    con.execute("CREATE OR REPLACE TABLE catalogue_snapshot AS SELECT 'run_B' AS run_id, 4 AS n_datasets")

nightly_job_with_replace()
nightly_job_with_replace()
print("replace twice ->", con.sql("SELECT * FROM catalogue_snapshot").fetchall())
```

```sql
-- Idempotent append: delete the run's rows, insert them again, inside one transaction.
BEGIN;
DELETE FROM catalogue_log WHERE run_id = 'run_B';
INSERT INTO catalogue_log VALUES ('run_B', 4);
COMMIT;
SELECT run_id, count(*) AS copies FROM catalogue_log GROUP BY run_id;
```

```python
# ROLLBACK undoes everything since BEGIN: all-or-nothing.
con.execute("BEGIN")
con.execute("INSERT INTO catalogue_log VALUES ('run_C', 99)")
print("inside the transaction:", con.sql("SELECT count(*) FROM catalogue_log WHERE run_id = 'run_C'").fetchone()[0])
con.execute("ROLLBACK")
print("after ROLLBACK        :", con.sql("SELECT count(*) FROM catalogue_log WHERE run_id = 'run_C'").fetchone()[0])
```

```sql
-- A view re-runs its query every time; a table is a snapshot taken when it was created.
CREATE TABLE src (x INTEGER);
INSERT INTO src VALUES (1), (2);
CREATE VIEW v_src AS SELECT sum(x) AS total FROM src;
CREATE TABLE t_src AS SELECT sum(x) AS total FROM src;
INSERT INTO src VALUES (10);
SELECT (SELECT total FROM v_src) AS view_total, (SELECT total FROM t_src) AS table_total;
```

```python
# Atomic swap: build the new table next to the live one, then swap names inside one transaction.
con.execute("CREATE TABLE report AS SELECT 'old data' AS note")
con.execute("BEGIN")
con.execute("CREATE TABLE report_new AS SELECT 'new data' AS note")
con.execute("DROP TABLE report")
con.execute("ALTER TABLE report_new RENAME TO report")
con.execute("COMMIT")
print("after the swap:", con.sql("SELECT note FROM report").fetchone()[0])
```

```sql
-- What did we create? "temporary" is true for TEMP tables (only visible to this connection).
CREATE TEMP TABLE scratch AS SELECT 1 AS x;
SELECT table_name, temporary FROM duckdb_tables() ORDER BY table_name;
```
