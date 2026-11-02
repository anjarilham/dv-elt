-- =============================================================================
-- flatten_datasets_metadata.sql         (DuckDB)
--
-- BRONZE -> SILVER for the Dataverse "dataset_metadata" entity.
--
--   bronze: one JSON document per dataset, exactly as the API sent it (nested)
--   silver: flat, typed, de-duplicated tables
--             silver_datasets          1 row per dataset
--             silver_dataset_authors   1 row per (dataset, author)
--             silver_dataset_subjects  1 row per (dataset, subject)
--             silver_dataset_keywords  1 row per (dataset, keyword)
--             silver_dataset_grants    1 row per (dataset, grant)
--
-- One placeholder is replaced by the caller (silver.py) before running:
--   {{RAW_FILES}}  a SQL list of the raw files to read, e.g.
--                  ['s3://raw/dataverse_search/extract_date=2026-10-13/run_id=<RUN_ID>/entity=dataset_metadata/dataverse=<NAME>/part-00000.jsonl.gz', ...]
--                  The caller builds it from the run manifests, so only files the manifest
--                  vouches for are read. It must appear exactly once outside comments.
--
-- This script only builds tables in the DuckDB session. silver.py writes them to Parquet and
-- uploads them, after the quality gate (qa_checks, section 6) has passed.
--
-- Notes learned from the real data:
--   * Timestamps arrive in two formats: '2024-12-19 06:20:56' (no time zone) and
--     '2025-03-01T04:00:00Z' (UTC). parse_ts() below handles both without depending on
--     the time zone of the machine running the script.
--   * DRAFT versions have no versionNumber, releaseTime or publicationDate (NULL is expected).
--   * A deaccessioned dataset comes back WITHOUT latestVersion: is_deaccessioned = TRUE and
--     its descriptive fields are NULL (the quality checks skip it).
--   * datasetContact (names and e-mail addresses) is personal data. It is deliberately NOT
--     extracted here. Keep the raw bucket restricted and do not copy it to silver/gold
--     unless a use case really needs it.
--
-- Run it with silver.py (or from Python, see the end of this file).
-- =============================================================================


-- -----------------------------------------------------------------------------
-- 1. READ BRONZE
--
-- We declare the shape we depend on ("schema on read") instead of letting DuckDB guess:
--   * keys missing from a record become NULL
--   * a value of the wrong type makes the query fail loudly
--   * keys we do not list are ignored (the raw files keep everything)
-- "value" is JSON because in the API it is sometimes text, sometimes a list, sometimes
-- an object, depending on the field.
-- run_id, extract_date and dataverse come from the folder names (hive_partitioning).
-- dataverse is the one that was searched (it is what the old code called dv_parent).
-- -----------------------------------------------------------------------------
CREATE OR REPLACE TEMP TABLE bronze_meta AS
SELECT run_id, extract_date, dataverse, data
FROM read_json(
    {{RAW_FILES}},
    format = 'newline_delimited',
    hive_partitioning = true,
    columns = {
        status: 'VARCHAR',
        data: 'STRUCT(
            id BIGINT, identifier VARCHAR, protocol VARCHAR, authority VARCHAR,
            publisher VARCHAR, publicationDate VARCHAR,
            latestVersion STRUCT(
                datasetPersistentId VARCHAR,
                versionNumber INTEGER, versionMinorNumber INTEGER, versionState VARCHAR,
                createTime VARCHAR, lastUpdateTime VARCHAR, releaseTime VARCHAR,
                termsOfUse VARCHAR,
                license STRUCT(name VARCHAR, uri VARCHAR),
                metadataBlocks STRUCT(
                    citation STRUCT(
                        fields STRUCT(typeName VARCHAR, typeClass VARCHAR, "value" JSON)[]
                    )
                ),
                files STRUCT(
                    label VARCHAR, restricted BOOLEAN,
                    dataFile STRUCT(id BIGINT, filename VARCHAR, contentType VARCHAR, filesize BIGINT)
                )[]
            )
        )'
    }
);


