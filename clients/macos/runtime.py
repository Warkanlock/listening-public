"""Assemble the offline server tools in a personal Apple silicon build."""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

CHECKOUT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(CHECKOUT / "scripts"))
from provision_sockseek import DEFAULT_PIN, extract_executable, load_pin

PYTHON_URL = ("https://github.com/astral-sh/python-build-standalone/releases/download/"
              "20261110/cpython-3.12.16%2B20261110-aarch64-apple-darwin-pgo%2Blto-full.tar.zst")
PYTHON_SHA = "4da072af9dbbc2557d3aabf51db553a19694f6ac61b5b7169749e4acfa5a052d"
FFMPEG_URL = "https://ffmpeg.org/releases/ffmpeg-9.0.2.tar.xz"
FFMPEG_SHA = "8c3850283eb25fa026482078a04051e0be17347b09ef81a0849bec15a96e002e"

OPUS_URL = "https://downloads.xiph.org/releases/opus/opus-1.5.2.tar.gz"
OPUS_SHA = "65c1d2f78b9f2fb20082c38cbe47c951ad5839345876e46941612ee87f9a7ce1"

# Build the audio tools against the macOS SDK. Disabling autodetection keeps
# build-machine libraries out of the distributable. Native decoders cover
# the room's supported audio formats and the tools retain tag handling.
FFMPEG_OPTIONS = [
    "--disable-autodetect", "--disable-network", "--disable-doc", "--disable-debug",
    "--disable-everything", "--enable-ffmpeg", "--enable-ffprobe",
    "--enable-protocol=file,pipe", "--enable-demuxer=aac,ac3,aiff,ape,asf,caf,flac,matroska,mov,mp3,ogg,wav,wv",
    "--enable-decoder=aac,aac_latm,ac3,alac,ape,flac,mp1,mp2,mp3,opus,pcm_*,vorbis,wavpack,wmav1,wmav2,wmalossless,wmapro",
    "--enable-parser=aac,aac_latm,ac3,flac,mpegaudio,opus,vorbis",
    "--enable-libopus", "--enable-encoder=aac,alac,flac,libopus,pcm_s16le,pcm_s24le",
    "--enable-muxer=adts,flac,ipod,mov,mp4,null,ogg,opus,wav",
    "--enable-filter=aformat,anull,anullsrc,aresample,sine,volume", "--enable-indev=lavfi",
    "--enable-small", "--extra-cflags=-mmacosx-version-min=13.0",
    "--extra-ldflags=-mmacosx-version-min=13.0",
]


def digest(path):
    checksum = hashlib.sha256()
    with path.open("rb") as file:
        for block in iter(lambda: file.read(1024 * 1024), b""):
            checksum.update(block)
    return checksum.hexdigest()


def fetch(cache, filename, url, expected):
    dest = cache / filename
    if dest.is_file() and digest(dest) == expected:
        return dest
    with tempfile.TemporaryDirectory(dir=cache) as staging:
        temporary = Path(staging) / filename
        print(f"Fetching {filename}", flush=True)
        request = urllib.request.Request(url, headers={"User-Agent": "ListeningRoom-Mac-builder"})
        with urllib.request.urlopen(request, timeout=180) as response, temporary.open("wb") as output:
            shutil.copyfileobj(response, output)
        if digest(temporary) != expected:
            raise ValueError(f"Checksum mismatch for {filename}")
        os.replace(temporary, dest)
    return dest


