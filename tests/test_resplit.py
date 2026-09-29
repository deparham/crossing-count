"""Saying how many cameras a video holds, when the split came out wrong.

The last word after what the export said and what the picture looks like: whoever is about to
count knows how many entrances they are looking at.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from crossing_count.wizard import Wizard
from crossing_count.wizard_app import create_wizard_app


@pytest.fixture
def open_video(two_tile_video: dict[str, Any], tmp_path: Path,
               monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("CROSSING_COUNT_HOME", str(tmp_path / "home"))
    client = TestClient(create_wizard_app(two_tile_video["dir"], tmp_path / "data",
                                          folders=[two_tile_video["video"].parent]))
    client.post("/api/open", json={"path": str(two_tile_video["video"])})
    return client


def test_a_person_can_say_how_many_cameras_there_are(open_video: TestClient) -> None:
    """The synthetic export really is two cameras side by side."""
    client = open_video
    assert len(client.get("/api/state").json()["pictures"]) == 2

    got = client.post("/api/pictures/split", json={"cameras": 1})
    assert got.status_code == 200, got.text
    assert got.json()["pictures"] == 1
    assert len(client.get("/api/state").json()["pictures"]) == 1

    back = client.post("/api/pictures/split", json={"cameras": 2}).json()
    assert back["pictures"] == 2


def test_what_a_person_says_is_taken_even_when_the_picture_disagrees(
        open_video: TestClient) -> None:
    """The point of saying so: their word beats the pixels. Asked for three on a two-camera
    export it gives three, and they see the three pictures and can put it back. The tool
    guessing three here is what the layout guards refuse; a person asking for it is not a
    guess, and the pictures are in front of them to judge."""
    client = open_video
    assert client.post("/api/pictures/split", json={"cameras": 3}).json()["pictures"] == 3
    assert len(client.get("/api/state").json()["pictures"]) == 3
    assert client.post("/api/pictures/split", json={"cameras": 2}).json()["pictures"] == 2


def test_a_count_outside_what_a_video_holds_is_refused(open_video: TestClient) -> None:
    for n in (0, -1, 7):
        said = open_video.post("/api/pictures/split", json={"cameras": n})
        assert said.status_code == 400, n
        assert "between 1 and" in said.json()["detail"].lower()


def test_the_cameras_named_against_the_old_pictures_are_forgotten(
        two_tile_video: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A picture is referred to by its place in the video, so cutting it up differently makes
    every one of those references mean something else."""
    from crossing_count.webapp import Setup

    monkeypatch.setenv("CROSSING_COUNT_HOME", str(tmp_path / "home"))
    w = Wizard(two_tile_video["video"], two_tile_video["dir"], tmp_path / "data")
    w.set_mode("manual")
    s = Setup(w.video, two_tile_video["dir"])
    w.set_named_cameras(s.existing(), [t.as_dict() for t in s.tiles],
                        [{"picture": 0, "name": "CN-123-PB1", "include": True},
                         {"picture": 1, "name": "CN-123-R2", "include": True}])
    assert len(w.state["cameras"]) == 2

    client = TestClient(create_wizard_app(two_tile_video["dir"], tmp_path / "data",
                                          folders=[two_tile_video["video"].parent]))
    client.post("/api/open", json={"path": str(two_tile_video["video"])})
    said = client.post("/api/pictures/split", json={"cameras": 1}).json()
    assert "forgotten" in " ".join(said["notes"])
    assert client.get("/api/state").json()["cameras"] == []
    again = Wizard(two_tile_video["video"], two_tile_video["dir"], tmp_path / "data")
    assert [d["action"] for d in again.state["decisions"]].count("resplit") == 1


def test_it_will_not_resplit_once_crossings_have_been_counted(
        two_tile_video: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Those answers are tied to cameras that are about to stop existing."""
    from crossing_count.webapp import Setup

    monkeypatch.setenv("CROSSING_COUNT_HOME", str(tmp_path / "home"))
    w = Wizard(two_tile_video["video"], two_tile_video["dir"], tmp_path / "data")
    w.set_mode("manual")
    s = Setup(w.video, two_tile_video["dir"])
    w.set_named_cameras(s.existing(), [t.as_dict() for t in s.tiles],
                        [{"picture": 0, "name": "CN-123-PB1", "include": True},
                         {"picture": 1, "name": "CN-123-R2", "include": False}])
    w.set_direction("in")
    w.manual_add("CN-123-PB1", 2.0, "in")

    client = TestClient(create_wizard_app(two_tile_video["dir"], tmp_path / "data",
                                          folders=[two_tile_video["video"].parent]))
    client.post("/api/open", json={"path": str(two_tile_video["video"])})
    said = client.post("/api/pictures/split", json={"cameras": 1})
    assert said.status_code == 400
    assert "counted by hand" in said.json()["detail"] and "start over" in said.json()["detail"]
    assert len(client.get("/api/state").json()["pictures"]) == 2  # untouched
    kept = Wizard(two_tile_video["video"], two_tile_video["dir"], tmp_path / "data")
    assert [c["sensor"] for c in kept.state["cameras"]] == ["CN-123-PB1"]  # and so are these