-- -----------------------------------------------------------------------------
-- 2. EXPLODE THE "fields" LIST
--
-- The descriptive metadata (title, authors, subjects...) is NOT a column per field.
-- It is a LIST of {typeName, typeClass, value} entries. UNNEST turns each list element
-- into its own row, so a dataset with 7 fields becomes 7 rows. This long shape
-- (one row per dataset + field) is often called EAV: entity, attribute, value.
--
-- global_id is the key that links everything (and links back to the search entity).
-- -----------------------------------------------------------------------------
CREATE OR REPLACE TEMP TABLE stg_fields AS
SELECT
    run_id,
    COALESCE(
        data.latestVersion.datasetPersistentId,
        data.protocol || ':' || data.authority || '/' || data.identifier
    )                AS global_id,
    f.typeName       AS type_name,
    f.typeClass      AS type_class,
    f."value"        AS value          -- JSON
FROM bronze_meta,
     UNNEST(data.latestVersion.metadataBlocks.citation.fields) AS t(f);


-- -----------------------------------------------------------------------------
-- 3. ONE ROW PER DATASET PER RUN (still untyped: dates are text here)
--
-- scalars: simple values that are already columns inside the document
-- pivoted: single-valued fields turned back into columns. Conditional aggregation
--          (max(...) FILTER (WHERE type_name = ...)) is the classic way to pivot rows
--          into columns. max() is only a way to pick the single matching value.
-- "->> '$'" extracts a JSON value as plain text (no surrounding quotes).
-- -----------------------------------------------------------------------------
CREATE OR REPLACE TEMP TABLE stg_datasets_all AS
WITH scalars AS (
    SELECT
        run_id,
        dataverse,
        COALESCE(
            data.latestVersion.datasetPersistentId,
            data.protocol || ':' || data.authority || '/' || data.identifier
        )                                           AS global_id,
        data.id                                     AS dataset_id,
        data.publisher                              AS publisher,
        data.publicationDate                        AS publication_date_raw,
        data.latestVersion.versionState             AS version_state,
        data.latestVersion.versionNumber            AS version_number,
        data.latestVersion.versionMinorNumber       AS version_minor_number,
        data.latestVersion.license.name             AS license_name,
        data.latestVersion.createTime               AS create_time_raw,
        data.latestVersion.lastUpdateTime           AS last_update_time_raw,
        data.latestVersion.releaseTime              AS release_time_raw,
        COALESCE(len(data.latestVersion.files), 0)  AS n_files,
        COALESCE(len(list_filter(data.latestVersion.files, f -> f.restricted)), 0) AS n_restricted_files,
        data.latestVersion.termsOfUse               AS terms_of_use_raw,
        data.latestVersion IS NULL                  AS is_deaccessioned
    FROM bronze_meta
),
pivoted AS (
    SELECT
        run_id,
        global_id,
        max(value ->> '$')                             FILTER (WHERE type_name = 'title')         AS title,
        max(value ->> '$')                             FILTER (WHERE type_name = 'depositor')     AS depositor,
        max(value ->> '$')                             FILTER (WHERE type_name = 'dateOfDeposit') AS date_of_deposit_raw,
        -- compound + multiple: a list of objects. We keep the first description.
        max(value ->> '$[0].dsDescriptionValue.value') FILTER (WHERE type_name = 'dsDescription') AS description
    FROM stg_fields
    GROUP BY run_id, global_id
)
SELECT s.*, p.title, p.depositor, p.date_of_deposit_raw, p.description
FROM scalars s
LEFT JOIN pivoted p USING (run_id, global_id);


-- -----------------------------------------------------------------------------
-- A MACRO is a reusable SQL expression (a small function you define once).
--
-- parse_ts(text) -> TIMESTAMP
--   * text with an explicit zone ('...Z' or an offset like '+07:00'): converted to UTC
--   * text WITHOUT a zone: the clock time is kept exactly as given. We must not cast it
--     to TIMESTAMPTZ, because DuckDB would then assume the time zone of the machine
--     running the query and silently shift the value (a Jakarta machine shifts it by 7 h).
--   * not a valid timestamp: NULL (the quality checks count these)
-- -----------------------------------------------------------------------------
CREATE OR REPLACE TEMP MACRO parse_ts(x) AS
    CASE
        WHEN x IS NULL THEN NULL
        WHEN regexp_matches(x, '(Z|[+-][0-9]{2}:?[0-9]{2})$')
            THEN TRY_CAST(x AS TIMESTAMPTZ) AT TIME ZONE 'UTC'
        ELSE TRY_CAST(x AS TIMESTAMP)
    END;


