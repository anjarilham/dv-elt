# silver.py
#
# Silver stage: read the raw (bronze) Dataverse metadata from MinIO, flatten and clean it with
# DuckDB (flatten_datasets_metadata.sql), check its quality, and publish it as Parquet files.
#
#   raw bucket (bronze)                        silver bucket
#   dataverse_search/.../run_id=X/...jsonl.gz  -->  dataverse_search/build=Y/datasets.parquet
#                                                    dataverse_search/build=Y/dataset_authors.parquet
#                                                    ... + _manifest.json + _SUCCESS
#
# Run it with:   python silver.py                  (all successful runs, default)
#                python silver.py --runs latest    (only the newest successful run)
#                python silver.py --run-id <ID>    (one specific run)

import argparse
import datetime
import json
import logging
import sys
import tempfile
import uuid
from pathlib import Path
from urllib.parse import urlparse

import duckdb
from botocore.exceptions import ClientError

from config import Config
from logger import setup_logger
from raw_store import create_s3_client, ensure_bucket

logger = logging.getLogger(__name__)

SOURCE = "dataverse_search"          # top-level prefix in both buckets (same as RawStore's default)
ENTITY = "dataset_metadata"          # the raw entity that holds one full record per dataset
SQL_FILE = Path(__file__).with_name("flatten_datasets_metadata.sql")

# Tables created by the SQL script that are published. "silver_datasets" becomes datasets.parquet.
SILVER_TABLES = [
    "silver_datasets",
    "silver_dataset_authors",
    "silver_dataset_subjects",
    "silver_dataset_keywords",
    "silver_dataset_grants",
]

_NOT_FOUND_CODES = ("404", "NoSuchKey", "NotFound", "NoSuchBucket")


class SilverError(Exception):
    """The silver build cannot continue: no usable raw runs, a failed quality check, or a count mismatch."""


# ------------------------------------------------------------------ 1. choose the raw runs


def list_successful_runs(s3, bucket: str, source: str = SOURCE) -> list[str]:
    """Find every raw run that finished successfully, oldest first.

    A run counts as successful when its ``_SUCCESS`` marker exists. Runs that failed or were
    killed half way never get that marker, so they are skipped here and their half-written
    data is never read. This is the whole reason the marker is written last by RawStore.

    The listing walks every object under the source prefix. That is fine for a few thousand
    runs; for much more, list the ``extract_date=`` folders first (Delimiter="/").

    Arguments:
        s3: boto3 S3 client (see raw_store.create_s3_client).
        bucket: raw bucket name, e.g. "raw".
        source: top-level prefix of the data source. Defaults to "dataverse_search".

    Returns:
        list[str]: run prefixes such as "dataverse_search/extract_date=2026-10-05/run_id=<ID>",
        sorted by run_id. The run_id starts with the UTC timestamp, so this is time order.

    Raises:
        SilverError: the raw bucket does not exist.
        botocore.exceptions.ClientError: any other S3 error (wrong credentials, ...).
    """
    suffix = "/_SUCCESS"
    prefixes = []
    try:
        for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=f"{source}/"):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                if key.endswith(suffix) and "/run_id=" in key:
                    prefixes.append(key[: -len(suffix)])
    except ClientError as e:
        if e.response["Error"]["Code"] in _NOT_FOUND_CODES:
            raise SilverError(f"Raw bucket {bucket!r} does not exist. Run main.py first.") from e
        raise
    return sorted(prefixes, key=_run_id_of)


def _run_id_of(run_prefix: str) -> str:
    """Return the run_id part of a run prefix: "a/b/run_id=XYZ" -> "XYZ"."""
    return run_prefix.rsplit("run_id=", 1)[1]


