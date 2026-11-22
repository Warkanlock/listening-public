"""The public terminal entry point for a Listening Room installation."""

from contextlib import closing
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile

from system import cli_fediverse, cli_room, onboarding
from system.client_artifacts import release_version
from system.config import default_config_path, default_root, legacy_config_path, resolve_config_path
from system.private_files import private_write
from system.server_control import control_path, lock_file, request

ROOT = Path(__file__).resolve().parents[2]
TIMEOUT = 45


def version():
    return release_version(ROOT)


def address(host, port):
    return f"http://{'127.0.0.1' if host == '0.0.0.0' else host}:{port}/"


def ready(url):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url + "api/status", timeout=1) as response:
            return (response.headers.get("Server", "").startswith("ListeningRoom/")
                    and isinstance(json.load(response), dict))
    except urllib.error.HTTPError as error:
        return error.code == 401 and error.headers.get("Server", "").startswith("ListeningRoom/")
    except (OSError, ValueError):
        return False


def wait_for(check, failure):
    deadline = time.monotonic() + TIMEOUT
    while time.monotonic() < deadline:
        result = check()
        if result:
            return result
        time.sleep(0.15)
    raise RuntimeError(failure)


def base_command(host, port):
    return [sys.executable, str(ROOT / "backend/server.py"), "--host", host, "--port", str(port)]


def require_shell():
    if not (ROOT / "frontend/dist/index.html").is_file():
        raise RuntimeError("The web app is missing. Run the source installer or reinstall the package.")


def local_server(args, config):
    path = control_path(config)
    with lock_file(path.with_suffix(".command")):
        info = request(path, "status")
        if args.action == "status":
            if not info:
                print("Stopped")
                return 1
            url = address(info["host"], info["port"])
            healthy = ready(url)
            print(("Running at " if healthy else "Unhealthy at ") + url)
            return 0 if healthy else 1
        if args.action in {"restart", "stop"}:
            def stopped():
                try:
                    with lock_file(path.with_suffix(".owner"), wait=False):
                        return True
                except BlockingIOError:
                    # A starting listener may not have a socket yet. A stopping
                    # listener still owns its room until final cleanup completes.
                    request(path, "stop")
                    return False
            wait_for(stopped, "Server did not finish stopping")
        if args.action == "stop":
            print("Stopped")
            return 0
        if args.action == "start" and info:
            url = address(info["host"], info["port"])
            if not ready(url):
                raise RuntimeError(f"The existing server is unhealthy at {url}")
            print(f"Already running at {url}")
            return 0
        require_shell()
        host = args.host or (info and info["host"]) or "0.0.0.0"
        port = args.port if args.port is not None else (info or {}).get("port", 8765)
        config.parent.mkdir(parents=True, exist_ok=True)
        log = config.parent / "server.log"
        with os.fdopen(os.open(log, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600), "ab") as output:
            proc = subprocess.Popen(base_command(host, port), start_new_session=True,
                                    stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT)

        def started():
            if proc.poll() is not None:
                raise RuntimeError(f"Server exited during startup. See {log}")
            record = request(path, "status")
            if record and record["pid"] == proc.pid:
                url = address(record["host"], record["port"])
                return url if ready(url) else None
        try:
            url = wait_for(started, f"Server startup timed out. See {log}")
        except BaseException:
            if proc.poll() is None:
                proc.terminate()
            proc.wait(timeout=10)
            raise
        print(f"Running at {url}")
        return 0


def manager_config(config):
    return config.with_name("server-manager.json")


def service_commands(settings):
    if settings["kind"] == "compose":
        command = ["docker", "compose", "-f", settings["file"], "-p", settings["project"]]
        status_cmd = [*command, "ps", "--status", "running", "-q", "listening-room"]
        running = lambda: bool(subprocess.check_output(status_cmd, text=True).strip())
    elif settings["kind"] == "systemd":
        command = ["systemctl", "--user"]
        status_cmd = [*command, "is-active", "--quiet", "listening-room.service"]
        running = lambda: subprocess.run(status_cmd).returncode == 0
    else:
        raise ValueError("Unknown server manager")
    return command, running


def managed_server(args, settings):
    if args.host is not None or args.port is not None:
        raise ValueError("Configure the bind address and port in the installed service")
    action = args.action
    compose = settings["kind"] == "compose"
    command, running = service_commands(settings)
    url = address("127.0.0.1", settings["port"])
    if action == "status":
        if not running():
            print("Stopped")
            return 1
        if not ready(url):
            raise RuntimeError(f"Server unhealthy at {url}")
    else:
        if compose:
            operation = {"start": ["up", "-d"], "restart": ["up", "-d", "--force-recreate"], "stop": ["stop"]}[action]
            subprocess.run([*command, *operation, "listening-room"], check=True)
        else:
            subprocess.run([*command, action, "listening-room.service"], check=True)
        if action == "stop":
            print("Stopped")
            return 0
        wait_for(lambda: running() and ready(url), f"Service did not answer at {url}")
    print(f"Running at {url}")
    return 0