-- -----------------------------------------------------------------------------
-- 4. SILVER: DATASETS  (typed + de-duplicated)
--
-- Typing: TRY_CAST returns NULL instead of failing when the text is not a valid
-- date/timestamp. The quality checks in section 6 then count the NULLs that should
-- not be NULL, so bad values are reported rather than crashing the run or being lost.
-- Timestamps use parse_ts() (see above). The result columns are plain TIMESTAMPs: values
-- that came with a zone are in UTC, values that came without one are kept as the source
-- gave them (the zone is unknown, so we do not claim it is UTC).
--
-- De-duplication: every run holds a full copy of the datasets, so the same global_id
-- appears once per run. ROW_NUMBER() numbers the copies of each global_id, newest
-- first, and QUALIFY keeps number 1. (QUALIFY filters on a window function; in other
-- databases you would wrap this in a subquery or CTE.)
-- -----------------------------------------------------------------------------
CREATE OR REPLACE TABLE silver_datasets AS
WITH typed AS (
    SELECT
        run_id                                                          AS source_run_id,
        dataverse,
        global_id,
        dataset_id,
        title,
        description,
        depositor,
        publisher,
        license_name,
        version_state,
        version_number,
        version_minor_number,
        n_files,
        n_restricted_files,
        n_files - n_restricted_files                                    AS n_open_files,
        CASE WHEN n_files = 0                       THEN 'No Files'
             WHEN n_restricted_files = 0            THEN 'Fully Open'
             WHEN n_restricted_files = n_files      THEN 'Fully Restricted'
             ELSE 'Partially Restricted' END                            AS restriction_status,
        COALESCE(trim(terms_of_use_raw), '') <> ''                      AS has_custom_terms,
        is_deaccessioned,
        TRY_CAST(publication_date_raw   AS DATE)                        AS publication_date,
        TRY_CAST(date_of_deposit_raw    AS DATE)                        AS date_of_deposit,
        parse_ts(create_time_raw)                                       AS create_time,
        parse_ts(last_update_time_raw)                                  AS last_update_time,
        parse_ts(release_time_raw)                                      AS release_time
    FROM stg_datasets_all
)
SELECT *
FROM typed
QUALIFY ROW_NUMBER() OVER (
            PARTITION BY global_id
            ORDER BY last_update_time DESC NULLS LAST, source_run_id DESC, dataverse
        ) = 1;


-- -----------------------------------------------------------------------------
-- 5. SILVER: CHILD TABLES  (one-to-many fields get their own table)
--
-- A dataset has several authors/subjects/keywords. Putting them in a list column makes
-- them hard to filter and join, so each becomes a table with one row per item.
-- We take them only from the version of the dataset that silver_datasets kept
-- (the join on source_run_id), so old versions never leak in.
--
-- from_json(value, '["JSON"]') turns a JSON array into a list; UNNEST makes one row per
-- element; WITH ORDINALITY adds the position (1, 2, 3...) so the order is not lost.
-- -----------------------------------------------------------------------------
CREATE OR REPLACE TABLE silver_dataset_authors AS
SELECT
    f.global_id,
    a.pos                                              AS author_position,
    a.item ->> '$.authorName.value'                    AS author_name,
    a.item ->> '$.authorAffiliation.value'             AS affiliation,
    a.item ->> '$.authorIdentifierScheme.value'        AS identifier_scheme,
    a.item ->> '$.authorIdentifier.value'              AS identifier
FROM stg_fields f
JOIN silver_datasets d
  ON d.global_id = f.global_id AND d.source_run_id = f.run_id
CROSS JOIN UNNEST(from_json(f.value, '["JSON"]')) WITH ORDINALITY AS a(item, pos)
WHERE f.type_name = 'author';

-- controlled vocabulary + multiple: the value is a plain list of text, e.g. ["Agricultural Sciences"]
CREATE OR REPLACE TABLE silver_dataset_subjects AS
SELECT
    f.global_id,
    s.pos     AS subject_position,
    s.item    AS subject
FROM stg_fields f
JOIN silver_datasets d
  ON d.global_id = f.global_id AND d.source_run_id = f.run_id
CROSS JOIN UNNEST(from_json(f.value, '["VARCHAR"]')) WITH ORDINALITY AS s(item, pos)
WHERE f.type_name = 'subject';

CREATE OR REPLACE TABLE silver_dataset_keywords AS
SELECT
    f.global_id,
    k.pos                           AS keyword_position,
    k.item ->> '$.keywordValue.value' AS keyword
FROM stg_fields f
JOIN silver_datasets d
  ON d.global_id = f.global_id AND d.source_run_id = f.run_id
