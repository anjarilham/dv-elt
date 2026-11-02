# 01 - Grain, keys and normalization

Level: foundation | In this project: the four child tables `silver_dataset_authors / _subjects / _keywords / _grants` | Needs: nothing

## 1. Theory

**Grain** is the answer to "what does ONE row of this table represent?". Say it in a sentence before
you write any query: "one row per dataset", "one row per (dataset, author)". Most wrong numbers in
reporting (inflated sums, duplicated rows) are grain mistakes.

**Keys** identify rows:
- Natural (business) key: a value that means something in the real world. Here: the DOI, `global_id`.
- Surrogate key: an id invented by a system. Here: Dataverse's numeric `dataset_id`, or an auto-increment.
- Composite key: several columns together, e.g. (`global_id`, `author_position`).
- A PRIMARY KEY is unique and never NULL. A FOREIGN KEY is a column that points at a key in another table.

**First normal form (1NF)**: every cell holds ONE value. A cell holding a list ("Doe, Jane; Roe, Rick")
breaks 1NF. The fix is a child table: one row per item, plus the parent's key. Second and third normal
form continue the idea: every column should depend on the whole key and nothing but the key, so a
fact (an author's affiliation, say) is stored once instead of copied into many rows.

**Trade-off**: normalized tables avoid duplicated facts and make filters exact. Wide, denormalized
tables need fewer joins and are faster to read. A common design keeps silver normalized and builds
wide tables for dashboards (gold) on top.

## 2. Real-world case

The Dataverse API returns authors as a list inside each dataset. Imagine the first version of the
silver table stored them as one text column, `'Doe, Jane; Roe, Rick'`. Then:
- "Which datasets did Ana write?" needs `LIKE '%Ana%'`, which also matches `Hanafi`.
- "How many authors per dataset?" needs string splitting.
- "Authors with an ORCID" has nowhere to put the ORCID.

The project splits every multi-valued field into its own table whose key states the grain:
(`global_id`, `author_position`), (`global_id`, `subject_position`), and so on.

## 3. Popular usage

- Orders and order lines; articles and tags; students and courses (a many-to-many needs a "bridge" table).
- Every warehouse fact table starts with a written grain statement ("one row per order line per day").
- dbt tests `unique` and `not_null` on the key columns are grain tests.

## 4. Example code

```sql
-- The "list in a cell" design: authors crammed into one text column.
CREATE TABLE datasets_flat AS
SELECT * FROM (VALUES
    ('DS1', 'Rice yield survey',    'Doe, Jane; Roe, Rick'),
    ('DS3', 'Fish landing records', 'Kim, Ana'),
    ('DS5', 'Market price series',  'Hanafi, Budi')
) AS t(global_id, title, authors);
```

```sql
-- "Which datasets did Ana write?"  ILIKE also matches the "ana" inside Hanafi: a wrong answer.
SELECT global_id, authors FROM datasets_flat WHERE authors ILIKE '%ana%' ORDER BY global_id;
```

```sql
-- Normalized: one row per (dataset, author). The primary key states the grain.
CREATE TABLE dataset_authors (
    global_id       VARCHAR NOT NULL,
    author_position INTEGER NOT NULL,
    author_name     VARCHAR NOT NULL,
    PRIMARY KEY (global_id, author_position)
);
INSERT INTO dataset_authors VALUES
    ('DS1', 1, 'Doe, Jane'), ('DS1', 2, 'Roe, Rick'), ('DS3', 1, 'Kim, Ana'), ('DS5', 1, 'Hanafi, Budi');
```

```sql
-- Exact match now: only the real Ana.
SELECT global_id FROM dataset_authors WHERE author_name = 'Kim, Ana';
```

```sql
-- Authors per dataset is a plain GROUP BY.
SELECT global_id, count(*) AS n_authors FROM dataset_authors GROUP BY global_id ORDER BY global_id;
```

```sql
-- A grain test: this must return 0 rows. (dbt calls it a "unique" test.)
SELECT global_id, author_position, count(*) AS n
FROM dataset_authors
GROUP BY ALL
HAVING count(*) > 1;
```

```python
# The primary key enforces the grain: a second row for the same (dataset, position) is rejected.
try:
    con.execute("INSERT INTO dataset_authors VALUES ('DS1', 1, 'Someone Else')")
except duckdb.ConstraintException as e:
    print("Rejected:", str(e).splitlines()[0])
```
