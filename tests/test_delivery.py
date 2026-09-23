"""Delivering a finished validation to the brand's folder on the shared drive."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from crossing_count import delivery, paths


@pytest.fixture
def drive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A shared drive with the brands' folders already on it, spelled as they are."""
    monkeypatch.setenv("CROSSING_COUNT_HOME", str(tmp_path / "home"))
    root = tmp_path / "External" / "RAG"
    for name in ("Connor", "Johnny Big", "Rockwear", "TAROCASH", "YD"):
        (root / name).mkdir(parents=True)
    (root / "Store_installation Validation.xlsx").write_text("someone else's")
    return root


def test_the_brand_comes_from_the_store_code() -> None:
    assert delivery.brand_of("CN-123") == "Connor"
    assert delivery.brand_of("jb-401") == "Johnny Big"
    assert delivery.brand_of("YD 612") == "YD"
    assert delivery.brand_of("392") is None  # a code with no letters says nothing
    assert delivery.brand_of("") is None
    assert delivery.brand_of("ZZ-1") is None  # a brand nobody has named yet


def test_a_brand_folder_already_there_is_used_as_it_is_spelled(drive: Path) -> None:
    """The folders belong to whoever made them. Making "Johnny Bigg" beside "Johnny Big",
    or "yd" beside "YD", would split a brand's work across two folders."""
    assert delivery.brand_folder(drive, "Johnny Big") == drive / "Johnny Big"
    assert delivery.brand_folder(drive, "YD") == drive / "YD"
    assert delivery.brand_folder(drive, "yd.") == drive / "YD"
    assert delivery.brand_folder(drive, "Tarocash") == drive / "TAROCASH"
    assert delivery.brand_folder(drive, "johnny  big") == drive / "Johnny Big"
    # only case, spaces and punctuation are looked past. A real difference in spelling is a
    # different brand as far as this can tell, so BRANDS holds the drive's own spelling:
    # "Johnny Big" is what the folder is called, and JB files there.
    assert delivery.brand_folder(drive, "Johnny Bigg") == drive / "Johnny Bigg"
    assert delivery.brand_of("JB-401") == "Johnny Big"
    assert delivery.brand_folder(drive, delivery.brand_of("JB-401") or "") == drive / "Johnny Big"
    # a brand with no folder yet is named as we know it, and made when something is delivered
    assert delivery.brand_folder(drive, "Perri Cutten") == drive / "Perri Cutten"


def test_the_store_folder_is_the_brand_the_store_and_the_code(drive: Path) -> None:
    assert delivery.folder_name("Connor", "Tweed Heads", "CN-123") == "Connor Tweed Heads - CN-123"
    assert delivery.folder_name("YD", "", "YD-612") == "YD - YD-612"
    assert "/" not in delivery.folder_name("Connor", "Bondi / Junction", "CN-9")
    where = delivery.where(drive, {"code": "CN-123", "name": "Tweed Heads"})
    assert where == drive / "Connor" / "Connor Tweed Heads - CN-123"


def test_a_code_that_says_no_brand_is_refused_rather_than_guessed(drive: Path) -> None:
    with pytest.raises(delivery.DeliveryError, match="does not say which brand"):
        delivery.where(drive, {"code": "392", "name": "Perri Cutten Armadale"})
    with pytest.raises(delivery.DeliveryError, match="store code is empty"):
        delivery.where(drive, {"code": "", "name": "Somewhere"})
    # until its prefix is named, and then it files like any other
    paths.save_settings({"brands": {"PC": "Perri Cutten"}})
    assert delivery.brand_of("PC-392") == "Perri Cutten"


def test_what_would_be_copied_is_said_before_anything_is(drive: Path, tmp_path: Path) -> None:
    report = tmp_path / "Connor Tweed Heads camera validation.pptx"
    report.write_bytes(b"deck")
    report.with_suffix(".pdf").write_bytes(b"pdf")
    video = tmp_path / "Export - CN-123.mp4"
    video.write_bytes(b"x" * 5000)
    store = {"code": "CN-123", "name": "Tweed Heads"}
    said = delivery.plan(drive, store, report, video)
    assert said["folder"] == str(drive / "Connor" / "Connor Tweed Heads - CN-123")
    assert said["exists"] is False  # nothing has been made yet: this is only what would be
    assert not (drive / "Connor" / "Connor Tweed Heads - CN-123").exists()
    # the report and the footage: the customer gets the report, not the workings
    assert [f["name"] for f in said["files"]] == [
        report.name, report.with_suffix(".pdf").name, video.name]
    assert [f["footage"] for f in said["files"]] == [False, False, True]
    assert said["bytes"] >= 5000

    without = delivery.plan(drive, store, report, None)
    assert [f["name"] for f in without["files"]] == [report.name,
                                                     report.with_suffix(".pdf").name]


