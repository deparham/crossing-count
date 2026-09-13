"""The setup page's API, on the synthetic two-camera export."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import pytest
from conftest import LINE_A, LINE_B, MASK_A
from fastapi.testclient import TestClient

from crossing_count.config import load_config
from crossing_count.webapp import create_app


@pytest.fixture(scope="module")
def app(two_tile_video: dict[str, Any], tmp_path_factory: pytest.TempPathFactory) -> Any:
    sites = tmp_path_factory.mktemp("sites")
    shutil.copy(two_tile_video["configs"]["CAM-A"], sites / "cam-a.json")
    return TestClient(create_app(two_tile_video["video"], sites)), sites


def draft(**kw: Any) -> dict[str, Any]:
    d: dict[str, Any] = {"picture": 1, "site": "SYN", "sensor": "CAM-B2",
                         "line": [list(p) for p in LINE_B], "inside": [320.0, 380.0]}
    d.update(kw)
    return d


def test_page_is_served_and_self_contained(app: Any) -> None:
    client, _ = app
    html = client.get("/").text
    assert "Camera setup" in html
    assert "http://" not in html and "https://" not in html  # nothing loaded from the internet


def test_info_lists_pictures_and_matches_saved_cameras(app: Any) -> None:
    client, _ = app
    info = client.get("/api/info").json()
    assert len(info["pictures"]) == 2
    (cam,) = info["configs"]
    assert cam["sensor"] == "CAM-A" and cam["picture"] == 0 and cam["match"] > 0.8


def test_picture_is_a_crop_of_one_camera(app: Any) -> None:
    client, _ = app
    for query in ("", "?t=5"):
        r = client.get(f"/api/picture/1.jpg{query}")
        assert r.headers["content-type"] == "image/jpeg"
        img = cv2.imdecode(np.frombuffer(r.content, np.uint8), cv2.IMREAD_COLOR)
        assert img is not None and img.shape[:2] == (480, 640)
    assert client.get("/api/picture/9.jpg").status_code == 404


def test_incomplete_draft_explains_what_is_missing(app: Any) -> None:
    client, _ = app
    v = client.post("/api/validate", json={"picture": 1}).json()
    assert not v["ok"]
    text = " ".join(v["errors"])
    assert "counting line" in text and "inside" in text and "sensor" in text


def test_valid_draft_reports_rule_side_and_line_match(app: Any) -> None:
    client, _ = app
    v = client.post("/api/validate", json=draft()).json()
    assert v["ok"], v["errors"]
    assert v["inside_side"] == "below" and v["rule"] == "line"
    assert v["line_match"] > 0.8


def test_mask_on_the_wrong_side_is_an_error(app: Any) -> None:
    client, _ = app
    bad_mask = [[100.0, 150.0], [500.0, 150.0], [500.0, 180.0], [100.0, 180.0]]
    v = client.post("/api/validate", json=draft(mask_zone=bad_mask)).json()
    assert not v["ok"] and any("inside" in e for e in v["errors"])


def test_thin_mask_with_a_dwell_warns(app: Any) -> None:
    client, _ = app
    thin = [[100.0, 330.0], [500.0, 320.0], [500.0, 330.0], [100.0, 340.0]]
    v = client.post("/api/validate", json=draft(mask_zone=thin, min_dwell_in_zone_s=0.4)).json()
    assert v["ok"] and any("thin mask band" in w for w in v["warnings"])


def test_save_writes_a_valid_config_and_asks_before_overwriting(app: Any) -> None:
    client, _ = app
    r = client.post("/api/save", json=draft(filter_zones=[[[60, 250], [600, 250], [600, 470], [60, 470]]]))
    assert r.json()["saved"]
    path = Path(r.json()["path"])
    cfg = load_config(path)
    assert cfg.sensor == "CAM-B2" and cfg.rule == "line+filter" and cfg.overlay_hsv is not None
    assert client.post("/api/save", json=draft()).json()["exists"]
    assert client.post("/api/save", json=draft(overwrite=True)).json()["saved"]
    assert json.loads(path.read_text())["filter_zones"] == []


def test_load_saved_camera_back_into_picture_pixels(app: Any) -> None:
    client, sites = app
    c = client.get("/api/config", params={"path": str(sites / "cam-a.json"), "picture": 0}).json()
    assert np.allclose(c["line"], LINE_A, atol=0.5)
    assert np.allclose(c["mask_zone"], MASK_A, atol=0.5)
    assert c["inside"][1] > 190  # inside is below line A


def test_a_saved_camera_with_a_crossed_zone_still_opens_so_it_can_be_fixed(
    two_tile_video: dict[str, Any], tmp_path: Path
) -> None:
    raw = json.loads(two_tile_video["configs"]["CAM-A"].read_text())
    raw["mask_zone"] = [[0.2, 0.5], [0.2, 0.55], [0.8, 0.5], [0.8, 0.55]]  # bow tie
    (tmp_path / "bad.json").write_text(json.dumps(raw))
    client = TestClient(create_app(two_tile_video["video"], tmp_path))
    info = client.get("/api/info")
    assert info.status_code == 200
    (cam,) = info.json()["configs"]
    assert cam["picture"] == 0 and "edges cross" in cam["problem"]
    shapes = client.get("/api/config", params={"path": str(tmp_path / "bad.json"), "picture": 0})
    assert shapes.status_code == 200 and len(shapes.json()["mask_zone"]) == 4
    v = client.post("/api/validate", json={"picture": 0, "site": "S", "sensor": "CAM-A",
                                           **{k: shapes.json()[k] for k in ("line", "inside", "mask_zone")}})
    assert not v.json()["ok"] and any("edges cross" in e for e in v.json()["errors"])


def test_configs_outside_the_sites_folder_cannot_be_read(app: Any, tmp_path: Path) -> None:
    client, _ = app
    other = tmp_path / "x.json"
    other.write_text("{}")
    assert client.get("/api/config", params={"path": str(other), "picture": 0}).status_code == 400
