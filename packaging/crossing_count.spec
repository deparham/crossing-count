# -*- mode: python ; coding: utf-8 -*-
# PyInstaller recipe for the Crossing Count program (one folder, then wrapped by
# installer.iss on Windows). Build from the project folder:
#     uv run --group build pyinstaller packaging/crossing_count.spec --noconfirm
# models/ must hold the weights (yolo26m.pt, yolo26s.pt, yolo11m.pt, yolo11s.pt) first.
import re
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

ROOT = Path(SPECPATH).parent
SCRIPTS = ["gate", "detect", "setup_ui", "proposed", "trace_line", "wizard", "label",
           "train_heads", "retailnext"]

# keyring finds the credential store (Credential Manager, Keychain) through its
# package metadata, so that goes in too.
datas = (collect_data_files("crossing_count") + collect_data_files("ultralytics")
         + copy_metadata("keyring") + collect_data_files("tzdata")  # tzdata: time zones on Windows
         + collect_data_files("webview"))  # the app's own window (pywebview)
for folder in ("models", "assets", "sites"):
    if (ROOT / folder).is_dir():
        datas.append((str(ROOT / folder), folder))

hiddenimports = (SCRIPTS + collect_submodules("uvicorn") + collect_submodules("crossing_count")
                 + collect_submodules("keyring")
                 + (["webview.platforms.cocoa"] if sys.platform == "darwin"
                    else ["webview.platforms.winforms", "webview.platforms.edgechromium"]))

a = Analysis(
    [str(ROOT / "packaging" / "launcher.py")],
    pathex=[str(ROOT), str(ROOT / "src")],
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["tkinter", "IPython", "notebook", "pytest", "mypy"],
    noarchive=False,
)
pyz = PYZ(a.pure)
MAC = sys.platform == "darwin"
ICON = str(ROOT / "packaging" / "mac" / "CrossingCount.icns")
VERSION = re.search(r'__version__ = "([^"]+)"',
                    (ROOT / "src" / "crossing_count" / "__init__.py").read_text()).group(1)

# Windows: who made this program, shown in the file's properties and in Windows'
# "unknown publisher" prompts. Without it the program has no publisher at all.
VERSION_FILE = None
if sys.platform == "win32":
    numbers = tuple(int(x) for x in (VERSION.split(".") + ["0", "0", "0", "0"])[:4])
    VERSION_FILE = str(Path(workpath) / "version_info.txt")  # PyInstaller's build folder
    Path(workpath).mkdir(parents=True, exist_ok=True)
    Path(VERSION_FILE).write_text(f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers={numbers}, prodvers={numbers}, mask=0x3f, flags=0x0,
                    OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('CompanyName', 'iTOi Solutions'),
      StringStruct('FileDescription', 'Crossing Count - human-verified crossing counts'),
      StringStruct('FileVersion', '{VERSION}'),
      StringStruct('InternalName', 'CrossingCount'),
      StringStruct('LegalCopyright', 'Copyright (c) 2026 Parham Forozan. All rights reserved.'),
      StringStruct('OriginalFilename', 'CrossingCount.exe'),
      StringStruct('ProductName', 'Crossing Count'),
      StringStruct('ProductVersion', '{VERSION}')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])])
""", encoding="utf-8")

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="CrossingCount",
    console=False,  # the app is its own window (the count wizard); closing it quits
    icon=ICON if MAC else None,
    version=VERSION_FILE,
)
coll = COLLECT(exe, a.binaries, a.datas, name="CrossingCount")
if MAC:
    app = BUNDLE(
        coll,
        name="CrossingCount.app",
        icon=ICON,
        bundle_identifier="com.parhamforozan.crossingcount",
        version=VERSION,
        info_plist={
            "CFBundleDisplayName": "CrossingCount",
            "CFBundleShortVersionString": VERSION,
            "NSHumanReadableCopyright": "© 2026 Parham Forozan. All rights reserved.",
            "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": "12.0",
        },
    )
