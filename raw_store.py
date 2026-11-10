# raw_store.py

import gzip
import io
import json
import logging
import os
import tempfile
import uuid
from datetime import datetime, timezone

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

_NOT_FOUND_CODES = ("404", "NoSuchKey", "NotFound", "NoSuchBucket")


class RawStoreError(Exception):
    """Problem with the raw store: run not found, data missing, or record counts that don't match."""


def create_s3_client(endpoint: str, access_key: str, secret_key: str):
    """Build a boto3 S3 client for MinIO (or any S3-compatible store).

    Settings applied to the client: path-style addressing (http://host:9000/<bucket>/<key>),
    region us-east-1 (MinIO's default), up to 5 attempts using boto3's built-in "standard" retry
    mode, a 5 second connect timeout and a 60 second read timeout. When the installed botocore
    supports it, checksums are only used "when_required", because newer boto3 versions add
    checksum headers that some MinIO versions reject.

    Arguments:
        endpoint: server URL including the scheme, e.g. http://localhost:9000.
        access_key: access key (user name).
        secret_key: secret key (password).

    Returns:
        botocore.client.S3: a configured client. No request is sent yet.
    """
    settings = dict(
        region_name="us-east-1",                  # MinIO's default region
        s3={"addressing_style": "path"},          # http://host:9000/<bucket>/<key>, not <bucket>.host
        retries={"max_attempts": 5, "mode": "standard"},
        connect_timeout=5,
        read_timeout=60,
    )
    try:
        # Newer boto3 adds checksum headers that some MinIO versions reject.
        config = Config(
            **settings,
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
        )
    except TypeError:
        # Older botocore doesn't know these options (and doesn't send those headers either).
        config = Config(**settings)

    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        config=config,
    )


def create_s3_client_from_env():
    """Build a boto3 S3 client for MinIO from environment variables (see create_s3_client).

    Arguments:
        None. It reads these environment variables (load your .env first, e.g. with
        load_dotenv):
            MINIO_ENDPOINT: server URL, e.g. http://localhost:9000.
            MINIO_ACCESS_KEY: access key (user name).
            MINIO_SECRET_KEY: secret key (password).

    Returns:
        botocore.client.S3: a configured client. No request is sent yet.

    Raises:
        RuntimeError: one of the three variables is missing or empty.
    """
    required = ["MINIO_ENDPOINT", "MINIO_ACCESS_KEY", "MINIO_SECRET_KEY"]
    missing = [name for name in required if not os.getenv(name)]
    if missing:
        raise RuntimeError(f"Missing environment variables: {missing}. Add them to your .env file.")

    return create_s3_client(
        os.environ["MINIO_ENDPOINT"], os.environ["MINIO_ACCESS_KEY"], os.environ["MINIO_SECRET_KEY"]
    )


def ensure_bucket(s3, bucket: str) -> None:
    """Make sure the bucket exists, creating it if it is missing.

    It also works as a connectivity and credentials check: any error other than "bucket not
    found" (wrong credentials, MinIO unreachable, ...) is raised unchanged.

    Arguments:
        s3: boto3 S3 client.
        bucket: bucket name.

    Raises:
        botocore.exceptions.ClientError: any S3 error other than "bucket not found".
    """
    try:
        s3.head_bucket(Bucket=bucket)
    except ClientError as e:
        if e.response["Error"]["Code"] not in _NOT_FOUND_CODES:
            raise
        logger.warning("Bucket %s does not exist, creating it", bucket)
        s3.create_bucket(Bucket=bucket)


