"""Terminal walkthrough and its equivalent noninteractive setup options."""
from getpass import getpass
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from urllib.parse import urlencode

from system.config import AppConfig, default_root, legacy_config_path, resolve_config_path
from system.hosts import configured_origin
from system.owner_auth import OwnerAuth
from system.server_control import control_path, lock_file
from system.updater import install_kind


NATIVE_CLIENTS = ("apk", "dmg", "ios", "ipad")


def add_options(command):
    command.add_argument("--client", choices=("web", *NATIVE_CLIENTS), action="append",
                         help="prepare the server with its web client, or build a native client (repeatable)")
    command.add_argument("--music", type=Path, help="index a read-only folder of tagged audio")
    command.add_argument("--origin", help="full server address that clients use, independent of the bind address")
    command.add_argument("--bind", help="listener address with --start (default localhost for guided setup)")
    command.add_argument("--owner", help="create the first owner using a hidden password prompt")
    command.add_argument("--room", type=Path, help="create an empty room or open a portable collection")
    command.add_argument("--start", action="store_true", help="start the selected server and print its address")
    command.add_argument("--port", type=int, help="local server port with --start (default 8765)")
    command.add_argument("--downloads", action="store_true", help="provision the verified download tool")
    command.add_argument("--systemd", action="store_true", help="install and start the Linux user service")
    command.add_argument("--source", type=Path, help="source checkout for native builds")
    command.add_argument("--output", type=Path, help="directory for native clients and checksums")
    command.add_argument("--non-interactive", action="store_true", help="use options without prompting")


def _ask(prompt, default):
    try:
        return input(f"{prompt} [{default}] ").strip() or default
    except (EOFError, KeyboardInterrupt):
        raise ValueError("Setup cancelled") from None


def _walk(args, configured_room, config):
    print("Listening Room setup\n")
    print("The server keeps your music and plays it on your devices.")
    print("  host   Create a room or resume setup")
    print("  join   Open an invitation to an existing room")
    while True:
        choice = _ask("What would you like to do?", "host").lower()
        if choice in {"host", "web", "join"}:
            break
        print("Choose host or join. To build a client, use the separate listening build command.")
    if choice == "join":
        return False
    args.client = ["web"]
    if configured_room:
        print(f"Resuming the room at {configured_room}.")
    if install_kind() != "docker":
        selected = Path(_ask("Room storage folder", str(configured_room or default_root()))).expanduser().resolve()
        if selected != configured_room:
            args.room = selected
    if not OwnerAuth(config.parent).enabled or (args.room and args.room != configured_room):
        args.owner = _ask("First owner username (or skip)", "owner")
        if args.owner == "skip":
            args.owner = None
    resume = args.room is None or args.room == configured_room
    saved = json.loads(config.read_text()) if resume and config.is_file() else {}
    if os.environ.get("LISTENING_ROOM_ORIGIN"):
        print("The server address is set by LISTENING_ROOM_ORIGIN.")
    else:
        if saved.get("advertised_origin"):
            print(f"Other devices use {configured_origin(saved['advertised_origin'])}.")
        raw = _ask("Server URL other devices will use (or skip)", "skip")
        if raw != "skip":
            args.origin = raw
    mounted = os.environ.get("LISTENING_ROOM_MUSIC_INBOX")
    if saved.get("music_inbox"):
        print("The existing music folder is configured. Skip to keep it.")
    folder = _ask("Read-only music folder (or skip)", "skip" if saved.get("music_inbox") else mounted or "skip")
    if folder != "skip":
        args.music = Path(folder)
    if install_kind() != "docker":
        args.start = _ask("Start the server after setup? (yes/no)", "yes").lower() in {"yes", "y"}
        if args.start:
            args.bind = _ask("Bind address", "127.0.0.1")
    return True