CROSS JOIN UNNEST(from_json(f.value, '["JSON"]')) WITH ORDINALITY AS k(item, pos)
WHERE f.type_name = 'keyword';


-- grants: compound + multiple, like authors. A grant may have an agency but no number.
CREATE OR REPLACE TABLE silver_dataset_grants AS
SELECT
    f.global_id,
    g.pos                                    AS grant_position,
    g.item ->> '$.grantNumberAgency.value'   AS agency,
    g.item ->> '$.grantNumberValue.value'    AS grant_number
FROM stg_fields f
JOIN silver_datasets d
  ON d.global_id = f.global_id AND d.source_run_id = f.run_id
CROSS JOIN UNNEST(from_json(f.value, '["JSON"]')) WITH ORDINALITY AS g(item, pos)
WHERE f.type_name = 'grantNumber';


-- -----------------------------------------------------------------------------
-- 6. QUALITY CHECKS  (every check counts BAD rows, so 0 means pass)
--
-- This is what tools such as dbt tests and Great Expectations do, written by hand.
-- Patterns used: duplicate detection (GROUP BY ... HAVING), NULL counts, a value that
-- failed to parse (raw text present but typed value NULL), an anti join (rows in one
-- table with no match in another, i.e. orphans), and reconciling counts across layers.
--
-- severity decides what silver.py does with a failed check (bad_rows > 0):
--   'error'  a defect in the silver tables themselves. The build stops and nothing is published.
--   'warn'   a problem in the source data. It is logged and saved in the build manifest,
--            but the build still publishes.
-- -----------------------------------------------------------------------------
CREATE OR REPLACE TABLE qa_checks AS
SELECT 'duplicate global_id in silver_datasets' AS check_name, 'error' AS severity, count(*) AS bad_rows
FROM (SELECT global_id FROM silver_datasets GROUP BY global_id HAVING count(*) > 1)
UNION ALL
SELECT 'dataset without a title', 'error', count(*)
FROM silver_datasets WHERE title IS NULL AND NOT is_deaccessioned
UNION ALL
SELECT 'publication_date text that is not a valid date', 'warn', count(*)
FROM stg_datasets_all
WHERE publication_date_raw IS NOT NULL AND TRY_CAST(publication_date_raw AS DATE) IS NULL
UNION ALL
SELECT 'last_update_time text that is not a valid timestamp', 'warn', count(*)
FROM stg_datasets_all
WHERE last_update_time_raw IS NOT NULL AND parse_ts(last_update_time_raw) IS NULL
UNION ALL
SELECT 'author row without a parent dataset (orphan)', 'error', count(*)
FROM silver_dataset_authors a ANTI JOIN silver_datasets d USING (global_id)
UNION ALL
SELECT 'dataset without any author', 'warn', count(*)
FROM silver_datasets d ANTI JOIN silver_dataset_authors a USING (global_id)
WHERE NOT d.is_deaccessioned
UNION ALL
SELECT 'dataset found under more than one dataverse (dataverse choice is arbitrary)', 'warn', count(*)
FROM (SELECT global_id FROM stg_datasets_all GROUP BY global_id HAVING count(DISTINCT dataverse) > 1)
UNION ALL
SELECT 'bronze datasets missing from silver (distinct global_id count differs)', 'error',
       abs((SELECT count(DISTINCT global_id) FROM stg_datasets_all) - (SELECT count(*) FROM silver_datasets));

-- Show the result. Anything with bad_rows > 0 needs attention before the data is used.
SELECT * FROM qa_checks ORDER BY bad_rows DESC, severity, check_name;


-- =============================================================================
-- Running it from Python (silver.py does this, plus the quality gate and the Parquet export)
--
--   import duckdb
--   sql = open("flatten_datasets_metadata.sql", encoding="utf-8").read()
--   files = ["s3://raw/dataverse_search/extract_date=2026-10-13/run_id=<RUN_ID>/entity=dataset_metadata/dataverse=<NAME>/part-00000.jsonl.gz"]
--   sql = sql.replace("{{RAW_FILES}}", "[" + ", ".join(repr(f) for f in files) + "]")
--   con = duckdb.connect()
--   # for s3:// paths, first run INSTALL httpfs; LOAD httpfs; and the SET s3_... lines
--   con.execute(sql)
--   print(con.sql("SELECT * FROM qa_checks").df())
-- =============================================================================