def media_tools(cache, archive, opus_archive):
    identity = json.dumps([FFMPEG_SHA, OPUS_SHA, FFMPEG_OPTIONS]).encode()
    compiled = cache / ("ffmpeg-tools-" + hashlib.sha256(identity).hexdigest()[:16])
    if compiled.is_dir():
        return compiled
    print("Compiling FFmpeg and ffprobe for Apple silicon", flush=True)
    with tempfile.TemporaryDirectory(dir=cache) as directory:
        work = Path(directory)
        subprocess.run(["/usr/bin/tar", "-xf", archive, "-C", work], check=True)
        subprocess.run(["/usr/bin/tar", "-xf", opus_archive, "-C", work], check=True)
        source = work / "ffmpeg-9.0.2"
        opus = work / "opus-1.5.2"
        prefix = work / "opus-prefix"
        pkg_config = shutil.which("pkg-config")
        if not pkg_config:
            raise ValueError("Install pkg-config to build the bundled audio tools.")
        env = dict(os.environ, PATH="/usr/bin:/bin:/usr/sbin:/sbin")
        with (work / "compile.log").open("w+") as log:
            try:
                jobs = f"-j{min(os.cpu_count() or 2, 10)}"
                subprocess.run(["./configure", f"--prefix={prefix}", "--disable-shared",
                                "--enable-static", "--disable-doc", "--disable-extra-programs"],
                               cwd=opus, env={**env, "CFLAGS": "-O2 -mmacosx-version-min=13.0"},
                               stdout=log, stderr=subprocess.STDOUT, check=True)
                subprocess.run(["/usr/bin/make", jobs, "install"], cwd=opus, env=env,
                               stdout=log, stderr=subprocess.STDOUT, check=True)
                subprocess.run(["./configure", *FFMPEG_OPTIONS, f"--pkg-config={pkg_config}", "--pkg-config-flags=--static"],
                               cwd=source, env={**env, "PKG_CONFIG_PATH": "", "PKG_CONFIG_LIBDIR": str(prefix / "lib/pkgconfig")},
                               stdout=log, stderr=subprocess.STDOUT, check=True)
                subprocess.run(["/usr/bin/make", jobs, "ffmpeg", "ffprobe"],
                               cwd=source, env=env, stdout=log, stderr=subprocess.STDOUT, check=True)
            except subprocess.CalledProcessError:
                log.seek(0)
                print("".join(log.readlines()[-30:]), file=sys.stderr)
                raise
        result = work / "compiled"
        result.mkdir()
        for name in ("ffmpeg", "ffprobe"):
            dependencies = subprocess.check_output(["/usr/bin/otool", "-L", source / name], text=True)
            for line in dependencies.splitlines()[1:]:
                if not line.strip().startswith(("/usr/lib/", "/System/Library/")):
                    raise ValueError(f"{name} requires an external library: {line.strip()}")
            shutil.copy2(source / name, result / name)
        shutil.copy2(source / "COPYING.LGPLv2.1", result / "FFmpeg-LICENSE")
        shutil.copy2(opus / "COPYING", result / "Opus-LICENSE")
        # Parallel builds may both finish. Each candidate was built from the
        # same verified source and options; keep the first completed directory.
        try:
            result.rename(compiled)
        except OSError:
            if not compiled.is_dir():
                raise
    return compiled


def add_runtime(resources):
    cache = Path(os.environ.get("LISTENING_MAC_BUILD_CACHE",
                                Path.home() / "Library/Caches/listening-build/macos"))
    cache.mkdir(parents=True, exist_ok=True)
    python = fetch(cache, "cpython-full.tar.zst", PYTHON_URL, PYTHON_SHA)
    source = fetch(cache, "ffmpeg-9.0.2.tar.xz", FFMPEG_URL, FFMPEG_SHA)
    opus = fetch(cache, "opus-1.5.2.tar.gz", OPUS_URL, OPUS_SHA)
    ffmpeg = media_tools(cache, source, opus)
    version, assets = load_pin(DEFAULT_PIN)
    asset = assets["osx-arm64"]
    sockseek = fetch(cache, asset["file"], asset["url"], asset["sha256"])
    # Install only the relocatable runtime and its upstream notices. Exclude
    # CPython's test suite and static developer libraries from the disk image.
    prefix = resources / "python"
    prefix.mkdir()
    subprocess.run(["/usr/bin/tar", "-xf", python, "-C", prefix, "--strip-components", "2",
                    "--exclude", "python/install/lib/python3.12/test",
                    "--exclude", "*.a", "python/install"], check=True)
    tools = resources / "bin"
    tools.mkdir(exist_ok=True)
    for name in ("ffmpeg", "ffprobe"):
        shutil.copy2(ffmpeg / name, tools / name)
    extract_executable(sockseek, tools / "sockseek")
    notices = resources / "Notices"
    notices.mkdir(exist_ok=True)
    subprocess.run(["/usr/bin/tar", "-xf", python, "-C", notices, "--strip-components", "1",
                    "python/licenses"], check=True)
    with tarfile.open(sockseek, "r:gz") as archive:
        with archive.extractfile("LICENSE") as license_file:
            (notices / "Sockseek-LICENSE").write_bytes(license_file.read())
    shutil.copy2(ffmpeg / "FFmpeg-LICENSE", notices)
    shutil.copy2(source, notices / source.name)
    shutil.copy2(ffmpeg / "Opus-LICENSE", notices)
    shutil.copy2(opus, notices / opus.name)
    shutil.copy2(Path(__file__), notices / "build-audio-tools.py.txt")
    (notices / "Runtime.txt").write_text(
        f"CPython 3.12.16, python-build-standalone\n{PYTHON_URL}\n"
        "Python and runtime dependency notices are in the licenses directory.\n\n"
        f"FFmpeg 9.0.2 (LGPL). Source and configure options accompany this file.\n{FFMPEG_URL}\n\n"
        f"Opus 1.5.2 (BSD)\n{OPUS_URL}\n\n"
        f"Sockseek {version}\n{asset['url']}\n")
