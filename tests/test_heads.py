"""Marking heads: frames to mark, the detector's guesses, saving, and the training set."""

from __future__ import annotations

import json
import pickle
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from crossing_count.detector import Detection
from crossing_count.heads import (
    DEFAULT_R,
    HeadLabels,
    LabelError,
    add_video,
    busy_times,
    head_points,
    match_heads,
    merge_found,
)
from crossing_count.heads_app import create_label_app
from crossing_count.util import default_run_dir
from crossing_count.video import probe

TILE = {"x0": 640, "y0": 60, "x1": 1280, "y1": 540}


def person(foot: tuple[float, float], centre: tuple[float, float], conf: float = 0.9) -> Detection:
    fy = foot[1]
    cx, cy = centre
    quad = ((cx - 15, 2 * cy - fy), (cx + 15, 2 * cy - fy), (cx + 15, fy), (cx - 15, fy))
    return Detection(bbox=(cx - 15, 2 * cy - fy, cx + 15, fy), foot=foot, conf=conf, quad=quad)


def test_heads_sit_at_the_far_end_of_each_body() -> None:
    (h,) = head_points([person((900, 300), (900, 250))], TILE)
    assert h == [260.0, 150.0, DEFAULT_R]  # 1.8 x (centre - feet) from the feet, picture pixels
    two = head_points([person((900, 300), (900, 250)), person((902, 300), (901, 251), 0.5)], TILE)
    assert len(two) == 1  # the same person found twice is marked once


def test_busy_moments_first_then_spread() -> None:
    recorded = {float(t): ([object()] * (5 if t in (20, 21, 40) else 1)) for t in range(60)}
    times = busy_times(recorded, 60.0, 5)
    assert 20.0 in times and 40.0 in times and 21.0 not in times  # 5 s apart at least
    assert len(times) == 5


def test_frames_are_added_with_guesses_and_marks_are_kept(two_tile_video: dict[str, Any],
                                                          tmp_path: Path) -> None:
    video = two_tile_video["video"]
    run = tmp_path / default_run_dir(video, None)
    (run / "cam-b").mkdir(parents=True)
    (run / "layout.json").write_text(json.dumps({
        "fingerprint": probe(video).fingerprint,
        "cameras": [{"sensor": "CAM-A", "picture": 0}, {"sensor": "CAM-B", "picture": 1}]}))
    frames = {round(t / 10, 3): [person((1060, 300), (1060, 270))] for t in range(600)}
    (run / "cam-b" / "detections.pkl").write_bytes(pickle.dumps({"frames": frames}))
    labels = tmp_path / "labels"
    assert add_video(video, per_camera=3, root=labels, runs_root=tmp_path) == 6
    store = HeadLabels(labels)
    rows = store.summary()
    assert {r["camera"] for r in rows} == {"CAM-A", "CAM-B"}
    b = store.get(next(r["id"] for r in rows if r["camera"] == "CAM-B"))
    assert b["prefilled"] and b["heads"] == [[420.0, 186.0, DEFAULT_R]]
    store.save(b["id"], [[100, 100, 12], [200, 150]])
    assert add_video(video, per_camera=3, root=labels, runs_root=tmp_path) == 0  # marks kept
    assert store.get(b["id"])["heads"] == [[100.0, 100.0, 12.0], [200.0, 150.0, DEFAULT_R]]
    with pytest.raises(LabelError, match="outside"):
        store.save(b["id"], [[900, 10, 12]])
    with pytest.raises(LabelError):
        store.get("../../etc/passwd")


def test_training_set_in_yolo_format(tmp_path: Path) -> None:
    store = HeadLabels(tmp_path / "labels")
    for k in range(5):
        fid = f"clip__cam__{k:08d}"
        (store.frames_dir / f"{fid}.jpg").write_bytes(b"jpg")
        (store.frames_dir / f"{fid}.json").write_text(json.dumps({
            "id": fid, "video": "clip.mp4", "camera": "cam", "t": k, "size": [640, 480],
            "prefill": [], "heads": [], "prefilled": False, "done": False, "skipped": False}))
        store.save(fid, [[320, 240, 16]] if k else [])
    data = store.export_yolo(tmp_path / "set")
    assert (data["train"], data["val"], data["heads"]) == (4, 1, 4)
    line = (tmp_path / "set" / "labels" / "train" / "clip__cam__00000001.txt").read_text()
    assert line == "0 0.500000 0.500000 0.050000 0.066667\n"
    assert "names:\n  0: head" in Path(data["yaml"]).read_text()


def test_training_set_holds_out_a_chosen_video(tmp_path: Path) -> None:
    store = HeadLabels(tmp_path / "labels")
    for k, video in enumerate(["Export - YD-612-PB1.mp4", "Export - RW-128.mp4", "other.mp4"]):
        fid = f"clip{k}__cam__00000000"
        (store.frames_dir / f"{fid}.jpg").write_bytes(b"jpg")
        (store.frames_dir / f"{fid}.json").write_text(json.dumps({
            "id": fid, "video": video, "camera": "cam", "t": 0, "size": [640, 480],
            "prefill": [], "heads": [], "prefilled": False, "done": False, "skipped": False}))
        store.save(fid, [[320, 240, 16]])
    data = store.export_yolo(tmp_path / "set", hold_out="yd-612")
    assert data["val_ids"] == ["clip0__cam__00000000"] and data["train"] == 2
    with pytest.raises(LabelError, match="must match"):
        store.export_yolo(tmp_path / "set", hold_out="nowhere")


def test_matching_found_heads_to_marked_ones() -> None:
    assert match_heads([(100, 100), (300, 300)], [[104, 98, 14], [500, 500, 14]]) == (1, 1, 1)


def test_boxes_on_one_head_become_one_point() -> None:
    pts: list[tuple[float, float]] = [(100, 100), (108, 104), (300, 300)]
    assert merge_found(pts, [0.3, 0.6, 0.2]) == [(108, 104), (300, 300)]  # surest one kept


def test_marking_page_api(two_tile_video: dict[str, Any], tmp_path: Path) -> None:
    client = TestClient(create_label_app(tmp_path / "labels", [two_tile_video["dir"]], tmp_path))
    assert "Mark heads" in client.get("/").text
    assert client.get("/api/state").json()["frames"] == 0
    assert client.post("/api/add", json={"path": str(two_tile_video["video"]),
                                         "per_camera": 2}).json()["started"]
    for _ in range(400):
        st = client.get("/api/state").json()
        if not st["adding"]["busy"]:
            break
        time.sleep(0.05)
    assert st["frames"] == 4 and st["adding"]["error"] is None
    fid = st["frames_list"][0]["id"]
    assert client.get(f"/api/image/{fid}").headers["content-type"] == "image/jpeg"
    saved = client.post(f"/api/frame/{fid}", json={"heads": [[10, 20, 9], [30, 40, 9]]}).json()
    assert saved["done"] == 1 and saved["heads"] == 2
    assert client.post(f"/api/frame/{fid}", json={"heads": [[9999, 1]]}).status_code == 400
