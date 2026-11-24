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
    if args[0] in {"Fixture - Older song", "Fixture - Direct song"}:
        title = args[0].removeprefix("Fixture - ")
        print(json.dumps([{"User": {"Username": "mp3", "HasFreeUploadSlot": True},
                           "File": {"Filename": f"Music/{title}.mp3"}}]
                         if args[args.index("--format") + 1] == "mp3" else []))
        sys.exit()
    if args[0] == "Fixture - AAC fallback":
        formats = args[args.index("--format") + 1].split(",")
        print(json.dumps([{"User": {"Username": "aac-fallback", "HasFreeUploadSlot": True},
                           "File": {"Filename": "Music/AAC fallback.m4a"}}] if "m4a" in formats else []))
        sys.exit()
    assert args[0] in {"Fixture - Format fixture", "Fixture - Format fixture Live"}
    # Prefer the fast AAC peer: both advertise an identical M4A filename.
    print(json.dumps([
        {"User": {"Username": codec, "UploadSpeed": speed, "HasFreeUploadSlot": True},
         "File": {"Filename": "Music/Format fixture + 100%.m4a"}}
        for codec, speed in [("aac", 10), ("alac", 5)]]))
elif args[0].startswith("slsk://"):
    codec = args[0].split("/")[2]
    individual = codec in {"mp3", "aac-fallback"}
    name = Path(unquote_plus(args[0])).name if individual else "Format fixture.m4a"
    assert unquote_plus(args[0]).endswith("/Music/" + (name if individual else "Format fixture + 100%.m4a"))
    source = name if individual else codec + ".m4a"
    shutil.copyfile(root / source, Path(args[args.index("--output-dir") + 1]) / name)
else:
    sys.exit("Unexpected Sockseek command")
