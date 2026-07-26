from pathlib import Path

from app.common.config import config_from_mapping
from app.dashboard.server import create_app
from tests.unit.test_config import valid_mapping


def test_dashboard_and_planned_camera_render(tmp_path: Path) -> None:
    config = config_from_mapping(valid_mapping(tmp_path))
    app = create_app(config)
    client = app.test_client()
    response = client.get("/")
    assert response.status_code == 200
    assert b"Today\xe2\x80\x99s recordings" in response.data
    assert b"Camera planned" in response.data


def test_control_rejects_unknown_action(tmp_path: Path) -> None:
    app = create_app(config_from_mapping(valid_mapping(tmp_path)))
    response = app.test_client().post("/api/control", json={"action": "delete"})
    assert response.status_code == 400


def test_download_rejects_path_traversal(tmp_path: Path) -> None:
    app = create_app(config_from_mapping(valid_mapping(tmp_path)))
    response = app.test_client().get("/recordings/file/..%2Fsecret.mkv")
    assert response.status_code == 404