def _update_room(args, config, *, interactive, origin, root):
    from library import music_intake

    scan = None
    folder = None
    if args.music:
        folder = args.music.expanduser().resolve(strict=True)
        mounted = os.environ.get("LISTENING_ROOM_MUSIC_INBOX")
        if mounted and folder != Path(mounted).resolve():
            raise ValueError("Use the music folder mounted by the server installation.")
        if config.parent.is_relative_to(folder):
            raise ValueError("Keep private Listening configuration outside the source music folder.")
        print("Scanning the tagged audio in your music folder …", flush=True)
        scan = music_intake.scan_folder(folder)
        print(f"Found {len(scan.tracks)} distinct tagged tracks. Allow up to {scan.size / 2**20:,.1f} MiB for audio.")
        for label, count in sorted(scan.skipped.items()):
            print(f"  {label}: {count}")
        if interactive and scan.tracks and _ask("Import these tracks on server startup? (yes/no)", "yes").lower() not in {"y", "yes"}:
            scan = None
        if scan and not scan.tracks:
            print("Choose another folder, or continue and add music later.")
            scan = None
    if args.origin is not None or scan:
        settings = AppConfig(config, root)
        data = settings.load()
        if args.origin is not None:
            data["advertised_origin"] = origin
        if scan:
            room = Path(data.get("folder") or default_root()).expanduser().resolve()
            if room.is_relative_to(folder) or folder.is_relative_to(room):
                raise ValueError("Choose a music source separate from the room's writable storage.")
            source = config.parent / "imports" / "listening-owned-music.xspf"
            music_intake.save_folder_index(source, scan)
            data["source_files"] = list(dict.fromkeys([*data.get("source_files", []), str(source)]))
            data["music_inbox"] = str(folder)
        settings.save()
    if args.owner:
        auth = OwnerAuth(config.parent)
        if auth.enabled:
            print("This room already has an owner. Use listening account set-owner to change its login.")
            return scan
        password = getpass("Owner password: ")
        if password != getpass("Repeat owner password: "):
            raise ValueError("The passwords differ. Rerun setup to finish.")
        auth.set_password(args.owner, password)
        print("Owner login saved.")
    return scan


def enable_legacy_downloads(root, base, config):
    with lock_file(control_path(base).with_suffix(".installation")):
        try:
            with lock_file(control_path(config).with_suffix(".owner"), wait=False):
                subprocess.run([sys.executable, str(root / "scripts/provision_sockseek.py")], cwd=root,
                               check=True, env={**os.environ, "LISTENING_ROOM_CONFIG": str(config)})
                app = AppConfig(config, root)
                data = app.load()
                fallback = root / "bin/sockseek"
                installed = config.parent / "bin/sockseek"
                if data["sockseek_binary"] == str(fallback) and not fallback.is_file() and installed.is_file():
                    data["sockseek_binary"] = str(installed)
                if data.get("download_engines") == {}:
                    data.pop("download_engines")
                app.save()
        except BlockingIOError:
            raise ValueError("Stop this room before switching to the legacy download tool.") from None


