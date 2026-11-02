# config.py

import os
import re
from datetime import datetime

from dotenv import load_dotenv

class Config:
    """All pipeline settings, read from environment variables (and the .env file).

    Every value is validated when the object is created, so a missing or malformed setting
    stops the run immediately with a clear message, instead of failing later in the middle of
    an extraction.

    Attributes:
        Dataverse (used by extractor.py and main.py):
            api_token: API token, sent as the X-Dataverse-key header.
            base_url: Dataverse server root URL, without a trailing slash.
            search_query: text sent as the search "q" parameter. Defaults to "*" (everything).
            all_dataverse_list: dataverse aliases to extract. At least one, no duplicates.
            search_date_range_start, search_date_range_end: YYYY-MM-DD strings, or None when
                no date filter is wanted. Either both are set or both are None.

        MinIO / S3 raw storage (used by raw_store.py and main.py):
            minio_endpoint: server URL including the scheme, e.g. "http://localhost:9000".
            minio_access_key, minio_secret_key: credentials.
            minio_bucket: bucket for the raw layer. Defaults to "raw".
            minio_silver_bucket: bucket for the silver layer (used by silver.py). Defaults to
                "silver".

        Logging (used by main.py):
            log_file: base name of the log file, without ".log". main.py adds a timestamp and
                the extension. Defaults to "pipeline".

        Later pipeline stages (not used by the extraction yet):
            validation_fields, fuzzy_score_cutoff, pg_host, pg_port, pg_user, pg_password,
            database, pg_schema, gsheet_credential_file, sheet_id.
    """

    def __init__(self, env_file: str | None = None):
        """Load the .env file, then read and validate every setting.

        Arguments:
            env_file: path of the .env file to load. Defaults to None, which searches for a
                file named ".env" starting in the folder of this file and walking up.

        Raises:
            ValueError: a required setting is missing or empty, or a value is malformed (bad
                URL, bad date, bad bucket name, ...). The message names the setting.
        """
        # override=True: values in .env win over variables already set in the process. This
        # avoids stale values in a long-running Jupyter kernel. If your deployment injects the
        # real settings as environment variables and also ships a .env file, change it to
        # override=False so the injected values win.
        load_dotenv(env_file, override=True)

        # ------------------------------------------------------------
        # Dataverse
        # ------------------------------------------------------------

        self.api_token = self._required("API_TOKEN")
        self.base_url = self._url("BASE_URL")

        self.search_query = self._optional("SEARCH_QUERY", "*")

        self.all_dataverse_list = self._list(
            "ALL_DATAVERSE_LIST",
            required=True
        )

        self.search_date_range_start = self._date("SEARCH_DATE_RANGE_START")
        self.search_date_range_end = self._date("SEARCH_DATE_RANGE_END")

        # The extractor only applies the date filter when BOTH dates are set. Setting just one
        # would silently extract every date, so refuse it.
        if (self.search_date_range_start is None) != (self.search_date_range_end is None):
            raise ValueError(
                "Set both SEARCH_DATE_RANGE_START and SEARCH_DATE_RANGE_END, "
                "or leave both empty."
            )
        if (
            self.search_date_range_start
            and self.search_date_range_start > self.search_date_range_end
        ):
            raise ValueError(
                "SEARCH_DATE_RANGE_START must not be later than SEARCH_DATE_RANGE_END."
            )

        # ------------------------------------------------------------
        # MinIO / S3 raw storage
        # ------------------------------------------------------------

        self.minio_endpoint = self._url("MINIO_ENDPOINT")
        self.minio_access_key = self._required("MINIO_ACCESS_KEY")
        self.minio_secret_key = self._required("MINIO_SECRET_KEY")
        self.minio_bucket = self._bucket("MINIO_BUCKET", default="raw")
        self.minio_silver_bucket = self._bucket("MINIO_SILVER_BUCKET", default="silver")

        # ------------------------------------------------------------
        # Logging
        # ------------------------------------------------------------

        # Accept "pipeline" and "pipeline.log" alike: main.py adds the timestamp and ".log".
        self.log_file = self._optional("LOG_FILE", "pipeline").removesuffix(".log")

        # ------------------------------------------------------------
        # Later pipeline stages (validation)
        # ------------------------------------------------------------

        self.validation_fields = self._list(
            "VALIDATION_FIELDS"
        )

        self.fuzzy_score_cutoff = self._int(
            "FUZZY_SCORE_CUTOFF",
            default=90
        )

        # ------------------------------------------------------------
        # PostgreSQL
        # ------------------------------------------------------------

        self.pg_host = self._required("PG_HOST")

        self.pg_port = self._int(
            "PG_PORT",
            default=5432
        )

        self.pg_user = self._required("PG_USER")

        self.pg_password = self._required("PG_PASSWORD")

        self.database = self._required("DATABASE")

        self.pg_schema = self._required("PG_SCHEMA")

        # ------------------------------------------------------------
        # Google Sheets
        # ------------------------------------------------------------

        self.gsheet_credential_file = self._required(
            "GSHEET_CREDENTIAL_FILE"
        )

        self.sheet_id = self._required("SHEET_ID")

    # ================================================================
    # Helper methods
    # ================================================================

    @staticmethod
    def _required(name):
        """Get a required environment variable.

        Arguments:
            name: name of the environment variable, e.g. "API_TOKEN".

        Returns:
            str: the value, exactly as set.

        Raises:
            ValueError: the variable is not set, or is empty / only spaces.
        """
        value = os.getenv(name)

        if value is None or value.strip() == "":
            raise ValueError(
                f"Required environment variable '{name}' is not set."
            )

        return value

    @staticmethod
    def _optional(name, default):
        """Get an optional environment variable, falling back to a default.

        A variable that is set but empty (for example "SEARCH_QUERY=") counts as not set, so
        a blank line in .env can never produce an empty value where a default is expected.

        Arguments:
            name: name of the environment variable.
            default: value to return when the variable is not set or is empty.

        Returns:
            str: the value with surrounding spaces removed, or the default.
        """
        value = os.getenv(name)

        if value is None or value.strip() == "":
            return default

        return value.strip()

    @staticmethod
    def _int(name, default=None):
        """Get an integer environment variable.

        Arguments:
            name: name of the environment variable, e.g. "PG_PORT".
            default: value to return when the variable is not set or is empty. With the
                default None, the variable becomes required.

        Returns:
            int: the parsed value, or the default.

        Raises:
            ValueError: the variable is missing with no default, or is not a valid integer.
        """
        value = os.getenv(name)

        if value is None or value.strip() == "":
            if default is not None:
                return default

            raise ValueError(
                f"Required environment variable '{name}' is not set."
            )

        try:
            return int(value)
        except ValueError:
            raise ValueError(
                f"Environment variable '{name}' "
                f"must be an integer. Got: {value!r}"
            ) from None

    @staticmethod
    def _list(name, required=False):
        """Convert a comma-separated environment variable into a Python list.

        Items are stripped of spaces, empty items are dropped and duplicates are removed
        (the first occurrence wins, so the order is kept). Listing the same dataverse twice
        would otherwise make the run try to write its raw data twice.

        Arguments:
            name: name of the environment variable, e.g. "ALL_DATAVERSE_LIST".
            required: when True, an unset or empty variable raises an error instead of
                returning an empty list. Defaults to False.

        Returns:
            list[str]: the items, e.g. "a, b ,a" gives ["a", "b"].

        Raises:
            ValueError: required is True and the list is empty.
        """
        value = os.getenv(name)

        items = []
        if value is not None:
            items = list(dict.fromkeys(
                item.strip()
                for item in value.split(",")
                if item.strip()
            ))

        if required and not items:
            raise ValueError(
                f"Required environment variable '{name}' is not set "
                f"(expected a comma-separated list)."
            )

        return items

    @staticmethod
    def _date(name):
        """Get an optional date environment variable in YYYY-MM-DD format.

        Arguments:
            name: name of the environment variable, e.g. "SEARCH_DATE_RANGE_START".

        Returns:
            str | None: the date as a YYYY-MM-DD string, or None if the variable is not set
            or is empty.

        Raises:
            ValueError: the value is not a real date in YYYY-MM-DD format.
        """
        value = os.getenv(name)

        if value is None or value.strip() == "":
            return None

        value = value.strip()
        try:
            datetime.strptime(value, "%Y-%m-%d")
        except ValueError:
            raise ValueError(
                f"Environment variable '{name}' must be a date in "
                f"YYYY-MM-DD format. Got: {value!r}"
            ) from None

        return value

    @staticmethod
    def _url(name):
        """Get a required URL environment variable.

        Arguments:
            name: name of the environment variable, e.g. "BASE_URL".

        Returns:
            str: the URL without a trailing slash, so that f"{url}/api/search" never produces
            a double slash.

        Raises:
            ValueError: the variable is missing, or does not start with http:// or https://.
        """
        value = Config._required(name).strip()

        if not value.startswith(("http://", "https://")):
            raise ValueError(
                f"Environment variable '{name}' must start with http:// "
                f"or https://. Got: {value!r}"
            )

        return value.rstrip("/")

    @staticmethod
    def _bucket(name, default):
        """Get an S3/MinIO bucket name and check it follows the bucket naming rules.

        Arguments:
            name: name of the environment variable, e.g. "MINIO_BUCKET".
            default: bucket name to use when the variable is not set or is empty.

        Returns:
            str: a valid bucket name.

        Raises:
            ValueError: the name is not 3-63 characters of lowercase letters, digits, "-"
                and ".", starting and ending with a letter or digit (underscores and capital
                letters are not allowed). Catching this here gives a clear message instead
                of an obscure error from MinIO later.
        """
        value = Config._optional(name, default)

        if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", value):
            raise ValueError(
                f"Environment variable '{name}' is not a valid bucket name: {value!r}. "
                f"Use 3-63 lowercase letters, digits, '-' or '.', with no underscores."
            )

        return value