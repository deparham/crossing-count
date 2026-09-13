# -*- mode: python ; coding: utf-8 -*-
# PyInstaller recipe for the Crossing Count program (one folder, then wrapped by
# installer.iss on Windows). Build from the project folder:
#     uv run --group build pyinstaller packaging/crossing_count.spec --noconfirm
# models/ must hold the weights (yolo11s.pt, yolo11m.pt) before building.
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

ROOT = Path(SPECPATH).parent
SCRIPTS = ["gate", "detect", "count", "review", "export", "setup_ui", "proposed",
           "trace_line", "wizard", "label", "train_heads", "retailnext"]

# keyring finds the credential store (Credential Manager, Keychain) through its
# package metadata, so that goes in too.
datas = (collect_data_files("crossing_count") + collect_data_files("ultralytics")
         + copy_metadata("keyring") + collect_data_files("tzdata"))  # tzdata: time zones on Windows
for folder in ("models", "assets", "sites"):
    if (ROOT / folder).is_dir():
        datas.append((str(ROOT / folder), folder))

hiddenimports = (SCRIPTS + collect_submodules("uvicorn") + collect_submodules("crossing_count")
                 + collect_submodules("keyring"))

a = Analysis(
    [str(ROOT / "packaging" / "launcher.py")],
    pathex=[str(ROOT), str(ROOT / "src")],
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["tkinter", "IPython", "notebook", "pytest", "mypy"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="CrossingCount",
    console=True,  # a small window that says the app is running; closing it quits
)
coll = COLLECT(exe, a.binaries, a.datas, name="CrossingCount")