def select_runs(run_prefixes: list[str], mode: str = "all", run_id: str | None = None) -> list[str]:
    """Pick which successful runs go into this silver build.

    Arguments:
        run_prefixes: result of list_successful_runs, oldest first.
        mode: "all" uses every successful run (a full rebuild; the SQL keeps the newest version
            of each dataset). "latest" uses only the newest one. Ignored when run_id is given.
        run_id: use only this run. It must be one of the successful runs.

    Returns:
        list[str]: the chosen run prefixes, oldest first.

    Raises:
        SilverError: there is no successful run, the run_id is not among them (typo, or the run
            failed or is unfinished), or mode is unknown.
    """
    if not run_prefixes:
        raise SilverError("No successful raw run found (no _SUCCESS marker). Run main.py first.")

    if run_id is not None:
        chosen = [p for p in run_prefixes if _run_id_of(p) == run_id]
        if not chosen:
            known = ", ".join(_run_id_of(p) for p in run_prefixes[-5:])
            raise SilverError(
                f"Run {run_id!r} is not a successful run (it may not exist, have failed, or still be "
                f"running). Most recent successful runs: {known}"
            )
        return chosen

    if mode == "all":
        return list(run_prefixes)
    if mode == "latest":
        return run_prefixes[-1:]
    raise SilverError(f"Unknown mode {mode!r}. Use 'all' or 'latest'.")


def collect_bronze_files(s3, bucket: str, run_prefixes: list[str]) -> tuple[list[str], list[dict]]:
    """Read each run's manifest and list the dataset_metadata files that should be read.

    Only files the manifest vouches for are returned: the dataverse must have status "success"
    and the file must hold at least one row. The manifest is the source of truth, not a
    directory listing, so a stray file in the bucket is never picked up.

    Arguments:
        s3: boto3 S3 client.
        bucket: raw bucket name.
        run_prefixes: runs chosen by select_runs.

    Returns:
        tuple: (keys, runs). keys is the list of object keys in the raw bucket. runs has one dict
        per run with run_id, files and rows, which goes into the silver manifest for lineage.

    Raises:
        SilverError: a run has a _SUCCESS marker but no readable manifest.
        botocore.exceptions.ClientError: any other S3 error.
    """
    keys: list[str] = []
    runs: list[dict] = []

    for prefix in run_prefixes:
        try:
            body = s3.get_object(Bucket=bucket, Key=f"{prefix}/_manifest.json")["Body"].read()
        except ClientError as e:
            if e.response["Error"]["Code"] in _NOT_FOUND_CODES:
                raise SilverError(f"Run {prefix} has _SUCCESS but no _manifest.json. Raw data is inconsistent.") from e
            raise
        manifest = json.loads(body)

        n_files = n_rows = 0
        for dataverse, info in manifest["dataverses"].items():
            entity = info.get("entities", {}).get(ENTITY)
            if info.get("status") != "success" or not entity or entity["rows"] == 0:
                continue
            keys.append(f"{prefix}/{entity['file']}")
            n_files += 1
            n_rows += entity["rows"]

        runs.append({"run_id": manifest["run_id"], "files": n_files, "rows": n_rows})
        logger.info("Raw run %s: %s files, %s dataset records", manifest["run_id"], n_files, n_rows)

    return keys, runs


# ------------------------------------------------------------------ 2. DuckDB


def _sql_string(value: str) -> str:
    """Quote a Python string as a SQL string literal ('' escapes a single quote)."""
    return "'" + str(value).replace("'", "''") + "'"


def duckdb_uri(bucket: str, key: str) -> str:
    """Return the path DuckDB uses to read one raw object: s3://<bucket>/<key>.

    It is a function of its own so a test can swap it for a local path.
    """
    return f"s3://{bucket}/{key}"