class RawStore:
    """Reads and writes one extraction run in the raw (bronze) layer in MinIO/S3.

    s3://<bucket>/<source>/extract_date=YYYY-MM-DD/run_id=<id>/
        entity=search/dataverse=<name>/part-00000.jsonl.gz            search results, as returned
        entity=dataset_metadata/dataverse=<name>/part-00000.jsonl.gz  full dataset records, as returned
        _manifest.json                                                params, status and counts, timings
        _SUCCESS                                                      exists only if every dataverse succeeded

    Each entity prefix holds one consistent schema, so it can be read like a table.

    Create a new run:        RawStore(s3, bucket, run_params={...})
    Re-open an existing run: RawStore.open_run(s3, bucket, run_id)

    Raw data is never overwritten. This class only deals with storage; it knows nothing about
    the Dataverse API (that is extractor.py).

    Attributes:
        s3: the boto3 S3 client used for every call.
        bucket: bucket name.
        run_id: "<UTC timestamp>_<6 hex chars>", e.g. "20261005T023203Z_0783c0".
        started_at: UTC datetime the run was created.
        run_params: dict describing how the run was configured. Saved in the manifest.
        run_prefix: key prefix of this run inside the bucket.
        run_uri: "s3://<bucket>/<run_prefix>", handy for logs and error messages.
        _dataverses: in-memory state per dataverse (status, error, stored entities with their
            rows / expected_total / size_bytes / file). It is written out by finalize().
    """

    def __init__(self, s3, bucket: str, source: str = "dataverse_search",
                 run_params: dict | None = None, run_id: str | None = None):
        """Start a new run, or attach to an existing one when run_id is given.

        New run (run_id is None): generates a run_id, records the start time and checks the bucket
        (creating it if missing), so an unreachable MinIO or wrong credentials fail now, before
        anything is extracted. Nothing is written for the run until write_dataverse or finalize.
        Existing run (run_id given): loads that run's _manifest.json to restore its state (params and
        per-dataverse status and counts). Prefer open_run(), which calls this.

        Arguments:
            s3: boto3 S3 client (see create_s3_client_from_env).
            bucket: bucket name, e.g. "raw". For a new run it is created if it does not exist.
            source: top-level prefix that groups all runs of this data source. Defaults to
                "dataverse_search".
            run_params: new run only. Dict describing how the run is configured (base_url,
                search_query, date range, "dataverses" list, ...). It is saved in the manifest, and
                its "dataverses" entry lets finalize() detect dataverses that were never processed.
                Never put secrets in it. Ignored when run_id is given (the
                saved params are loaded instead).
            run_id: id of an existing run to attach to, or None to start a new run.

        Raises:
            RawStoreError: run_id is malformed, or (existing run) the run has no manifest.
            botocore.exceptions.ClientError: S3 rejected a request, e.g. wrong credentials.
            botocore.exceptions.EndpointConnectionError: MinIO is unreachable.
        """
        self.s3 = s3
        self.bucket = bucket
        self._dataverses: dict[str, dict] = {}

        if run_id is None:  # ---- new run
            self.started_at = datetime.now(timezone.utc)
            self.run_id = f"{self.started_at:%Y%m%dT%H%M%SZ}_{uuid.uuid4().hex[:6]}"
            self.run_params = run_params or {}
        else:               # ---- existing run (see open_run)
            self.run_id = run_id

        # The run_id starts with the UTC date, which is also the extract_date partition.
        try:
            extract_date = datetime.strptime(self.run_id[:8], "%Y%m%d").strftime("%Y-%m-%d")
        except ValueError as e:
            raise RawStoreError(f"Invalid run_id: {self.run_id!r}") from e
        self.run_prefix = f"{source}/extract_date={extract_date}/run_id={self.run_id}"
        self.run_uri = f"s3://{bucket}/{self.run_prefix}"

        if run_id is None:
            # Fail fast: if MinIO is unreachable or credentials are wrong, stop before extracting anything.
            self._ensure_bucket()
        else:
            self._load_manifest()

    @classmethod
    def open_run(cls, s3, bucket: str, run_id: str, source: str = "dataverse_search") -> "RawStore":
        """Re-open a run that is already stored in the bucket, to read from it or add to it.

        Arguments:
            s3: boto3 S3 client.
            bucket: bucket holding the run.
            run_id: id of the run, e.g. "20261005T023203Z_0783c0". It appears in the logs and in the
                manifest.
            source: prefix the run was stored under. It must match the value used when the run was
                created. Defaults to "dataverse_search".

        Returns:
            RawStore: a store whose state (params, per-dataverse status and entity counts) is
            restored from the run's manifest.

        Raises:
            RawStoreError: run_id is malformed, or the run has no manifest.
        """
        return cls(s3, bucket, source=source, run_id=run_id)

    # ------------------------------------------------------------------ helpers

    def _ensure_bucket(self) -> None:
        """Make sure this run's bucket exists, creating it if it is missing (see ensure_bucket)."""
        ensure_bucket(self.s3, self.bucket)

    def _key(self, entity: str, dataverse: str) -> str:
        """Build the object key of one data file inside this run.

        Arguments:
            entity: kind of data, "search" or "dataset_metadata".
            dataverse: dataverse alias, e.g. "orpertanian".

        Returns:
            str: "<run_prefix>/entity=<entity>/dataverse=<dataverse>/part-00000.jsonl.gz".
        """
        return f"{self.run_prefix}/entity={entity}/dataverse={dataverse}/part-00000.jsonl.gz"

    def _put_json(self, key: str, obj: dict) -> None:
        """Upload a dict as a pretty-printed UTF-8 JSON object (ContentType application/json).

        A single PUT is atomic: readers see the old object or the complete new one, never half.

        Arguments:
            key: full object key inside the bucket.
            obj: JSON-serializable dict.
        """
        body = json.dumps(obj, indent=2, ensure_ascii=False).encode("utf-8")
        self.s3.put_object(Bucket=self.bucket, Key=key, Body=body, ContentType="application/json")

    def _load_manifest(self) -> None:
        """Load this run's _manifest.json and restore started_at, run_params and the per-dataverse state.

        Raises:
            RawStoreError: the manifest does not exist (wrong run_id, wrong source, or the run was
                killed before it could write one).
            botocore.exceptions.ClientError: any other S3 error.
        """
        try:
            obj = self.s3.get_object(Bucket=self.bucket, Key=f"{self.run_prefix}/_manifest.json")
        except ClientError as e:
            if e.response["Error"]["Code"] in _NOT_FOUND_CODES:
                raise RawStoreError(
                    f"Run {self.run_id} not found (or it has no manifest) at {self.run_uri}"
                ) from e
            raise
        manifest = json.loads(obj["Body"].read())
        self.started_at = datetime.fromisoformat(manifest["started_at"])
        self.run_params = manifest["params"]
        self._dataverses = manifest["dataverses"]

    # --------------------------------------------------------------------- read

    def has_entity(self, dataverse: str, entity: str) -> bool:
        """Check whether one entity is already stored for a dataverse.

        It asks the bucket itself (a HEAD request) rather than relying on in-memory state, so it is
        always correct, including for a run that was re-opened.

        Arguments:
            dataverse: dataverse alias.
            entity: "search" or "dataset_metadata".

        Returns:
            bool: True if the file exists, False if it does not.

        Raises:
            botocore.exceptions.ClientError: any S3 error other than "not found".
        """
        try:
            self.s3.head_object(Bucket=self.bucket, Key=self._key(entity, dataverse))
            return True
        except ClientError as e:
            if e.response["Error"]["Code"] in _NOT_FOUND_CODES:
                return False
            raise

    def read_entity(self, dataverse: str, entity: str) -> list[dict]:
        """Load the stored records of one entity of a dataverse back from the bucket.

        The file is streamed through gzip and parsed line by line (JSON Lines), but all records are
        returned as one list, so the whole entity must fit in memory. If the manifest knows how many
        rows were written, the number read is checked against it.

        Arguments:
            dataverse: dataverse alias.
            entity: "search" or "dataset_metadata".

        Returns:
            list[dict]: one dict per stored line, exactly as it was written.

        Raises:
            RawStoreError: nothing is stored for that dataverse and entity, or the number of records
                read differs from the count in the manifest (a corrupt or tampered file).
        """
        try:
            obj = self.s3.get_object(Bucket=self.bucket, Key=self._key(entity, dataverse))
        except ClientError as e:
            if e.response["Error"]["Code"] in _NOT_FOUND_CODES:
                raise RawStoreError(
                    f"No stored {entity} data for dataverse {dataverse} in run {self.run_id}"
                ) from e
            raise

        with gzip.GzipFile(fileobj=obj["Body"]) as gz:
            records = [json.loads(line) for line in io.TextIOWrapper(gz, encoding="utf-8") if line.strip()]

        expected = self._dataverses.get(dataverse, {}).get("entities", {}).get(entity, {}).get("rows")
        if expected is not None and len(records) != expected:
            raise RawStoreError(
                f"Stored {entity} data for dataverse {dataverse} has {len(records)} records, "
                f"but the manifest says {expected}"
            )
        logger.info("Read %s %s records for dataverse %s from the raw store", len(records), entity, dataverse)
        return records

    # -------------------------------------------------------------------- write

    def write_dataverse(self, dataverse: str, entity: str, items: list[dict], expected_total: int) -> str:
        """Save one entity of one dataverse to the bucket as gzipped JSON Lines.

        The records are stored exactly as given (no cleaning). They are compressed into a buffer that
        spills to disk above 64 MB, then uploaded (as a multipart upload for large files). Raw data is
        never overwritten: if the file already exists this raises. The in-memory state is updated
        (rows, expected_total, size_bytes, file), but the manifest in the bucket is only written by
        finalize().

        Arguments:
            dataverse: dataverse alias.
            entity: "search" or "dataset_metadata". It becomes the entity=<...> folder.
            items: the records to store, one per line.
            expected_total: number of records the source said exist. It is saved next to the number
                of rows actually written, so a mismatch can be spotted later.

        Returns:
            str: the s3:// URI of the file that was written.

        Raises:
            RawStoreError: this entity is already stored for the dataverse.
            botocore.exceptions.ClientError: the upload failed.
        """
        key = self._key(entity, dataverse)
        if self.has_entity(dataverse, entity):
            raise RawStoreError(f"{entity} data for dataverse {dataverse} already exists. Raw data is never overwritten.")

        # Compress into a buffer that spills to disk above 64 MB, so memory stays bounded.
        with tempfile.SpooledTemporaryFile(max_size=64 * 1024 * 1024) as buffer:
            with gzip.GzipFile(fileobj=buffer, mode="wb") as gz:
                for item in items:
                    gz.write((json.dumps(item, ensure_ascii=False) + "\n").encode("utf-8"))
            size_bytes = buffer.tell()
            buffer.seek(0)
            # upload_fileobj switches to multipart upload automatically for large files.
            self.s3.upload_fileobj(buffer, self.bucket, key, ExtraArgs={"ContentType": "application/gzip"})

        info = self._dataverses.setdefault(dataverse, {"status": "success", "entities": {}})
        info["entities"][entity] = {
            "rows": len(items),
            "expected_total": expected_total,
            "size_bytes": size_bytes,
            "file": key.removeprefix(self.run_prefix + "/"),
        }
        uri = f"s3://{self.bucket}/{key}"
        logger.info("Wrote %s %s rows for dataverse %s to %s", len(items), entity, dataverse, uri)
        return uri

    def record_failure(self, dataverse: str, error: Exception) -> None:
        """Remember that a dataverse failed, so finalize() marks the whole run as failed.

        Only the in-memory state changes. Entities already stored for the dataverse are kept.

        Arguments:
            dataverse: dataverse alias.
            error: the exception that caused the failure. It is saved in the manifest as
                "<ExceptionType>: <message>".
        """
        info = self._dataverses.setdefault(dataverse, {"status": "success", "entities": {}})
        info["status"] = "failed"
        info["error"] = f"{type(error).__name__}: {error}"

    def clear_failure(self, dataverse: str) -> None:
        """Forget an earlier failure because the dataverse is about to be retried.

        Resets the status to "success" (meaning: no failure recorded) and removes the saved error.
        Does nothing for a dataverse the store does not know yet. If the retry fails as well, call
        record_failure again.

        Arguments:
            dataverse: dataverse alias.
        """
        info = self._dataverses.get(dataverse)
        if info:
            info["status"] = "success"
            info.pop("error", None)

    def finalize(self) -> dict:
        """Write the manifest to the bucket and, only if nothing failed, the _SUCCESS marker.

        Call it once, at the end of a run. First, any dataverse listed in run_params["dataverses"]
        that was never recorded is marked failed ("Never processed"), so a run cannot look successful
        by silently skipping something. The run status is then "failed" if any dataverse failed and
        "success" otherwise. The _SUCCESS file is written last: its existence means everything else
        in the run is complete.

        Returns:
            dict: the manifest that was written, with the keys run_id, location, started_at,
            finished_at, status, params, rows_by_entity (total rows per entity) and dataverses
            (per dataverse: status, error, and per entity rows / expected_total / size_bytes / file).

        Raises:
            botocore.exceptions.ClientError: the manifest or _SUCCESS could not be uploaded.
        """
        # Safety net: a dataverse that was supposed to be in this run but never got recorded
        # must not let the run look successful.
        for dataverse in self.run_params.get("dataverses", []):
            if dataverse not in self._dataverses:
                self._dataverses[dataverse] = {"status": "failed", "entities": {}, "error": "Never processed"}

        failed = [d for d, info in self._dataverses.items() if info["status"] != "success"]
        rows_by_entity: dict[str, int] = {}
        for info in self._dataverses.values():
            for entity, e in info["entities"].items():
                rows_by_entity[entity] = rows_by_entity.get(entity, 0) + e["rows"]

        manifest = {
            "run_id": self.run_id,
            "location": self.run_uri,
            "started_at": self.started_at.isoformat(),
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "status": "failed" if failed else "success",
            "params": self.run_params,
            "rows_by_entity": rows_by_entity,
            "dataverses": self._dataverses,
        }
        self._put_json(f"{self.run_prefix}/_manifest.json", manifest)

        if not failed:
            # Written last: its existence means "everything above is complete".
            self.s3.put_object(Bucket=self.bucket, Key=f"{self.run_prefix}/_SUCCESS", Body=b"")
        logger.info("Run %s finished with status: %s", self.run_id, manifest["status"])
        return manifest