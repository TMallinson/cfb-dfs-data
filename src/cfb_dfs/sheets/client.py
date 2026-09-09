"""Low-level Sheets API wrapper: retries with backoff, request counting.

Sheets quota is ~60 read and ~60 write requests per minute per user; the
pipeline batches so a full run needs roughly a dozen requests total.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from typing import Any

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from cfb_dfs.logging_setup import get_logger

log = get_logger(__name__)

RETRYABLE = {429, 500, 502, 503, 504}


@dataclass
class SheetsClient:
    credentials: service_account.Credentials
    spreadsheet_id: str
    reads: int = 0
    writes: int = 0
    _service: Any = field(default=None, repr=False)

    @property
    def service(self) -> Any:
        if self._service is None:
            self._service = build(
                "sheets", "v4", credentials=self.credentials, cache_discovery=False
            )
        return self._service

    def _execute(self, request: Any, kind: str, attempts: int = 6) -> Any:
        delay = 1.0
        for attempt in range(1, attempts + 1):
            try:
                result = request.execute(num_retries=0)
            except HttpError as exc:
                status = exc.resp.status
                if status in RETRYABLE and attempt < attempts:
                    sleep_for = delay + random.uniform(0, 0.5)
                    log.warning(
                        "sheets.retry",
                        kind=kind,
                        status=status,
                        attempt=attempt,
                        sleep=round(sleep_for, 1),
                    )
                    time.sleep(sleep_for)
                    delay = min(delay * 2, 32)
                    continue
                raise
            if kind == "read":
                self.reads += 1
            else:
                self.writes += 1
            return result
        raise RuntimeError("unreachable")

    # -- reads -----------------------------------------------------------------------------

    def get_metadata(
        self, fields: str, ranges: list[str] | None = None, include_grid_data: bool = False
    ) -> dict[str, Any]:
        req = self.service.spreadsheets().get(
            spreadsheetId=self.spreadsheet_id,
            fields=fields,
            ranges=ranges or [],
            includeGridData=include_grid_data,
        )
        return self._execute(req, "read")

    def batch_get_values(
        self,
        ranges: list[str],
        render: str = "UNFORMATTED_VALUE",
        date_render: str = "FORMATTED_STRING",
    ) -> list[dict[str, Any]]:
        req = (
            self.service.spreadsheets()
            .values()
            .batchGet(
                spreadsheetId=self.spreadsheet_id,
                ranges=ranges,
                valueRenderOption=render,
                dateTimeRenderOption=date_render,
            )
        )
        return self._execute(req, "read").get("valueRanges", [])

    # -- writes ----------------------------------------------------------------------------

    def batch_update_values(
        self, data: list[dict[str, Any]], value_input: str = "USER_ENTERED"
    ) -> dict[str, Any]:
        req = (
            self.service.spreadsheets()
            .values()
            .batchUpdate(
                spreadsheetId=self.spreadsheet_id,
                body={"valueInputOption": value_input, "data": data},
            )
        )
        return self._execute(req, "write")

    def batch_clear_values(self, ranges: list[str]) -> dict[str, Any]:
        req = (
            self.service.spreadsheets()
            .values()
            .batchClear(spreadsheetId=self.spreadsheet_id, body={"ranges": ranges})
        )
        return self._execute(req, "write")

    def batch_update(self, requests: list[dict[str, Any]]) -> dict[str, Any]:
        if not requests:
            return {}
        req = self.service.spreadsheets().batchUpdate(
            spreadsheetId=self.spreadsheet_id, body={"requests": requests}
        )
        return self._execute(req, "write")
