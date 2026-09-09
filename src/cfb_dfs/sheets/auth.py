"""Service-account credentials from a file path (local) or base64 JSON (CI)."""

from __future__ import annotations

import base64
import json
from pathlib import Path

from google.oauth2 import service_account

from cfb_dfs.config import Secrets

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


class AuthError(Exception):
    pass


def load_credentials(secrets: Secrets) -> service_account.Credentials:
    """Prefer the B64 secret (CI), then the file path (local). Never logs contents."""
    if secrets.google_service_account_b64:
        try:
            info = json.loads(base64.b64decode(secrets.google_service_account_b64))
        except (ValueError, json.JSONDecodeError) as exc:
            raise AuthError("GOOGLE_SERVICE_ACCOUNT_B64 is not valid base64 JSON") from exc
        return service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    if secrets.google_credentials_path:
        path = Path(secrets.google_credentials_path).expanduser()
        if not path.is_file():
            raise AuthError(f"GOOGLE_APPLICATION_CREDENTIALS points to a missing file: {path}")
        return service_account.Credentials.from_service_account_file(str(path), scopes=SCOPES)
    raise AuthError(
        "No Google credentials: set GOOGLE_APPLICATION_CREDENTIALS (path) "
        "or GOOGLE_SERVICE_ACCOUNT_B64 (base64 JSON)."
    )
