import json
import os
import stat
import urllib.parse
from pathlib import Path

from app.offload.google_drive import (
    CredentialStore,
    GoogleOAuth,
    OAuthClientCredentials,
    TokenStore,
    credentials_from_mapping,
)


class JsonResponse:
    def __init__(self, value: dict):
        self.value = value

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self) -> bytes:
        return json.dumps(self.value).encode()


def test_oauth_json_is_stored_privately_and_only_exposes_a_hint(tmp_path: Path) -> None:
    credentials = credentials_from_mapping(
        {
            "installed": {
                "client_id": "1234567890-example.apps.googleusercontent.com",
                "client_secret": "private-client-secret",
            }
        }
    )
    store = CredentialStore(tmp_path / "oauth.json")
    store.save(credentials)

    assert store.load() == credentials
    if os.name != "nt":
        assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    public = credentials.public_dict()
    assert public["client_id_hint"].endswith(".apps.googleusercontent.com")
    assert "private-client-secret" not in json.dumps(public)
    assert "1234567890-example" not in json.dumps(public)


def test_device_and_refresh_token_requests_include_client_secret(tmp_path: Path) -> None:
    requests: list[dict[str, list[str]]] = []

    def opener(request, timeout):
        assert timeout == 60
        values = urllib.parse.parse_qs(request.data.decode())
        requests.append(values)
        if "device_code" in values:
            return JsonResponse(
                {"access_token": "access", "refresh_token": "refresh"}
            )
        return JsonResponse({"access_token": "refreshed"})

    credentials = OAuthClientCredentials(
        "client.apps.googleusercontent.com", "client-secret"
    )
    oauth = GoogleOAuth(
        credentials,
        TokenStore(tmp_path / "token.json"),
        opener=opener,
    )

    oauth.finish("device-code")
    assert oauth.access_token() == "refreshed"
    assert requests[0]["client_secret"] == ["client-secret"]
    assert requests[1]["client_secret"] == ["client-secret"]
