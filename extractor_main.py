# extractor_main.py

import argparse
import asyncio
import datetime
import logging
import os
import sys

from dotenv import load_dotenv

from config import Config
from extractor import DatasetExtract, ExtractionError
from logger import setup_logger
from raw_store import RawStore, RawStoreError, create_s3_client_from_env

logger = logging.getLogger(__name__)


async def run_search(extractor, store, dataverses) -> None:
    """Stage 1: search each dataverse and save the results to the raw layer.

    For each dataverse it calls extractor.extract_dataverse, then stores the result as entity
    "search". A failure in one dataverse is logged with its traceback and recorded on the store
    (record_failure), and the loop carries on, so one bad dataverse does not hide the state of
    the others. Nothing is returned and the results are not kept in memory: later stages read
    them back from MinIO.

    Arguments:
        extractor: DatasetExtract instance from extractor.py, already inside its ``async with``
            block (it needs the open HTTP session).
        store: RawStore instance from raw_store.py for the current run. Results are written to
            it and failures are recorded on it.
        dataverses: list of dataverse aliases to search, e.g. ["orpertanian", "ORNM"].

    Returns:
        None. The outcome lives in the store and, after store.finalize(), in the manifest.
    """
    for dataverse in dataverses:
        # One bad dataverse must not hide the state of the others,
        # so record the failure and keep going. The run is failed at the end.
        try:
            items, total = await extractor.extract_dataverse(dataverse)
            if not items:
                logger.warning("Dataverse %s returned 0 datasets", dataverse)
            store.write_dataverse(dataverse, "search", items, total)
        except Exception as e:  # includes unexpected bugs, which also must fail the run
            logger.exception("Search FAILED for dataverse: %s", dataverse)
            store.record_failure(dataverse, e)


async def run_enrichment(extractor, store, dataverses) -> None:
    """Stage 2: fetch the full metadata of every dataset found by stage 1.

    The input is read back from the raw store (MinIO), not passed along in memory. That is what
    lets this stage be re-run on its own against an existing run. For each dataverse:
      - dataset_metadata already stored: skipped.
      - no search results stored: skipped with a warning. Any failure recorded by stage 1 stays
        in the manifest.
      - otherwise: clear any earlier failure, read the stored search results, call
        extractor.fetch_dataset_metadata and store the result as entity "dataset_metadata".
    A failure is logged with its traceback and recorded on the store, and the loop carries on.

    Arguments:
        extractor: DatasetExtract instance from extractor.py, already inside its ``async with``
            block.
        store: RawStore for the run to enrich. It can be a new run or one re-opened with
            RawStore.open_run.
        dataverses: list of dataverse aliases to process.

    Returns:
        None. The outcome lives in the store and, after store.finalize(), in the manifest.
    """
    for dataverse in dataverses:
        if store.has_entity(dataverse, "dataset_metadata"):
            logger.info("Dataverse %s already enriched, skipping", dataverse)
            continue
        if not store.has_entity(dataverse, "search"):
            # Nothing to enrich. Any failure recorded by stage 1 stays in the manifest.
            logger.warning("No stored search results for dataverse %s, skipping enrichment", dataverse)
            continue

        store.clear_failure(dataverse)  # we are (re)trying it, so forget any earlier failure
        try:
            search_items = store.read_entity(dataverse, "search")
            records, total = await extractor.fetch_dataset_metadata(dataverse, search_items)
            store.write_dataverse(dataverse, "dataset_metadata", records, total)
        except Exception as e:
            logger.exception("Enrichment FAILED for dataverse: %s", dataverse)
            store.record_failure(dataverse, e)


