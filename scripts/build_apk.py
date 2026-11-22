"""Build and verify the private Android release. Invoked by listening build apk."""
import argparse
import fcntl
import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile


SOURCE = Path(__file__).resolve().parents[1]
PROJECT = SOURCE / "clients" / "android"
BUILD_TOOLS = "35.0.0"


def required(path, label):
    if not path.is_file():
        raise SystemExit(f"Missing {label} at {path}")
    return path


def build(output):
    user = Path.home()
    cache = Path(os.environ.get("XDG_CACHE_HOME", user / ".cache")) / "listening-build/android"
    if os.uname().sysname == "Darwin":
        cache = user / "Library/Caches/listening-build/android"
    env = dict(os.environ)
    sdk = env.get("ANDROID_HOME") or env.get("ANDROID_SDK_ROOT")
    if not sdk:
        sdk = next((p for p in (user / "Library/Android/sdk", user / "Android/Sdk", cache / "sdk")
                    if p.is_dir()), None)
    if not sdk:
        raise SystemExit("Install Android SDK platform 35 and build-tools 35.0.0, then set ANDROID_HOME.")
    sdk = Path(sdk).expanduser().resolve()
    required(sdk / "platforms/android-35/android.jar", "Android SDK platform 35")
    signer = required(sdk / "build-tools" / BUILD_TOOLS / "apksigner", "Android APK signer")
    env["ANDROID_HOME"] = str(sdk)
    if (cache / "gradle").is_dir():
        env.setdefault("GRADLE_USER_HOME", str(cache / "gradle"))
    if "JAVA_HOME" not in env and os.uname().sysname == "Darwin":
        homebrew_jdk = Path("/opt/homebrew/opt/openjdk@21/libexec/openjdk.jdk/Contents/Home")
        if homebrew_jdk.is_dir():
            env["JAVA_HOME"] = str(homebrew_jdk)
    java_bin = Path(env["JAVA_HOME"]) / "bin" if "JAVA_HOME" in env else None
    if java_bin:
        env["PATH"] = str(java_bin) + os.pathsep + env.get("PATH", "")
    if not shutil.which("javac", path=env.get("PATH")):
        raise SystemExit("Install a JDK 17 or 21 and set JAVA_HOME.")
    keys = user / ".config/listening-release/android"
    keyfile = required(Path(env.get("LISTENING_ANDROID_KEYSTORE", keys / "release.p12")).expanduser(),
                       "release keystore (LISTENING_ANDROID_KEYSTORE)")
    store_password = required(Path(env.get("LISTENING_ANDROID_STORE_PASSWORD_FILE", keys / "password")).expanduser(),
                              "keystore password file (LISTENING_ANDROID_STORE_PASSWORD_FILE)")
    key_password = required(Path(env.get("LISTENING_ANDROID_KEY_PASSWORD_FILE", store_password)).expanduser(),
                            "key password file")
    alias = env.get("LISTENING_ANDROID_KEY_ALIAS", "listening")
    subprocess.run(["keytool", "-list", "-keystore", keyfile, "-alias", alias,
                    "-storepass:file", store_password], env=env, stdout=subprocess.DEVNULL, check=True)
    version = re.search(r"versionName '([\w.-]+)'", (PROJECT / "app/build.gradle").read_text()).group(1)
    name = f"listening-room-{version}.apk"
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    # Gradle writes to a shared output tree. Serialize signing and publication
    # with other builders using the same checkout, including other --output dirs.
    build_dir = PROJECT / ".gradle"
    build_dir.mkdir(exist_ok=True)
    with (build_dir / "listening-release.lock").open("a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        with tempfile.TemporaryDirectory(prefix=".listening-", dir=output) as stage:
            staging = Path(stage)
            subprocess.run([PROJECT / "gradlew", "--no-daemon", ":app:assembleRelease"],
                           cwd=PROJECT, env=env, check=True)
            unsigned = required(PROJECT / "app/build/outputs/apk/release/app-release-unsigned.apk",
                                "unsigned release from Gradle")
            apk = staging / name
            key_options = [] if key_password.samefile(store_password) else ["--key-pass", "file:" + str(key_password)]
            subprocess.run([signer, "sign", "--ks", keyfile, "--ks-key-alias", alias,
                            "--ks-pass", "file:" + str(store_password),
                            *key_options,
                            "--out", apk, unsigned], env=env, check=True)
            subprocess.run([signer, "verify", "--verbose", apk], env=env, check=True)
            checksum = staging / (name + ".sha256")
            checksum.write_text(hashlib.sha256(apk.read_bytes()).hexdigest() + "  " + name + "\n")
            os.replace(apk, output / name)
            os.replace(checksum, output / checksum.name)
    print(output / name)
    print(output / (name + ".sha256"))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    build(args.output)