def build(args):
    checkout = args.source.resolve() if args.source else Path.cwd().resolve()
    if not args.source:
        checkout = next((p for p in (checkout, *checkout.parents) if (p / "listening").is_file()
                         and (p / "scripts").is_dir()), ROOT)
    target = "ipad" if args.target == "ios" else args.target
    script = checkout / "scripts" / f"build_{target}.py"
    if not script.is_file():
        raise ValueError("Choose a Listening Room source checkout with --source DIR")
    out = (args.output or checkout / "dist/clients").resolve()
    command = [sys.executable, str(script), "--output", str(out)]
    return subprocess.run(command, cwd=checkout).returncode


def room_config(base, room):
    identity = hashlib.sha256(os.fsencode(room.resolve())).hexdigest()
    return base.parent / "rooms" / identity / "config.json"


def selected_config(base):
    selector = base.with_suffix(base.suffix + ".room")
    if selector.is_file():
        return Path(selector.read_text(encoding="utf-8"))
    if "LISTENING_ROOM_CONFIG" in os.environ or base != default_config_path().resolve() or base.exists():
        return base
    legacy = legacy_config_path(base)
    if legacy and legacy.exists():
        return base
    # Existing installations may have credentials or a catalog before a
    # config is saved. Keep those hosts on their original configuration path.
    if base.parent.exists() and any(p.name not in {"rooms", "server-manager.json", ".DS_Store"}
                                    for p in base.parent.iterdir()):
        return base
    return room_config(base, default_root())


def select_room(base, config, room, *, create):
    from catalog.collection_snapshot import collection_access, create_empty_collection, verify_collection
    room = room.resolve()
    if room.name == "collection.sqlite3" and room.is_file():
        room = room.parent
    base.parent.mkdir(parents=True, exist_ok=True)
    config.parent.mkdir(parents=True, exist_ok=True)
    with collection_access(config.parent, exclusive=True):
        if create:
            create_empty_collection(room)
        else:
            verify_collection(room)
        legacy = legacy_config_path(base)
        first_default = (config == base and not base.exists() and not (legacy and legacy.exists())
                         and room == default_root().resolve()
                         and not any(base.parent.glob("*.sqlite3")))
        chosen = base if first_default else room_config(base, room)
        if not chosen.exists():
            data = {"folder": str(room), "download_engines": {}, "radio_providers": []}
            private_write(chosen, json.dumps(data), exclusive=True)
        if chosen != base:
            private_write(base.with_suffix(base.suffix + ".room"), str(chosen))
    print(f"Selected room at {room}")


def install_command():
    target = Path.home() / ".local/bin/listening"
    target.parent.mkdir(parents=True, exist_ok=True)
    launcher = ROOT.parent / "bin/listening" if os.environ.get("LISTENING_ROOM_INSTALL_KIND") == "app" else ROOT / "listening"
    if target.is_symlink() and target.resolve() == launcher:
        pass
    elif target.exists() or target.is_symlink():
        raise ValueError(f"{target} already points to another installation")
    else:
        target.symlink_to(launcher)
    print(f"Installed {target}")
    if shutil.which("listening") is None:
        print('Add the command to your shell with: export PATH="$HOME/.local/bin:$PATH"')


def manager_options(parser):
    parser.add_argument("kind", choices=("local", "systemd", "compose"))
    parser.add_argument("--file", type=Path, help="Compose file for the installed service")
    parser.add_argument("--project", help="Compose project name")
    parser.add_argument("--port", type=int, default=8765, help="published service port (default 8765)")


class RoomParser(argparse.ArgumentParser):
    def format_help(self):
        return f"{cli_room.heading()}   v{version()}\n\n" + super().format_help()


