#!/usr/bin/env python3
"""Offline Sockseek stand-in for the rendered request/restart regression."""
import json
import shutil
import sys
from pathlib import Path
from urllib.parse import unquote_plus

root = Path(__file__).resolve().parent
args = sys.argv[1:]
with (root / "calls.jsonl").open("a", encoding="utf8") as calls:
    calls.write(json.dumps(args) + "\n")
if "--print" in args:
    assert "--song" in args and args[args.index("--input-type") + 1] == "string"
    assert args[0] in {"Fixture - Format fixture", "Fixture - Format fixture Live"}
    # Prefer the fast AAC peer: both advertise an identical M4A filename.
    print(json.dumps([
        {"User": {"Username": codec, "UploadSpeed": speed, "HasFreeUploadSlot": True},
         "File": {"Filename": "Music/Format fixture + 100%.m4a"}}
        for codec, speed in [("aac", 10), ("alac", 5)]]))
elif args[0].startswith("slsk://"):
    assert unquote_plus(args[0]).endswith("/Music/Format fixture + 100%.m4a")
    codec = args[0].split("/")[2]
    shutil.copyfile(root / (codec + ".m4a"),
                    Path(args[args.index("--output-dir") + 1]) / "Format fixture.m4a")
else:
    sys.exit("Unexpected Sockseek command")
