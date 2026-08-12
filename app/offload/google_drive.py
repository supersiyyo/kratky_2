from __future__ import annotations

import json
import hashlib
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

from app.capture.state import atomic_write_json, read_json


DEVICE_CODE_URL = "https://oauth2.googleapis.com/device/code"
TOKEN_URL = "https://oauth2.googleapis.com/token"
DRIVE_API = "https://www.googleapis.com/drive/v3"
DRIVE_UPLOAD_API = "https://www.googleapis.com/upload/drive/v3"
DRIVE_FILE_SCOPE = "https://www.googleapis.com/auth/drive.file"


class GoogleDriveError(RuntimeError):
    pass


class AuthorizationPending(GoogleDriveError):
    pass


@dataclass(frozen=True, slots=True)
class OAuthClientCredentials:
    client_id: str
    client_secret: str

    def public_dict(self) -> dict[str, str]:
        identifier = self.client_id.split(".", 1)[0]
        hint = f"…{identifier[-8:]}.apps.googleusercontent.com"
        fingerprint = hashlib.sha256(self.client_id.encode()).hexdigest()[:12]
        return {"client_id_hint": hint, "fingerprint": fingerprint}


class CredentialStore:
    def __init__(self, path: Path):
        self.path = path

    def load(self) -> OAuthClientCredentials | None:
        value = read_json(self.path, None)
        try:
            return credentials_from_mapping(value)
        except GoogleDriveError:
            return None

    def save(self, credentials: OAuthClientCredentials) -> None:
        atomic_write_json(
            self.path,
            {
                "client_id": credentials.client_id,
                "client_secret": credentials.client_secret,
            },
            mode=0o600,
        )

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)


def credentials_from_mapping(value: Any) -> OAuthClientCredentials:
    if not isinstance(value, dict):
        raise GoogleDriveError("OAuth credential file must contain a JSON object")
    candidate = value.get("installed", value)
    if not isinstance(candidate, dict):
        raise GoogleDriveError("OAuth credential JSON has an invalid installed section")
    client_id = str(candidate.get("client_id", "")).strip()
    client_secret = str(candidate.get("client_secret", "")).strip()
    if not client_id.endswith(".apps.googleusercontent.com"):
        raise GoogleDriveError("OAuth credential JSON does not contain a valid Google client ID")
    if not client_secret:
        raise GoogleDriveError("OAuth credential JSON does not contain a client secret")
    return OAuthClientCredentials(client_id, client_secret)


@dataclass(frozen=True, slots=True)
class DeviceAuthorization:
    device_code: str
    user_code: str
    verification_url: str
    expires_in: int
    interval: int

    def public_dict(self) -> dict[str, object]:
        return {
            "user_code": self.user_code,
            "verification_url": self.verification_url,
            "expires_in": self.expires_in,
            "interval": self.interval,
        }


class TokenStore:
    def __init__(self, path: Path):
        self.path = path

    def load(self) -> dict[str, Any] | None:
        value = read_json(self.path, None)
        return value if isinstance(value, dict) and value.get("refresh_token") else None

    def save(self, value: dict[str, Any]) -> None:
        atomic_write_json(self.path, value, mode=0o600)

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)


