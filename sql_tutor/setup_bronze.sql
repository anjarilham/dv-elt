-- setup_bronze.sql
--
-- Helper used by several lessons: builds bronze_meta from the sample files, exactly the way
-- flatten_datasets_metadata.sql (section 1) does it for the real data. Lesson 10 explains it.

CREATE OR REPLACE TEMP TABLE bronze_meta AS
SELECT run_id, extract_date, dataverse, data
FROM read_json(
    'sample_data/extract_date=*/run_id=*/entity=dataset_metadata/dataverse=*/*.jsonl',
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
