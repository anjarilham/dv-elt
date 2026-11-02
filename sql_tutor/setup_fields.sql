-- setup_fields.sql
--
-- Helper used by several lessons. Needs bronze_meta (run setup_bronze.sql first).
-- Builds stg_fields: one row per dataset field (title, author, subject, ...), the "long" EAV
-- shape. Lesson 13 explains the UNNEST, lesson 14 the EAV idea.

CREATE OR REPLACE TEMP TABLE stg_fields AS
SELECT
    run_id,
    COALESCE(
        data.latestVersion.datasetPersistentId,
        data.protocol || ':' || data.authority || '/' || data.identifier
    )                AS global_id,
    f.typeName       AS type_name,
    f.typeClass      AS type_class,
    f."value"        AS value
FROM bronze_meta,
     UNNEST(data.latestVersion.metadataBlocks.citation.fields) AS t(f);