def test_delivering_it(drive: Path, tmp_path: Path) -> None:
    report = tmp_path / "r.pptx"
    report.write_bytes(b"deck")
    video = tmp_path / "Export - CN-123.mp4"
    video.write_bytes(b"footage")
    store = {"code": "CN-123", "name": "Tweed Heads"}

    got = delivery.deliver(drive, store, report, video)
    out = drive / "Connor" / "Connor Tweed Heads - CN-123"
    assert Path(got["folder"]) == out and got["footage"] is True
    assert sorted(p.name for p in out.iterdir()) == ["Export - CN-123.mp4", "r.pptx"]
    assert (out / "Export - CN-123.mp4").read_bytes() == b"footage"
    assert len(list(drive.iterdir())) == 6  # no second Connor folder; nothing else touched

    # delivered twice: the first is left exactly as it was, for whoever is relying on it
    report.write_bytes(b"a corrected deck")
    again = delivery.deliver(drive, store, report, None)
    assert again["files"] == ["r (2).pptx"] and again["footage"] is False
    assert (out / "r.pptx").read_bytes() == b"deck"
    assert (out / "r (2).pptx").read_bytes() == b"a corrected deck"


def test_a_drive_that_is_not_there_says_so(tmp_path: Path,
                                           monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CROSSING_COUNT_HOME", str(tmp_path / "home"))
    report = tmp_path / "r.pptx"
    report.write_bytes(b"deck")
    gone = tmp_path / "not-mounted" / "RAG"
    # the folder is worked out without reaching the drive, so it can be shown before trying
    assert delivery.where(gone, {"code": "CN-9", "name": "X"}).name == "Connor X - CN-9"
    monkeypatch.setattr(Path, "mkdir",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("no such volume")))
    with pytest.raises(delivery.DeliveryError, match="Is the drive connected"):
        delivery.deliver(gone, {"code": "CN-9", "name": "X"}, report)


def test_the_whole_way_through_the_pages(two_tile_video: dict[str, Any], tmp_path: Path,
                                         monkeypatch: pytest.MonkeyPatch) -> None:
    """Set the folder, see what would be sent, send it. Nothing moves until it is asked for."""
    from fastapi.testclient import TestClient

    from crossing_count.webapp import Setup
    from crossing_count.wizard import Wizard
    from crossing_count.wizard_app import create_wizard_app

    monkeypatch.setenv("CROSSING_COUNT_HOME", str(tmp_path / "home"))
    drive = tmp_path / "External" / "RAG"
    (drive / "Connor").mkdir(parents=True)
    w = Wizard(two_tile_video["video"], two_tile_video["dir"], tmp_path / "data")
    w.set_mode("manual")
    s = Setup(w.video, two_tile_video["dir"])
    w.set_named_cameras(s.existing(), [t.as_dict() for t in s.tiles],
                        [{"picture": 0, "name": "CN-123-PB1", "include": True},
                         {"picture": 1, "name": "CN-123-R2", "include": False}])
    w.set_direction("in")
    w.manual_add("CN-123-PB1", 2.0, "in")
    w.manual_watched("CN-123-PB1", 0.0, float(w.state["duration_s"]))
    w.manual_done()
    w.set_sensor({"in": 1})
    w.set_store(name="Tweed Heads", code="CN-123", operator="Sam")
    w.make_report()

    client = TestClient(create_wizard_app(two_tile_video["dir"], tmp_path / "data",
                                          folders=[two_tile_video["video"].parent]))
    client.post("/api/open", json={"path": str(two_tile_video["video"])})
    assert client.post("/api/deliver", json={"footage": False}).status_code == 400  # no folder
    client.post("/api/settings", json={"examples_dir": "", "delivery_dir": str(drive)})

    said = client.get("/api/deliver", params={"footage": "false"}).json()["plan"]
    assert said["folder"] == str(drive / "Connor" / "Connor Tweed Heads - CN-123")
    assert not any(f["footage"] for f in said["files"])
    assert not Path(said["folder"]).exists()  # asking is not sending

    with_video = client.get("/api/deliver", params={"footage": "true"}).json()["plan"]
    assert sum(1 for f in with_video["files"] if f["footage"]) == 1
    assert with_video["bytes"] > said["bytes"]

    got = client.post("/api/deliver", json={"footage": True}).json()
    out = Path(got["folder"])
    assert out == drive / "Connor" / "Connor Tweed Heads - CN-123"
    assert got["footage"] is True and two_tile_video["video"].name in got["files"]
    assert (out / two_tile_video["video"].name).is_file()
    assert any(p.suffix == ".pdf" for p in out.iterdir())
    assert got["delivered"]["at"] and len(list(drive.iterdir())) == 1  # no second Connor folder
    # nothing of the validation's own record goes: that stays with the validation
    assert not [p for p in out.iterdir() if p.suffix == ".json"]
    assert [d["action"] for d in Wizard(two_tile_video["video"], two_tile_video["dir"],
                                        tmp_path / "data").state["decisions"]].count("delivered") == 1
