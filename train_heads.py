#!/usr/bin/env python3
"""Train a head detector on the heads you marked, and compare it with the current one.

    uv run train_heads.py [--epochs 60] [--base yolo11s.pt]

Everything runs on this computer. The marked frames are split: most train the
new detector, the rest (from other videos where possible) are kept back to check
it. On those, the report says how many marked heads each detector found, missed
and invented, so you can see whether the new one is better before using it.
The new weights are saved as models/heads.pt in your data folder.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from datetime import datetime
from typing import Any

from crossing_count import paths
from crossing_count.detector import pick_device
from crossing_count.heads import HeadLabels, match_heads

MIN_HEADS = 200


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--epochs", type=int, default=60)
    ap.add_argument("--base", default="yolo11s.pt", help="starting weights in models/")
    ap.add_argument("--imgsz", type=int, default=640)
    ap.add_argument("--force", action="store_true", help=f"train with fewer than {MIN_HEADS} heads")
    args = ap.parse_args(argv)

    store = HeadLabels()
    st = store.stats()
    print(f"{st['heads']} heads marked on {st['done']} pictures from {len(st['videos'])} video(s)")
    if st["heads"] < MIN_HEADS and not args.force:
        print(f"Mark at least {MIN_HEADS} heads first (about 1000 is the aim), or pass --force.")
        return 2
    run = paths.data_root() / "training" / datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    data = store.export_yolo(run / "dataset")
    print(f"training on {data['train']} pictures, checking on {data['val']}")

    os.environ.setdefault("YOLO_OFFLINE", "1")
    from ultralytics import YOLO, settings  # type: ignore[attr-defined]

    settings.update({"sync": False})  # type: ignore[no-untyped-call]
    base = next((d / args.base for d in paths.models_dirs() if (d / args.base).is_file()), None)
    if base is None:
        print(f"starting weights {args.base} not found in {[str(d) for d in paths.models_dirs()]}")
        return 2
    model = YOLO(str(base))
    model.train(data=data["yaml"], epochs=args.epochs, imgsz=args.imgsz, batch=8,
                device=pick_device(None), plots=False, project=str(run), name="train",
                exist_ok=True, workers=0, verbose=False)
    best = run / "train" / "weights" / "best.pt"
    out = paths.data_root() / "models" / "heads.pt"
    out.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(best, out)

    # The check: the new detector against the current one, on pictures it never trained on.
    trained = YOLO(str(best))
    new = [0, 0, 0]
    old = [0, 0, 0]
    compared = 0
    for fid in data["val_ids"]:
        frame = store.get(fid)
        marked = frame["heads"]
        res: Any = trained.predict(str(store.image(fid)), imgsz=args.imgsz, conf=0.25,
                                   verbose=False)
        boxes = res[0].boxes.xyxy.tolist() if res and res[0].boxes is not None else []
        found = [((b[0] + b[2]) / 2, (b[1] + b[3]) / 2) for b in boxes]
        new = [a + b for a, b in zip(new, match_heads(found, marked), strict=True)]
        if frame.get("prefilled"):
            compared += 1
            guess = [(p[0], p[1]) for p in frame["prefill"]]
            old = [a + b for a, b in zip(old, match_heads(guess, marked), strict=True)]
    total = sum(len(store.get(fid)["heads"]) for fid in data["val_ids"])
    print(f"\nChecked on {len(data['val_ids'])} pictures kept out of training ({total} marked heads):")
    print(f"  new head detector:  found {new[0]}, missed {new[2]}, invented {new[1]}")
    if compared:
        print(f"  current detector (on the {compared} of them it had guessed): found {old[0]}, "
              f"missed {old[2]}, invented {old[1]}")
    print(f"\nSaved {out}. It is not used for counting until it proves better on your clips.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