def connect_duckdb(config: Config) -> duckdb.DuckDBPyConnection:
    """Open a fresh in-memory DuckDB that can read from MinIO.

    It loads the httpfs extension (DuckDB's S3 client; ``INSTALL`` downloads it once and needs
    internet access the first time) and points it at the MinIO endpoint with path-style URLs,
    the same way the earlier notebook experiment did. The session time zone is set to UTC so
    results never depend on the machine the job runs on.

    In-memory means nothing persists between runs: the silver Parquet files in the silver
    bucket are the output, not a .duckdb file.

    Arguments:
        config: Config with minio_endpoint, minio_access_key and minio_secret_key.

    Returns:
        duckdb.DuckDBPyConnection: ready to run the SQL script. The caller closes it.

    Raises:
        duckdb.Error: httpfs cannot be installed or loaded (for example no internet on the
            first run).
    """
    endpoint = urlparse(config.minio_endpoint)
    settings = {
        "s3_endpoint": _sql_string(endpoint.netloc),               # host:port, without http://
        "s3_use_ssl": "true" if endpoint.scheme == "https" else "false",
        "s3_url_style": "'path'",                                   # http://host:9000/<bucket>/<key>
        "s3_region": "'us-east-1'",
        "s3_access_key_id": _sql_string(config.minio_access_key),
        "s3_secret_access_key": _sql_string(config.minio_secret_key),
        "TimeZone": "'UTC'",
    }

    con = duckdb.connect()  # no argument = in-memory
    try:
        con.execute("INSTALL httpfs")
        con.execute("LOAD httpfs")
        for name, literal in settings.items():
            con.execute(f"SET {name} = {literal}")
    except Exception:
        con.close()
        raise
    return con


def build_silver_tables(con: duckdb.DuckDBPyConnection, uris: list[str]) -> None:
    """Run flatten_datasets_metadata.sql, creating the silver_* tables and qa_checks in the session.

    The script contains one placeholder, {{RAW_FILES}}, which is replaced by the list of files to
    read. Only the real use inside read_json is replaced; the mentions in comments are left alone.

    Arguments:
        con: connection from connect_duckdb.
        uris: paths of the raw files, from duckdb_uri.

    Raises:
        SilverError: the SQL file does not contain exactly one {{RAW_FILES}} placeholder outside
            comments.
        duckdb.Error: the SQL failed (a bad file, an unreadable record, ...).
    """
    file_list = "[" + ", ".join(_sql_string(u) for u in uris) + "]"

    lines = []
    replaced = 0
    for line in SQL_FILE.read_text(encoding="utf-8").splitlines():
        if not line.lstrip().startswith("--") and "{{RAW_FILES}}" in line:
            line = line.replace("{{RAW_FILES}}", file_list)
            replaced += 1
        lines.append(line)
    if replaced != 1:
        raise SilverError(f"{SQL_FILE.name} must contain {{{{RAW_FILES}}}} exactly once outside comments (found {replaced}).")

    con.execute("\n".join(lines))


def check_quality(con: duckdb.DuckDBPyConnection, expected_bronze_rows: int) -> list[dict]:
    """Reconcile row counts with the raw manifests and evaluate the SQL quality checks.

    Two gates, both of which stop the build before anything is published:
      1. The number of rows DuckDB loaded from the raw files must equal the number the manifests
         say were written. A difference means a file was truncated or only partly read.
      2. Every qa_checks row with severity "error" must have bad_rows = 0.
    Checks with severity "warn" describe problems in the source data. They are logged and kept in
    the manifest, but do not block publishing.

    Arguments:
        con: connection after build_silver_tables.
        expected_bronze_rows: total dataset_metadata rows according to the raw manifests.

    Returns:
        list[dict]: every check as {"check_name", "severity", "bad_rows"}.

    Raises:
        SilverError: the row counts differ, or at least one error-severity check found bad rows.
    """
    loaded = con.execute("SELECT count(*) FROM bronze_meta").fetchone()[0]
    if loaded != expected_bronze_rows:
        raise SilverError(
            f"DuckDB loaded {loaded} raw records but the manifests say {expected_bronze_rows} were stored. "
            "A raw file may be truncated or unreadable."
        )

    rows = con.execute("SELECT check_name, severity, bad_rows FROM qa_checks ORDER BY severity, check_name").fetchall()
    checks = [{"check_name": n, "severity": s, "bad_rows": int(b)} for n, s, b in rows]

    failed = [c for c in checks if c["severity"] == "error" and c["bad_rows"] > 0]
    for c in checks:
        if c["bad_rows"] == 0:
            logger.info("QA ok:   %s", c["check_name"])
        elif c["severity"] == "error":
            logger.error("QA FAIL: %s (%s bad rows)", c["check_name"], c["bad_rows"])
        else:
            logger.warning("QA warn: %s (%s rows)", c["check_name"], c["bad_rows"])

    if failed:
        names = "; ".join(f"{c['check_name']} ({c['bad_rows']})" for c in failed)
        raise SilverError(f"Quality gate failed, nothing was published: {names}")
    return checks