def main(argv=None):
    cli = RoomParser(prog="listening", usage="listening COMMAND [OPTIONS]",
                     description="Host a music library and connect your devices.",
                     formatter_class=argparse.RawDescriptionHelpFormatter,
                     epilog="First time?  listening setup\n"
                            "Room online? listening status\n"
                            "New device?  listening invite\n\n"
                            "Use listening COMMAND --help for options and arguments.")
    cli.set_defaults(json=False)
    cli.add_argument("--version", action="version", version=version())
    groups = cli.add_subparsers(dest="group", title="commands", metavar="COMMAND",
                                parser_class=argparse.ArgumentParser)
    cli_room.add_commands(groups)
    cli_fediverse.add_commands(groups)
    config_parser = groups.add_parser("config", help="inspect installation paths or select the server manager")
    cli_room.json_option(config_parser)
    configurations = config_parser.add_subparsers(dest="action")
    for name, description in (("show", "inspect the selected installation"), ("path", "print the launcher’s selected config path")):
        cli_room.json_option(configurations.add_parser(name, help=description))
    manager_options(configurations.add_parser("manager", help="choose local, systemd or Compose hosting"))
    setup = groups.add_parser("setup", help="walk through server and client setup",
                              description="Choose a local room, prepare the browser player, or build Android, Mac or iOS clients.")
    onboarding.add_options(setup)
    groups.add_parser("install-command", help="add the source launcher to your PATH")
    server = groups.add_parser("server", help="manage the room").add_subparsers(dest="action", required=True)
    for name in ("start", "stop", "restart", "status"):
        command = server.add_parser(name)
        command.set_defaults(host=None, port=None, foreground=False)
        if name in ("start", "restart"):
            command.add_argument("--host")
            command.add_argument("--port", type=int)
        if name == "start":
            command.add_argument("--foreground", action="store_true")
    manager = server.add_parser("configure", help="select the local, systemd or Compose installation")
    manager_options(manager)
    clients = groups.add_parser("build", help="build an Android, Mac or iOS client")
    clients.add_argument("target", choices=onboarding.NATIVE_CLIENTS)
    clients.add_argument("--source", type=Path)
    clients.add_argument("--output", type=Path)
    music = groups.add_parser("music", help="prepare a demo or import playlist from owned audio")
    music_actions = music.add_subparsers(dest="action", required=True)
    music_actions.add_parser("demo", help="generate a playable offline demo").add_argument("folder", type=Path)
    index = music_actions.add_parser("index", help="index a folder into a portable CSV playlist")
    index.add_argument("folder", type=Path)
    index.add_argument("playlist", type=Path)
    spotify = groups.add_parser("spotify", help="prepare your Spotify export for local import")
    convert = spotify.add_subparsers(dest="action", required=True).add_parser(
        "convert", help="convert a Spotify personal-data export to a portable playlist bundle")
    convert.add_argument("source", type=Path, metavar="SPOTIFY_EXPORT")
    convert.add_argument("destination", type=Path, metavar="NEW_BUNDLE.zip")
    collection = groups.add_parser("collection", help="copy or restore a portable collection")
    actions = collection.add_subparsers(dest="action", required=True)
    for action in ("create", "open"):
        actions.add_parser(action).add_argument("room", type=Path)
    for action in ("preview", "snapshot"):
        sub = actions.add_parser(action)
        sub.add_argument("destination", type=Path)
        if action == "preview":
            sub.add_argument("--check-audio", action="store_true", help="probe and hash source files and list individual track issues")
    restore = actions.add_parser("restore")
    restore.add_argument("snapshot", type=Path)
    restore.add_argument("room", type=Path)
    args = cli.parse_args(argv)
    if not args.group:
        cli.print_help()
        return 0
    if args.group == "join":
        return cli_room.join(args.link)
    if args.group == "fediverse":
        return cli_fediverse.run(args)
    if args.group == "build":
        return build(args)
    if args.group == "spotify":
        from adapters.spotify_export import MAX_EXPORT_BYTES, parse_export
        if args.destination.suffix.lower() != ".zip" or not args.source.is_file():
            raise ValueError("Choose a Spotify export file and a new .zip destination.")
        with args.source.open("rb") as source:
            data = source.read(MAX_EXPORT_BYTES + 1)
        converted = parse_export(args.source.name, data)
        if not converted.lists:
            raise ValueError("The export did not contain any supported playlists or saved tracks.")
        lists = [(name, content.encode("utf-8")) for name, content in converted.lists]
        total = sum(len(content) for _, content in lists)
        if (len(lists) > 500 or total > 30 * 1024 * 1024
                or any(len(content) > 25 * 1024 * 1024 for _, content in lists)):
            raise ValueError("This export is too large for one import. Convert individual Spotify JSON files.")
        from io import BytesIO
        bundle = BytesIO()
        with zipfile.ZipFile(bundle, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for filename, content in lists:
                archive.writestr("lists/" + filename, content)
        with args.destination.open("xb") as output:
            output.write(bundle.getbuffer())
        print(f"Converted {len(converted.lists)} playlists with {converted.tracks} entries. "
              f"Skipped {converted.skipped}. Import the bundle in Settings → Sources.")
        return 0
    if args.group == "music":
        from library import music_intake
        if args.action == "demo":
            music_intake.create_demo(args.folder)
            print(f"Created demo music in {cli_room.display(args.folder)}.")
        else:
            count, skipped = music_intake.write_index(args.folder, args.playlist)
            print(f"Indexed {count} tracks and skipped {skipped}. Import {cli_room.display(args.playlist)} in Sources.")
        return 0
    if args.group == "setup":
        def choose(room, create):
            return main(["collection", "create" if create else "open", str(room)])

        return onboarding.run(args, root=ROOT, selected_config=selected_config, select_room=choose,
                              install_command=install_command, start_server=lambda port, host: main(
                                  ["server", "start"] + (["--port", str(port)] if port is not None else [])
                                  + (["--host", host] if host else [])),
                              join=lambda: cli_room.join(None),
                              build=lambda target, source, output: build(argparse.Namespace(
                                  target=target, source=source, output=output)))
    if args.group == "install-command":
        install_command()
        return 0
    base = resolve_config_path().expanduser().resolve()
    if args.group == "server" and getattr(args, "foreground", False):
        os.environ["LISTENING_ROOM_CONFIG"] = str(selected_config(base))
        require_shell()
        command = base_command(args.host or "0.0.0.0", 8765 if args.port is None else args.port)
        os.execv(command[0], command)
    if args.group == "doctor" or (args.group == "collection" and args.action == "preview"):
        return dispatch_room(args, base, selected_config(base))
    with lock_file(control_path(base).with_suffix(".installation")):
        return dispatch_room(args, base, selected_config(base))


def dispatch_room(args, base, config):
    if args.group in {"status", "doctor", "account", "export", "invite", "pair", "pairing", "settings", "config", "extensions"}:
        file = manager_config(base)
        manager = json.loads(file.read_text()) if file.is_file() else {"kind": "local"}
        if args.group == "account":
            return cli_room.account_command(args, manager, config)
        if args.group != "config":
            if os.environ.get("LISTENING_ROOM_LOCAL_CONTROL") == "1":
                manager = {"kind": "local"}
            return cli_room.live_command(args, manager, lambda: request(control_path(config), "status"),
                                         service_commands, ready, config=config)
        if args.action != "manager":
            if args.action == "path":
                cli_room.emit({"config_path": str(config)}, as_json=args.json, single="config_path")
            else:
                data = {"manager": manager["kind"], "config_path": str(config), "manager_path": str(file)}
                if manager["kind"] != "local":
                    data["service_port"] = manager["port"]
                if manager["kind"] == "compose":
                    data.update(compose_file=manager["file"], project=manager["project"])
                cli_room.emit(data, as_json=args.json)
            return 0
    if args.group == "collection":
        from catalog.collection_snapshot import create_snapshot, restore_snapshot
        if args.action in ("create", "open"):
            select_room(base, config, args.room, create=args.action == "create")
        elif args.action == "preview":
            from catalog.collection_preview import preview
            facts = preview(config, args.destination, check_audio=args.check_audio)
            print(json.dumps(facts, indent=2))
            if facts["snapshot_blockers"]:
                raise ValueError("Resolve the issues listed under snapshot_blockers before copying this room")
            if facts["available_bytes"] < facts["required_bytes_including_reserve"]:
                raise ValueError("Choose a destination with more space")
        elif args.action == "snapshot":
            destination = create_snapshot(config, args.destination.resolve())
            print(destination)
            with closing(sqlite3.connect(destination.as_uri() + "?mode=ro", uri=True)) as db:
                for name, reason in db.execute("SELECT name,reason FROM collection_inventory WHERE reason != ''"):
                    print(f"Skipped {name}: {reason}")
        else:
            restore_snapshot(args.snapshot.resolve(), args.room.resolve(), unpack_lists=False)
            print(args.room.resolve())
        return 0
    if args.port is not None and not 0 <= args.port <= 65535:
        raise ValueError("Port must be in the range 0–65535")
    if args.action in ("configure", "manager"):
        data = {"kind": args.kind, "port": args.port}
        if args.kind == "compose":
            if not args.file or not args.file.is_file() or not args.project:
                raise ValueError("Compose requires --file PATH and --project NAME")
            data.update(file=str(args.file.resolve()), project=args.project)
        manager = manager_config(base)
        previous = json.loads(manager.read_text()) if manager.exists() else {"kind": "local"}
        if previous != data:
            if previous["kind"] == "local":
                running = request(control_path(config), "status") is not None
            else:
                _, is_running = service_commands(previous)
                running = is_running()
            if running:
                raise ValueError("Stop the selected server before changing its manager")
            private_write(manager, json.dumps(data))
        print(f"Selected {args.kind} server")
        return 0
    os.environ["LISTENING_ROOM_CONFIG"] = str(config)
    file = manager_config(base)
    if file.exists():
        settings = json.loads(file.read_text())
        if settings["kind"] != "local":
            return managed_server(args, settings)
    return local_server(args, config)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, RuntimeError, sqlite3.Error, subprocess.SubprocessError) as error:
        sys.exit(f"listening: {error}")
