# extractor.py

import asyncio
import logging

import aiohttp

logger = logging.getLogger(__name__)

# Worth retrying: rate limiting and temporary server-side problems.
# Anything else (401, 403, 404, ...) means our request is wrong, so retrying won't help.
RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class ExtractionError(Exception):
    """Base class for anything that makes an extraction run untrustworthy.

    Catch this one class to handle every extraction failure in a single place (extractor_main.py does).
    Subclasses say what went wrong: RequestFailedError and IncompleteExtractionError.
    """


class RequestFailedError(ExtractionError):
    """A request failed for good: a non-retryable HTTP status (401, 403, 404, ...), all retry
    attempts used up, or an HTTP 200 answer whose body reports an error status.
    """


class IncompleteExtractionError(ExtractionError):
    """The requests worked, but fewer records were retrieved than the API said exist.

    Raised by extract_dataverse (fewer search results than total_count) and by
    fetch_dataset_metadata (at least one dataset's full record could not be fetched).
    """


class DatasetExtract:
    """Fetches dataset metadata from the Dataverse API in two steps: a search (summary
    records) and an enrichment (full records).

    Use it as an async context manager so the HTTP session is opened and closed for you:

        async with DatasetExtract(config) as extractor:
            items, expected_total = await extractor.extract_dataverse("orpertanian")
            records, total = await extractor.fetch_dataset_metadata("orpertanian", items)

    This class only talks to the Dataverse API and returns plain Python lists. It knows nothing
    about storage; saving is done by RawStore (raw_store.py). Every failure raises an
    ExtractionError subclass, so a partial result can never be mistaken for a complete one.

    Attributes:
        config: the settings object passed in.
        base_url: Dataverse server root URL.
        dataverse_list: dataverse aliases from the config (the methods take the alias as an
            argument, so this is only kept for convenience).
        search_query: text sent as the "q" parameter of the search ("*" means everything).
        start_date, end_date: optional YYYY-MM-DD bounds of the date filter. The filter is only
            applied when both are set.
        retries: attempts per request before giving up (default 5).
        backoff_seconds: base wait between attempts, doubled after each one (default 1 second).
        max_concurrency: maximum simultaneous requests during enrichment (default 35).
        timeout: aiohttp.ClientTimeout applied to every request (60 seconds in total).
        _session: the aiohttp session. None outside the ``async with`` block.
    """

    def __init__(self, config):
        """Copy the settings from the config object and set the request tunables.

        No network call is made here. The HTTP session is opened by __aenter__.

        Arguments:
            config: settings object (see config.py). It must provide:
                base_url: Dataverse server root URL.
                all_dataverse_list: list of dataverse aliases.
                search_query: the "q" search text, "*" for everything.
                search_date_range_start, search_date_range_end: YYYY-MM-DD strings, or empty/None
                    for no date filter.
        """
        self.config = config
        self.base_url = self.config.base_url
        self.dataverse_list = self.config.all_dataverse_list
        self.search_query = self.config.search_query
        self.start_date = self.config.search_date_range_start
        self.end_date = self.config.search_date_range_end

        self.retries = 5
        self.backoff_seconds = 1  # waits 1s, 2s, 4s, 8s between attempts
        self.max_concurrency = 35  # max simultaneous requests during enrichment
        self.timeout = aiohttp.ClientTimeout(total=60)
        self._session = None

    async def __aenter__(self):
        """Open the shared HTTP session when entering ``async with DatasetExtract(config)``.

        One aiohttp.ClientSession is reused for every request, so connections are pooled instead of
        being re-opened each time. The session applies self.timeout to every request.

        Returns:
            DatasetExtract: this instance (the name after ``as``).
        """
        self._session = aiohttp.ClientSession(timeout=self.timeout)
        return self

    async def __aexit__(self, exc_type, exc, tb):
        """Close the HTTP session when leaving the ``async with`` block, even after an error.

        Arguments:
            exc_type: exception class raised inside the block, or None. Unused.
            exc: exception instance raised inside the block, or None. Unused.
            tb: traceback of that exception, or None. Unused.

        Returns:
            None. Returning None (falsy) means exceptions raised inside the block are not suppressed.
        """
        await self._session.close()

    async def _request_with_retry(self, url, params=None) -> dict:
        """GET a URL with retries and exponential backoff, and return the parsed JSON body.

        Retried (up to self.retries attempts): HTTP 429, 500, 502, 503, 504, network errors and
        timeouts. Between attempts it waits backoff_seconds * 2^(attempt - 1) seconds (1, 2, 4, 8
        with the defaults) and does not wait after the last attempt.
        Not retried: any other non-200 status (401, 403, 404, ...). Repeating the same request
        cannot fix those, so they fail immediately.

        Arguments:
            url: full URL to request.
            params: optional query-string parameters, URL-encoded by aiohttp. Defaults to None.

        Returns:
            dict: the parsed JSON of an HTTP 200 response. Never None.

        Raises:
            RequestFailedError: a non-retryable HTTP status, or all attempts failed.
            Other unexpected exceptions (for example invalid JSON in the body) are not caught here
            and propagate to the caller.
        """
        last_error = None

        for attempt in range(1, self.retries + 1):
            try:
                async with self._session.get(url, params=params) as response:
                    if response.status == 200:
                        return await response.json()

                    body = (await response.text())[:200]
                    last_error = f"HTTP {response.status}: {body}"

                    if response.status not in RETRYABLE_STATUS:
                        # Our request is wrong (wrong URL, ...): fail immediately.
                        raise RequestFailedError(f"Non-retryable error for {url}. {last_error}")

            except RequestFailedError:
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                last_error = f"{type(e).__name__}: {e}"

            logger.warning("Attempt %s/%s failed for %s: %s", attempt, self.retries, url, last_error)

            if attempt < self.retries:  # don't sleep after the final attempt
                await asyncio.sleep(self.backoff_seconds * 2 ** (attempt - 1))

        raise RequestFailedError(f"All {self.retries} attempts failed for {url}. Last error: {last_error}")

    async def extract_dataverse_aliases(self) -> tuple[list[str], int]:
        """Fetch the alias of every dataverse in the installation using the search API.

        Pages through /api/search with type=dataverse, 1000 results at a time (the API maximum),
        the same way extract_dataverse pages through datasets. The query is always "*" (match
        everything) and no date filter or subtree is applied, so the result is every public
        dataverse in the installation, sub-dataverses included. The first page reports total_count, and when
        paging ends the number retrieved is compared with it.

        Returns:
            tuple[list[str], int]: (aliases, expected_total). aliases are the "identifier" values
            of the search records, duplicates removed, in first-seen order. expected_total is the
            total_count the API reported.

        Raises:
            RequestFailedError: a request failed for good, or the API answered HTTP 200 with a
                status other than "OK".
            IncompleteExtractionError: fewer records were retrieved than total_count.
            ExtractionError: one or more records have no (or an empty) identifier, because those
                dataverses could not be searched.
        """
        logger.info("Searching all dataverses in the installation")

        per_page = 1000
        params = {"q": "*", "type": "dataverse", "per_page": per_page}

        results = []
        expected_total = None
        start = 0

        while True:
            params["start"] = start
            logger.info("Requesting dataverses, start: %s, per_page: %s", start, per_page)

            payload = await self._request_with_retry(f"{self.base_url}/api/search", params=params)

            # The API can answer HTTP 200 and still report an error in the body.
            if payload.get("status") != "OK":
                raise RequestFailedError(
                    f"API returned status={payload.get('status')!r} while listing dataverses: {payload}"
                )

            data = payload.get("data", {})

            if expected_total is None:
                expected_total = data.get("total_count", 0)
                logger.info("Total dataverses count: %s", expected_total)

            items = data.get("items", [])
            results.extend(items)
            logger.info("Retrieved %s dataverses", len(items))

            if not items or len(results) >= expected_total:
                break
            start += per_page

        if len(results) < expected_total:
            raise IncompleteExtractionError(f"Retrieved {len(results)} of {expected_total} dataverses")

        missing = sum(1 for item in results if not item.get("identifier"))
        if missing:
            raise ExtractionError(f"{missing} dataverse search result(s) have no identifier")

        aliases = list(dict.fromkeys(item["identifier"] for item in results))
        logger.info("Completed dataverse listing. Total dataverses retrieved: %s", len(aliases))
        return aliases, expected_total

    async def extract_dataverse(self, dataverse: str) -> tuple[list[dict], int]:
        """Fetch every dataset (summary records) in one dataverse using the search API.

        Pages through /api/search 1000 results at a time (the API maximum) and applies the optional
        date filter from the config. The first page reports total_count, the number of datasets the
        API says exist. When paging ends, the number retrieved is compared with it.

        Arguments:
            dataverse: alias of the dataverse to search, e.g. "orpertanian". It is sent as the
                "subtree" filter, so datasets in its sub-dataverses are included.

        Returns:
            tuple[list[dict], int]: (items, expected_total). items are the search result records
            exactly as the API returned them. expected_total is the total_count the API reported.

        Raises:
            RequestFailedError: a request failed for good, or the API answered HTTP 200 with a
                status other than "OK".
            IncompleteExtractionError: fewer records were retrieved than total_count (for example
                an empty page came back early).
        """
        logger.info("Searching datasets in dataverse: %s", dataverse)

        per_page = 1000
        params = {
            "q": self.search_query,
            "type": "dataset",
            "per_page": per_page,
            "subtree": dataverse,
        }

        if self.start_date and self.end_date:
            params["fq"] = f"dateSort:[{self.start_date}T00:00:00Z TO {self.end_date}T00:00:00Z]"
            params["sort"] = "date"
            params["order"] = "asc"
            logger.info("Given date search range : %s - %s", self.start_date, self.end_date)
        else:
            logger.info("No date range specified")

        results = []
        expected_total = None
        start = 0

        while True:
            params["start"] = start
            logger.info("Requesting dataverse: %s, start: %s, per_page: %s", dataverse, start, per_page)

            payload = await self._request_with_retry(f"{self.base_url}/api/search", params=params)

            # The API can answer HTTP 200 and still report an error in the body.
            if payload.get("status") != "OK":
                raise RequestFailedError(
                    f"API returned status={payload.get('status')!r} for dataverse {dataverse}: {payload}"
                )

            data = payload.get("data", {})

            if expected_total is None:
                expected_total = data.get("total_count", 0)
                logger.info("Total datasets count for dataverse %s: %s", dataverse, expected_total)

            items = data.get("items", [])
            results.extend(items)
            logger.info("Retrieved %s datasets for dataverse: %s", len(items), dataverse)

            if not items or len(results) >= expected_total:
                break
            start += per_page

        # The completeness check: this is what stops a partial result passing as a full one.
        if len(results) < expected_total:
            raise IncompleteExtractionError(
                f"Dataverse {dataverse}: retrieved {len(results)} of {expected_total} datasets"
            )

        logger.info("Completed dataverse: %s. Total datasets retrieved: %s", dataverse, len(results))
        return results, expected_total

    @staticmethod
    def _global_ids(search_items: list[dict]) -> list[str]:
        """Extract the unique dataset IDs from search results, ready for the enrichment step.

        Arguments:
            search_items: search result records, as returned by extract_dataverse. Each should have
                a "global_id" key (the dataset's persistent ID, e.g. "doi:10.xxxx/ABC").

        Returns:
            list[str]: the global IDs with duplicates removed, in first-seen order.

        Raises:
            ExtractionError: one or more records have no (or an empty) global_id, because those
                datasets could not be fetched.
        """
        ids = [item.get("global_id") for item in search_items]
        missing = sum(1 for i in ids if not i)
        if missing:
            raise ExtractionError(f"{missing} search result(s) have no global_id")
        return list(dict.fromkeys(ids))

    async def fetch_dataset_metadata(self, dataverse: str, search_items: list[dict]) -> tuple[list[dict], int]:
        """Enrichment step: fetch the full metadata record of every dataset found by the search.

        The search endpoint only returns summary fields. This calls /api/datasets/:persistentId once
        per dataset to get the complete record (citation blocks, files, license, ...). Requests run
        concurrently, at most self.max_concurrency at a time (enforced by a semaphore), and each one
        goes through _request_with_retry. Progress is logged every 100 datasets. One dataset failing
        does not stop the others: all are attempted and the failures are reported together.

        Arguments:
            dataverse: alias of the dataverse the datasets belong to. Only used in log and error
                messages.
            search_items: search result records for that dataverse. This can be the output of
                extract_dataverse or the same data read back from the raw store. Each record must
                contain "global_id".

        Returns:
            tuple[list[dict], int]: (records, expected_total). records are the API responses exactly
            as received (not modified), in the same order as the unique dataset IDs. expected_total
            is the number of unique dataset IDs that were requested.

        Raises:
            ExtractionError: a search record has no global_id.
            IncompleteExtractionError: at least one dataset could not be fetched after retries. The
                full list of failed IDs is logged and the message shows the first three.
        """
        global_ids = self._global_ids(search_items)
        total = len(global_ids)
        logger.info("Fetching full metadata for %s datasets in dataverse: %s", total, dataverse)

        url = f"{self.base_url}/api/datasets/:persistentId/"
        semaphore = asyncio.Semaphore(self.max_concurrency)  # caps simultaneous requests
        finished = 0

        async def fetch_one(global_id: str):
            """Fetch one dataset's full metadata while holding one concurrency permit.

            Arguments:
                global_id: persistent ID of the dataset, e.g. "doi:10.xxxx/ABC".

            Returns:
                tuple: (global_id, payload, error). On success payload is the parsed API response and
                error is None. On failure payload is None and error is an "ErrorType: message" string.
                It does not raise for a failed dataset, so the other fetches keep running.
            """
            nonlocal finished
            async with semaphore:
                try:
                    payload = await self._request_with_retry(url, params={"persistentId": global_id})
                    if payload.get("status") != "OK":
                        raise RequestFailedError(f"API returned status={payload.get('status')!r}")
                    outcome = (global_id, payload, None)
                except Exception as e:  # one bad dataset must not cancel the others
                    logger.debug("Failed to fetch %s: %s", global_id, e)
                    outcome = (global_id, None, f"{type(e).__name__}: {e}")

            finished += 1
            if finished % 100 == 0 or finished == total:
                logger.info("Dataverse %s: processed %s/%s datasets", dataverse, finished, total)
            return outcome

        outcomes = await asyncio.gather(*(fetch_one(g) for g in global_ids))

        records = [payload for _, payload, error in outcomes if error is None]
        failures = {gid: error for gid, _, error in outcomes if error is not None}

        if failures:
            logger.error("Dataverse %s: failed dataset IDs: %s", dataverse, list(failures))
            first = dict(list(failures.items())[:3])  # keep the exception message readable
            raise IncompleteExtractionError(
                f"Dataverse {dataverse}: metadata fetch failed for {len(failures)} of {total} datasets. "
                f"First failures: {first}"
            )

        logger.info("Completed metadata fetch for dataverse: %s. Records: %s", dataverse, len(records))
        return records, total