# ------------------------------------------------------------------ 3. publish


def export_parquet(con: duckdb.DuckDBPyConnection, out_dir: Path) -> dict[str, dict]:
    """Write each silver table to a Parquet file (zstd compressed) in a local folder.

    The files are written locally first and uploaded by publish_build with the same boto3
    client used everywhere else, so the upload and the commit marker follow one code path.

    Arguments:
        con: connection holding the silver_* tables.
        out_dir: existing local folder. Files are named after the table without "silver_",
            e.g. silver_dataset_authors -> dataset_authors.parquet.

    Returns:
        dict: table name -> {"rows", "size_bytes", "file"}, e.g. {"datasets": {...}, ...}.
    """
    tables: dict[str, dict] = {}
    for table in SILVER_TABLES:
        name = table.removeprefix("silver_")
        path = out_dir / f"{name}.parquet"
        con.execute(f"COPY {table} TO {_sql_string(path.as_posix())} (FORMAT parquet, COMPRESSION zstd)")
        rows = con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        tables[name] = {"rows": rows, "size_bytes": path.stat().st_size, "file": path.name}
        logger.info("Wrote %s: %s rows, %s bytes", path.name, rows, path.stat().st_size)
    return tables


def publish_build(s3, bucket: str, build_prefix: str, out_dir: Path, tables: dict[str, dict], manifest: dict) -> None:
    """Upload the Parquet files, then the manifest, then the _SUCCESS marker, in that order.

    The order is the commit protocol: a reader that finds _SUCCESS knows every file before it is
    complete. If the upload dies half way, there is no _SUCCESS and readers ignore the build.

    Arguments:
        s3: boto3 S3 client.
        bucket: silver bucket name. Created if missing.
        build_prefix: key prefix of this build, e.g. "dataverse_search/build=<ID>".
        out_dir: local folder with the files written by export_parquet.
        tables: second result of export_parquet (only the file names are used).
        manifest: dict saved as _manifest.json.

    Raises:
        botocore.exceptions.ClientError: an upload was rejected.
    """
    ensure_bucket(s3, bucket)

    for info in tables.values():
        s3.upload_file(str(out_dir / info["file"]), bucket, f"{build_prefix}/{info['file']}")

    body = json.dumps(manifest, indent=2, ensure_ascii=False).encode("utf-8")
    s3.put_object(Bucket=bucket, Key=f"{build_prefix}/_manifest.json", Body=body, ContentType="application/json")
    s3.put_object(Bucket=bucket, Key=f"{build_prefix}/_SUCCESS", Body=b"")  # written last: the commit


# ------------------------------------------------------------------ 4. orchestration


