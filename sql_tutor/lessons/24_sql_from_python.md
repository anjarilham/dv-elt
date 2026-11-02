# 24 - Running SQL safely from Python: parameters, quoting, templates

Level: intermediate | In this project: `_sql_string()`, the `{{RAW_FILES}}` substitution in `build_silver_tables`, `SET s3_...` in `connect_duckdb` | Needs: nothing

## 1. Theory

SQL is text. If you build that text by pasting untrusted values into it, the value can change the meaning of the statement:
**SQL injection**. The defence is to keep code and data separate.

1. **Parameters** (prepared statements): `execute("... WHERE title = ?", [value])` or named `$name` with a dict. The database
   receives the value separately and never parses it as SQL. Use them for every VALUE in `WHERE`, `INSERT`, `VALUES`.
2. **Quoting literals**: some places cannot take parameters (`SET option = ...`, `COPY ... TO 'path'`, file paths in `read_json`).
   There, escape the value for SQL: wrap in single quotes and double any single quote inside (`O'Brien` -> `'O''Brien'`).
3. **Identifiers** (table and column names) cannot be parameters either. Take them from a fixed list (whitelist), or quote them
   with double quotes and double any double quote inside. Never paste user text as an identifier.
4. **Templates**: replacing a placeholder in a SQL file is fine when the replacement is built from trusted, quoted pieces, and
   when you check that the placeholder was found exactly the expected number of times.
5. **Secrets**: credentials end up in SQL strings (`SET s3_secret_access_key = ...`). Do not log those statements.
   DuckDB's `CREATE SECRET` keeps them out of ad-hoc statements.

`executemany(sql, rows)` runs one parameterized statement for many rows; it is also much faster than a Python loop of formatted strings.

## 2. Real-world case

`silver.py` must give DuckDB a list of raw file paths taken from the run manifests, and values for the S3 settings. Those cannot be
bound as parameters, so `_sql_string()` quotes them, the table names come from a constant list (`SILVER_TABLES`), and
`build_silver_tables` refuses to run unless `{{RAW_FILES}}` appears exactly once outside comments. A dataverse alias or path
containing an apostrophe would otherwise break the statement, or in the worst case change it.

## 3. Popular usage

- Every web application and ETL script that filters on user input.
- ORMs and drivers always use parameters under the hood; hand-written f-string SQL is the usual source of vulnerabilities.
- Parameterized dashboards: the BI tool binds the filter values.

## 4. Example code

```python
# 1) The classic injection. The user types a value that turns the WHERE into "always true".
con.execute("CREATE TABLE datasets AS SELECT * FROM (VALUES ('Rice yield survey'), ('Fish landing records'), ('Soil moisture sensors')) AS t(title)")
user_input = "x' OR '1'='1"

unsafe_sql = f"SELECT title FROM datasets WHERE title = '{user_input}'"
print("statement  :", unsafe_sql)
print("f-string   :", con.sql(unsafe_sql).fetchall())                                   # every row leaks
print("parameter  :", con.execute("SELECT title FROM datasets WHERE title = ?", [user_input]).fetchall())  # no match
```

```python
# 2) Named parameters and executemany.
print(con.execute("SELECT $a + $b AS total", {"a": 40, "b": 2}).fetchall())
con.execute("CREATE TABLE tags (global_id VARCHAR, tag VARCHAR)")
con.executemany("INSERT INTO tags VALUES (?, ?)", [("DS1", "rice"), ("DS1", "yield"), ("DS3", "fish")])
print(con.sql("SELECT global_id, count(*) AS n FROM tags GROUP BY global_id ORDER BY 1").fetchall())
```

```python
# 3) Where parameters are impossible (paths, SET values): quote the literal, as silver._sql_string does.
def sql_string(value):
    return "'" + str(value).replace("'", "''") + "'"

path = "s3://raw/dataverse_search/O'Brien/part-00000.jsonl.gz"
print("quoted literal:", sql_string(path))
print("round trip    :", con.sql(f"SELECT {sql_string(path)} AS p").fetchone()[0] == path)
```

```python
# 4) Identifiers: whitelist them. Never paste user text where a table name goes.
ALLOWED_TABLES = {"datasets", "tags"}

def count_rows(table):
    if table not in ALLOWED_TABLES:
        raise ValueError(f"table not allowed: {table!r}")
    return con.sql(f'SELECT count(*) FROM "{table}"').fetchone()[0]

print("datasets:", count_rows("datasets"))
try:
    count_rows("datasets; DROP TABLE datasets")
except ValueError as e:
    print("rejected:", e)
```

```python
# 5) A checked template, like build_silver_tables: replace the placeholder only on code lines, and insist on exactly one.
def fill_template(template, files):
    file_list = "[" + ", ".join(sql_string(f) for f in files) + "]"
    lines, replaced = [], 0
    for line in template.splitlines():
        if not line.lstrip().startswith("--") and "{{RAW_FILES}}" in line:
            line = line.replace("{{RAW_FILES}}", file_list)
            replaced += 1
        lines.append(line)
    if replaced != 1:
        raise ValueError(f"placeholder must appear exactly once outside comments (found {replaced})")
    return "\n".join(lines)

template = "-- reads {{RAW_FILES}} (comment: left alone)\nSELECT * FROM read_json({{RAW_FILES}}, format = 'newline_delimited')"
print(fill_template(template, ["a.jsonl", "b'c.jsonl"]))
try:
    fill_template("SELECT 1 -- no placeholder here", [])
except ValueError as e:
    print("rejected:", e)
```