def run(args, *, root, selected_config, select_room, install_command, build, start_server, join):
    base = resolve_config_path().expanduser().resolve()
    config = selected_config(base)
    existing = config if config.is_file() else legacy_config_path(config)
    current = None
    needs_current_room = (not args.client or "web" in args.client or args.room or args.start
                          or args.downloads or args.systemd or args.music or args.origin is not None or args.owner)
    if needs_current_room and existing and existing.is_file():
        current = Path(json.loads(existing.read_text()).get("folder") or default_root()).expanduser().resolve()
    elif needs_current_room and config == base and any(base.parent.glob("*.sqlite3")):
        # A preconfigured host can already own a state database before its
        # settings are first saved. The normal boot path adopts that room.
        current = default_root().resolve()
    supplied = any((args.client, args.room, args.start, args.port is not None, args.downloads, args.systemd,
                    args.source, args.output, args.music, args.origin is not None, args.owner, args.bind))
    interactive = sys.stdin.isatty() and not args.non_interactive and not supplied
    if interactive and not _walk(args, current, existing if existing and existing.is_file() else config):
        return join()
    origin = configured_origin(args.origin) if args.origin is not None else ""
    if args.owner and not sys.stdin.isatty():
        raise ValueError("Create the room owner from a terminal so the password stays hidden.")
    if args.origin is not None and os.environ.get("LISTENING_ROOM_ORIGIN"):
        raise ValueError("Change LISTENING_ROOM_ORIGIN in the server installation to update its advertised URL.")
    if args.bind and (not args.start or args.systemd):
        raise ValueError("Use --bind with --start for a local server.")
    targets = ["ios" if target == "ipad" else target for target in (args.client or ["web"])]
    if args.port is not None and (not args.start or args.systemd or not 0 <= args.port <= 65535):
        raise ValueError("Use --port 0–65535 with --start for a local server")
    if (args.output or args.source) and not any(t != "web" for t in targets):
        raise ValueError("Choose a native --client to specify a client source or output folder")
    host = ("web" in targets or args.room or args.start or args.downloads or args.systemd
            or args.music or args.origin is not None or args.owner)
    kind = install_kind()
    packaged = kind != "checkout"
    room = args.room.expanduser().resolve() if args.room else None
    if host and room is None and current is None:
        default = default_root().resolve()
        if (not default.exists() or default.is_dir() and not any(default.iterdir())) and not any(base.parent.glob("*.sqlite3")):
            room = default
    if room and room.is_file() and room.name == "collection.sqlite3":
        room = room.parent
    if room and room != current and room.exists() and not (room / "collection.sqlite3").is_file():
        if not room.is_dir() or any(room.iterdir()):
            raise ValueError("Choose an empty folder or a room containing collection.sqlite3. For a legacy library, use collection preview first.")
    if args.systemd and (packaged or not sys.platform.startswith("linux")):
        raise ValueError("The systemd installer requires a Linux source checkout")

    if host:
        if not packaged and not args.systemd:
            frontend = root / "frontend/react"
            if not shutil.which("yarn"):
                raise ValueError("Install Yarn Classic and Node 22.12 or newer to build the web client")
            print("Building the web client for the local server …", flush=True)
            subprocess.run([sys.executable, str(root / "scripts/preflight.py")], check=True)
            subprocess.run(["yarn", "install", "--frozen-lockfile"], cwd=frontend, check=True)
            subprocess.run(["yarn", "build"], cwd=frontend, check=True)
        if not args.systemd and kind in ("app", "checkout"):
            install_command()
        if room and room != current:
            select_room(room, not (room / "collection.sqlite3").is_file())
            current = room
        config = selected_config(base)
        scanned = None
        if args.music or args.origin is not None or args.owner:
            with lock_file(control_path(base).with_suffix(".installation")):
                if args.music or args.origin is not None:
                    try:
                        with lock_file(control_path(config).with_suffix(".owner"), wait=False):
                            scanned = _update_room(args, config, interactive=interactive, origin=origin, root=root)
                    except BlockingIOError:
                        raise ValueError("Stop this room before changing its source folder or advertised URL.") from None
                else:
                    _update_room(args, config, interactive=interactive, origin=origin, root=root)
        if args.downloads and not packaged:
            enable_legacy_downloads(root, base, config)
        if args.systemd:
            # The installer works in the checkout. Keep a caller-relative host
            # config tied to the same room it selected before that chdir.
            environment = os.environ.copy()
            if "LISTENING_ROOM_CONFIG" in environment:
                environment["LISTENING_ROOM_CONFIG"] = str(base)
            subprocess.run([str(root / "scripts/server_setup.sh")], cwd=root, check=True, env=environment)
        elif args.start:
            if start_server(args.port, args.bind):
                raise RuntimeError("The server could not start")
        if kind == "docker":
            print("Start the room with docker compose up -d --no-build on the host.")
        elif args.start or args.systemd:
            print("Run listening status to check your room.")
        else:
            print("Start the server with: listening server start")
        saved = json.loads(config.read_text()) if config.is_file() else {}
        advertised = configured_origin(os.environ.get("LISTENING_ROOM_ORIGIN") or saved.get("advertised_origin", ""))
        local_port = args.port or 8765
        if args.port == 0 and args.start:
            from system.server_control import request
            local = request(control_path(config), "status")
            if local:
                local_port = local["port"]
        address = advertised or ("" if kind == "docker" else f"http://127.0.0.1:{local_port}")
        if address:
            if scanned:
                first = scanned.tracks[0]
                print(f"Play your first import: {address}/?{urlencode({'q': first['album'] or first['title']})}")
            print(f"Connect a device: {address}/?setup=clients")
        else:
            print("Find the published port on the Compose host with docker compose port listening-room 8765.")
            print("Open the host’s reachable URL with that port, then choose Settings → Connect a device.")
        print("Open Settings → Room setup for optional extensions. Use listening invite to pair a device.")
        if current:
            print(f"Room: {current}")
    for target in dict.fromkeys(targets):
        if target != "web":
            print(f"Building the {target.upper()} client …", flush=True)
            code = build(target, args.source, args.output)
            if code:
                return code
            if target == "ios" and not os.environ.get("LISTENING_APPLE_TEAM", "").strip():
                continue
            if target == "apk":
                print("Install Listening Room, then pair with an invitation or open Device files to play saved music.")
                continue
            print("Install the client, open Connect to a room, and paste the link from listening invite on the server.")
    return 0