def run_silver(config: Config, s3, mode: str = "all", run_id: str | None = None) -> dict:
    """Run the whole silver stage: choose raw runs, build with DuckDB, check, publish.

    Arguments:
        config: loaded Config (buckets and MinIO connection).
        s3: boto3 S3 client.
        mode: "all" or "latest" (see select_runs). Ignored when run_id is given.
        run_id: process only this raw run, or None.

    Returns:
        dict: the manifest that was written to the silver bucket (build_id, location, source_runs,
        bronze_rows, tables, qa_checks, ...).

    Raises:
        SilverError: no usable raw data, a count mismatch, or a failed quality gate.
        duckdb.Error: the SQL or the S3 read failed.
        botocore.exceptions.ClientError: an S3 request failed.
    """
    started_at = datetime.datetime.now(datetime.timezone.utc)

    run_prefixes = select_runs(list_successful_runs(s3, config.minio_bucket), mode, run_id)
    keys, runs = collect_bronze_files(s3, config.minio_bucket, run_prefixes)
    if not keys:
        raise SilverError("The selected runs contain no dataset_metadata rows to process.")
    expected_rows = sum(r["rows"] for r in runs)
    logger.info("Building silver from %s runs, %s files, %s records", len(runs), len(keys), expected_rows)

    build_id = f"{started_at:%Y%m%dT%H%M%SZ}_{uuid.uuid4().hex[:6]}"
    build_prefix = f"{SOURCE}/build={build_id}"

    con = connect_duckdb(config)
    try:
        build_silver_tables(con, [duckdb_uri(config.minio_bucket, k) for k in keys])
        checks = check_quality(con, expected_rows)
        with tempfile.TemporaryDirectory(prefix="silver_") as tmp:
            out_dir = Path(tmp)
            tables = export_parquet(con, out_dir)
            manifest = {
                "build_id": build_id,
                "location": f"s3://{config.minio_silver_bucket}/{build_prefix}",
                "started_at": started_at.isoformat(),
                "finished_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "status": "success",
                "mode": f"run_id={run_id}" if run_id else mode,
                "source_bucket": config.minio_bucket,
                "source_runs": runs,
                "bronze_rows": expected_rows,
                "tables": tables,
                "qa_checks": checks,
                "duckdb_version": duckdb.__version__,
            }
            publish_build(s3, config.minio_silver_bucket, build_prefix, out_dir, tables, manifest)
    finally:
        con.close()

    logger.info("Silver build %s published to %s", build_id, manifest["location"])
    return manifest


def main(argv: list[str] | None = None) -> int:
    """Entry point: parse options, load settings, set up logging, run the silver stage.

    Every failure is logged and turned into exit code 1: expected failures (SilverError, DuckDB
    errors) with their message, unexpected ones with a full traceback, so cron, Airflow or CI can
    detect that the stage failed.

    Arguments:
        argv: command-line arguments without the program name, or None to read sys.argv.
            ``--runs {all,latest}`` chooses the raw runs, ``--run-id RUN_ID`` selects one.

    Returns:
        int: 0 if the build was published, 1 otherwise. Pass it to sys.exit().
    """
    parser = argparse.ArgumentParser(description="Build the silver layer (Parquet) from the raw layer with DuckDB.")
    parser.add_argument(
        "--runs",
        choices=["all", "latest"],
        default="all",
        help="all (default): every successful raw run, newest version of each dataset wins. "
             "latest: only the newest successful run.",
    )
    parser.add_argument("--run-id", metavar="RUN_ID", help="Use only this raw run (overrides --runs).")
    args = parser.parse_args(argv)

    config = Config()
    log_file = f"logs/{config.log_file}_silver_{datetime.datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.log"
    setup_logger(log_file)

    logger.info("Starting silver build")
    try:
        s3 = create_s3_client(config.minio_endpoint, config.minio_access_key, config.minio_secret_key)
        manifest = run_silver(config, s3, args.runs, args.run_id)
    except SilverError as e:
        logger.error("Silver build FAILED: %s", e)
        return 1
    except duckdb.Error:
        logger.exception("Silver build FAILED in DuckDB")
        return 1
    except Exception:
        logger.exception("Silver build FAILED with an unexpected error")
        return 1

    logger.info("Silver build completed. Rows by table: %s", {t: i["rows"] for t, i in manifest["tables"].items()})
    return 0


if __name__ == "__main__":
    sys.exit(main())