class GoogleOAuth:
    def __init__(
        self,
        credentials: OAuthClientCredentials,
        token_store: TokenStore,
        opener: Any = urllib.request.urlopen,
    ) -> None:
        self.credentials = credentials
        self.token_store = token_store
        self.opener = opener

    def start(self) -> DeviceAuthorization:
        payload = self._form(
            DEVICE_CODE_URL,
            {"client_id": self.credentials.client_id, "scope": DRIVE_FILE_SCOPE},
        )
        return DeviceAuthorization(
            str(payload["device_code"]),
            str(payload["user_code"]),
            str(payload.get("verification_url") or payload["verification_uri"]),
            int(payload["expires_in"]),
            int(payload.get("interval", 5)),
        )

    def finish(self, device_code: str) -> dict[str, Any]:
        try:
            token = self._form(
                TOKEN_URL,
                {
                    "client_id": self.credentials.client_id,
                    "client_secret": self.credentials.client_secret,
                    "device_code": device_code,
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                },
            )
        except GoogleDriveError as exc:
            if "authorization_pending" in str(exc) or "slow_down" in str(exc):
                raise AuthorizationPending("waiting for Google authorization") from exc
            raise
        self.token_store.save(token)
        return token

    def access_token(self) -> str:
        token = self.token_store.load()
        if token is None:
            raise GoogleDriveError("Google Drive is not connected")
        refreshed = self._form(
            TOKEN_URL,
            {
                "client_id": self.credentials.client_id,
                "client_secret": self.credentials.client_secret,
                "refresh_token": str(token["refresh_token"]),
                "grant_type": "refresh_token",
            },
        )
        if "refresh_token" not in refreshed:
            refreshed["refresh_token"] = token["refresh_token"]
        self.token_store.save(refreshed)
        return str(refreshed["access_token"])

    def _form(self, url: str, values: dict[str, str]) -> dict[str, Any]:
        request = urllib.request.Request(
            url,
            data=urllib.parse.urlencode(values).encode(),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        return _json_request(self.opener, request)


class GoogleDriveClient:
    def __init__(
        self,
        oauth: GoogleOAuth,
        opener: Any = urllib.request.urlopen,
    ) -> None:
        self.oauth = oauth
        self.opener = opener

    def create_folder(self, name: str, parent_id: str | None = None) -> dict[str, Any]:
        metadata: dict[str, Any] = {
            "name": name,
            "mimeType": "application/vnd.google-apps.folder",
        }
        if parent_id:
            metadata["parents"] = [parent_id]
        return self._json(
            "POST",
            f"{DRIVE_API}/files?fields=id,name,webViewLink",
            metadata,
        )

    def file_metadata(self, file_id: str) -> dict[str, Any]:
        fields = "id,name,size,md5Checksum,webViewLink,trashed"
        return self._json("GET", f"{DRIVE_API}/files/{file_id}?fields={fields}")

    def begin_upload(
        self,
        name: str,
        parent_id: str,
        size: int,
        mime_type: str = "application/octet-stream",
    ) -> str:
        token = self.oauth.access_token()
        request = urllib.request.Request(
            f"{DRIVE_UPLOAD_API}/files?uploadType=resumable&fields=id,name,size,md5Checksum,webViewLink",
            data=json.dumps({"name": name, "parents": [parent_id]}).encode(),
            method="POST",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json; charset=UTF-8",
                "X-Upload-Content-Type": mime_type,
                "X-Upload-Content-Length": str(size),
            },
        )
        try:
            with self.opener(request, timeout=60) as response:
                location = response.headers.get("Location")
        except urllib.error.HTTPError as exc:
            raise _http_error(exc) from exc
        if not location:
            raise GoogleDriveError("Google did not return a resumable upload URL")
        return str(location)

    def upload(
        self,
        session_url: str,
        source: BinaryIO,
        size: int,
        offset: int,
        chunk_size: int,
        progress: Any | None = None,
    ) -> dict[str, Any]:
        source.seek(offset)
        while offset < size:
            block = source.read(min(chunk_size, size - offset))
            if not block:
                raise GoogleDriveError("local file ended before its recorded size")
            final = offset + len(block) - 1
            request = urllib.request.Request(
                session_url,
                data=block,
                method="PUT",
                headers={
                    "Content-Length": str(len(block)),
                    "Content-Range": f"bytes {offset}-{final}/{size}",
                },
            )
            try:
                with self.opener(request, timeout=180) as response:
                    body = response.read()
                    status = response.status
                    range_header = response.headers.get("Range")
            except urllib.error.HTTPError as exc:
                if exc.code == 308:
                    status = 308
                    body = b""
                    range_header = exc.headers.get("Range")
                else:
                    raise _http_error(exc) from exc
            if status in {200, 201}:
                return json.loads(body or b"{}")
            if status != 308:
                raise GoogleDriveError(f"unexpected Google upload status: {status}")
            offset = _next_offset(range_header, final + 1)
            if progress:
                progress(offset)
        raise GoogleDriveError("Google upload completed without file metadata")

    def _json(
        self,
        method: str,
        url: str,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        token = self.oauth.access_token()
        request = urllib.request.Request(
            url,
            data=json.dumps(body).encode() if body is not None else None,
            method=method,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )
        return _json_request(self.opener, request)


def _next_offset(range_header: str | None, fallback: int) -> int:
    if not range_header or "-" not in range_header:
        return fallback
    try:
        return int(range_header.rsplit("-", 1)[1]) + 1
    except ValueError:
        return fallback


def _json_request(opener: Any, request: urllib.request.Request) -> dict[str, Any]:
    try:
        with opener(request, timeout=60) as response:
            return json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        raise _http_error(exc) from exc
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise GoogleDriveError(str(exc)) from exc


def _http_error(exc: urllib.error.HTTPError) -> GoogleDriveError:
    try:
        detail = exc.read().decode("utf-8", errors="replace")
    except OSError:
        detail = str(exc)
    return GoogleDriveError(f"Google API error {exc.code}: {detail[:1000]}")