async def run_extraction(
    config, s3, bucket: str, enrich_run_id: str | None = None, root_installation: bool = False
) -> dict:
    """Run a whole extraction into the raw layer and decide whether the run succeeded.

    Normal run (enrich_run_id is None): takes the dataverses from config.all_dataverse_list (it
    must not be empty) or, with root_installation, fetches every dataverse alias in the
    installation from the search API (extractor.extract_dataverse_aliases). Then it starts a new
    RawStore run and runs run_search followed by run_enrichment.
    Enrichment-only (enrich_run_id given): re-opens that stored run, uses the dataverse list
    saved in its manifest (the config's list is ignored) and runs run_enrichment only, which
    skips whatever is already stored.
    Either way it ends with store.finalize(), which writes the manifest and, if everything
    succeeded, the _SUCCESS marker.

    Arguments:
        config: Config object (see config.py) with the Dataverse settings used by DatasetExtract
            and the values saved in the manifest (base_url, search_query, date range,
            all_dataverse_list).
        s3: boto3 S3 client from create_s3_client_from_env().
        bucket: bucket name for the raw layer, e.g. "raw".
        enrich_run_id: id of a stored run to re-open for an enrichment-only run, or None to
            start a new run.
        root_installation: True to extract every dataverse in the installation, with the aliases
            fetched from the search API, instead of the ones in config.all_dataverse_list.
            Ignored when enrich_run_id is given.

    Returns:
        dict: the manifest of the successful run (see RawStore.finalize), for example
        manifest["rows_by_entity"].

    Raises:
        ExtractionError: no dataverses are configured, or (with root_installation) listing them
            failed or returned none, or at least one dataverse failed. In the last case the
            manifest has already been written, and the message lists the failed dataverses and
            where the run is stored.
        RawStoreError: enrich_run_id does not exist or has no manifest.
    """
    if enrich_run_id:
        store = RawStore.open_run(s3, bucket, enrich_run_id)
        dataverses = store.run_params["dataverses"]
        logger.info("Re-opened run %s (%s). Running the enrichment stage only.", store.run_id, store.run_uri)
    else:
        if root_installation:
            # Every dataverse in the installation, as listed by the search API.
            async with DatasetExtract(config) as extractor:
                dataverses, _ = await extractor.extract_dataverse_aliases()
            if not dataverses:
                raise ExtractionError("The search API returned no dataverses")
        else:
            dataverses = config.all_dataverse_list
            if not dataverses:
                raise ExtractionError("No dataverses configured (check ALL_DATAVERSE_LIST)")
        store = RawStore(
            s3,
            bucket,
            run_params={
                "base_url": config.base_url,
                "search_query": config.search_query,
                "date_start": config.search_date_range_start or None,
                "date_end": config.search_date_range_end or None,
                "dataverses": list(dataverses),
            },
        )
        logger.info("Run %s started. Raw output: %s", store.run_id, store.run_uri)

    async with DatasetExtract(config) as extractor:
        if not enrich_run_id:
            await run_search(extractor, store, dataverses)
        await run_enrichment(extractor, store, dataverses)

    manifest = store.finalize()

    if manifest["status"] != "success":
        failed = [d for d, info in manifest["dataverses"].items() if info["status"] != "success"]
        raise ExtractionError(f"Run {store.run_id} failed for dataverses: {failed}. See {store.run_uri}")

    return manifest


async def main(enrich_run_id: str | None = None, root_installation: bool = False) -> int:
    """Entry point: load settings, set up logging, run the extraction, return an exit code.

    Steps: load .env (its values override variables already set in the environment), build
    Config, set up logging (console plus a timestamped file in logs/, named from LOG_FILE),
    create the MinIO client from the MINIO_* variables (bucket from MINIO_BUCKET, default "raw"),
    then call run_extraction. Every failure is logged here and turned into exit code 1: expected
    failures with their message, unexpected ones with a full traceback. That lets cron, Airflow
    or CI detect the failure.

    Arguments:
        enrich_run_id: id of a stored run to re-run only the enrichment stage on (see
            run_extraction), or None to start a new run. Set by the --enrich-run command-line
            option.
        root_installation: True to extract every dataverse in the installation instead of the
            ones in the config (see run_extraction). Set by the --root-installation
            command-line option.

    Returns:
        int: 0 if the run succeeded, 1 if it failed. Pass it to sys.exit().
    """
    load_dotenv(override=True)
    config = Config()

    log_file = f"logs/{os.getenv('LOG_FILE')}_{datetime.datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.log"
    setup_logger(log_file)

    logger.info("Starting dataset extraction process")
    try:
        s3 = create_s3_client_from_env()
        bucket = os.getenv("MINIO_BUCKET", "raw")
        manifest = await run_extraction(config, s3, bucket, enrich_run_id, root_installation)
    except (ExtractionError, RawStoreError) as e:
        logger.error("Extraction FAILED: %s", e)
        return 1  # non-zero exit code so cron / Airflow / CI sees the failure
    except Exception:
        # Anything unexpected (MinIO down, missing env vars, a bug): log the traceback, still exit non-zero.
        logger.exception("Extraction FAILED with an unexpected error")
        return 1

    logger.info("Extraction completed. Rows by entity: %s", manifest["rows_by_entity"])
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Extract Dataverse metadata into the raw layer.")
    parser.add_argument(
        "--enrich-run",
        metavar="RUN_ID",
        help="Skip the search stage: re-open this stored run and (re)run only the enrichment stage.",
    )
    parser.add_argument(
        "--root-installation",
        action="store_true",
        help="Extract every dataverse in the installation (aliases are fetched from the search API) "
        "instead of the dataverses in ALL_DATAVERSE_LIST.",
    )
    args = parser.parse_args()
    if args.root_installation and args.enrich_run:
        parser.error("--root-installation cannot be used with --enrich-run: that run already has its dataverse list")
    sys.exit(asyncio.run(main(args.enrich_run, args.root_installation)))