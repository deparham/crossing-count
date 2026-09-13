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
from crossing_count.heads import HeadLabels, LabelError, match_heads

MIN_HEADS = 200


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--epochs", type=int, default=60)
    ap.add_argument("--base", default="yolo11s.pt", help="starting weights in models/")
    ap.add_argument("--imgsz", type=int, default=640)
    ap.add_argument("--force", action="store_true", help=f"train with fewer than {MIN_HEADS} heads")
    ap.add_argument("--hold-out", metavar="TEXT",
                    help="check on the videos whose file name contains TEXT (e.g. a store code)")
    args = ap.parse_args(argv)

    store = HeadLabels()
    st = store.stats()
    print(f"{st['heads']} heads marked on {st['done']} pictures from {len(st['videos'])} video(s)")
    if st["heads"] < MIN_HEADS and not args.force:
        print(f"Mark at least {MIN_HEADS} heads first (about 1000 is the aim), or pass --force.")
        return 2
    run = paths.data_root() / "training" / datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    try:
        data = store.export_yolo(run / "dataset", hold_out=args.hold_out)
    except LabelError as e:
        print(e)
        return 2
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
    # The current one's guesses exist only where it had run; both are scored on those.
    trained = YOLO(str(best))
    confs = (0.15, 0.25, 0.40)
    new = {c: [0, 0, 0] for c in confs}
    new_same = {c: [0, 0, 0] for c in confs}
    old = [0, 0, 0]
    compared = marked_same = 0
    for fid in data["val_ids"]:
        frame = store.get(fid)
        marked = frame["heads"]
        res: Any = trained.predict(str(store.image(fid)), imgsz=args.imgsz, conf=min(confs),
                                   verbose=False)
        boxes = res[0].boxes if res and res[0].boxes is not None else None
        xyxy = boxes.xyxy.tolist() if boxes is not None else []
        sure = boxes.conf.tolist() if boxes is not None else []
        for c in confs:
            found = [((b[0] + b[2]) / 2, (b[1] + b[3]) / 2) for b, s in zip(xyxy, sure) if s >= c]
            score = match_heads(found, marked)
            new[c] = [a + b for a, b in zip(new[c], score, strict=True)]
            if frame.get("prefilled"):
                new_same[c] = [a + b for a, b in zip(new_same[c], score, strict=True)]
        if frame.get("prefilled"):
            compared += 1
            marked_same += len(marked)
            guess = [(p[0], p[1]) for p in frame["prefill"]]
            old = [a + b for a, b in zip(old, match_heads(guess, marked), strict=True)]
    total = sum(len(store.get(fid)["heads"]) for fid in data["val_ids"])
    print(f"\nChecked on {len(data['val_ids'])} pictures kept out of training ({total} marked heads):")
    for c in confs:
        f, inv, miss = new[c]
        print(f"  new head detector, {c:.0%} sure:  found {f}, missed {miss}, invented {inv}")
    if compared:
        print(f"\nOn the {compared} of them the current detector had guessed ({marked_same} heads):")
        print(f"  current detector:              found {old[0]}, missed {old[2]}, invented {old[1]}")
        for c in confs:
            f, inv, miss = new_same[c]
            print(f"  new head detector, {c:.0%} sure:  found {f}, missed {miss}, invented {inv}")
    print(f"\nSaved {out}. It is not used for counting until it proves better on your clips.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
