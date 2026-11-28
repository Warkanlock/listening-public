"""Terminal access to the running room's existing host API."""

import argparse
from functools import partial
import http.client
import ipaddress
import json
import os
import subprocess
import sys
from urllib.parse import parse_qs, urlsplit
import webbrowser
from getpass import getpass

from system.doctor import inspect_room
from system.hosts import configured_origin
from system.pairing import invitation_authority, is_tailnet_host
from system.tailscale import local_identity

WRITABLE = ("audio-store", "auto-download", "share-folder")
READABLE = (*WRITABLE, "folder", "lists-folder", "music-folder", "playlist-output", "download-ready")
ENGINE_ACTIONS = ("install", "test", "enable", "disable", "update", "rollback", "remove")


def heading():
    mark = "  ▂ ▆ █ ▅ ▂   LISTENING"
    try:
        mark.encode(sys.stdout.encoding or "ascii")
    except UnicodeEncodeError:
        mark = "  .:|:.   LISTENING"
    if sys.stdout.isatty() and "NO_COLOR" not in os.environ and os.environ.get("TERM") != "dumb":
        return "\033[1;36m" + mark + "\033[0m"
    return mark


def emit(data, *, as_json, single=None):
    if as_json:
        print(json.dumps(data, indent=2))
    elif single:
        print(display(data[single]))
    else:
        for key, value in data.items():
            print(f"{key.replace('_', ' ').capitalize():<17} {display(value)}")


def display(value):
    if isinstance(value, bool):
        return "on" if value else "off"
    # Device names and file paths may originate outside the terminal.
    return "".join(c if c.isprintable() else ascii(c)[1:-1] for c in str(value))


def json_option(parser):
    parser.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="write machine-readable JSON")


def add_commands(groups):
    status = groups.add_parser("status", help="show server health, library counts and devices")
    json_option(status)
    doctor = groups.add_parser("doctor", help="check storage, network configuration, and room health")
    doctor.add_argument("--check-database", action="store_true", help="scan the portable collection for SQLite errors")
    json_option(doctor)
    account = groups.add_parser("account", help="manage access to the room from its host")
    accounts = account.add_subparsers(dest="action", required=True)
    accounts.add_parser("set-owner", help="set or reset the owner login").add_argument("username")
    accounts.add_parser("add-member", help="create a separate listening profile").add_argument("username")
    accounts.add_parser("add-stream-key", help="create a read-only OpenSubsonic key").add_argument("name")
    json_option(accounts.add_parser("stream-keys", help="list OpenSubsonic keys by name and ID"))
    accounts.add_parser("revoke-stream-key", help="revoke an OpenSubsonic key").add_argument("key_id")
    json_option(accounts.add_parser("members", help="list household logins"))
    for action, help_text in (
        ("reset-member", "set a new password and revoke the member's sessions"),
        ("enable-member", "allow a member to sign in again"),
        ("disable-member", "disallow sign-in and revoke the member's sessions"),
        ("revoke-member", "sign a member out of all browsers"),
    ):
        accounts.add_parser(action, help=help_text).add_argument("member_id", metavar="MEMBER_ID")
    export = groups.add_parser("export", help="export original or compact audio")
    actions = export.add_subparsers(dest="action", required=True)
    start = actions.add_parser("start", help="export the library or one playlist")
    start.add_argument("--playlist", help="export a catalog playlist by its key")
    start.add_argument("--format", choices=("original", "opus", "aac"), default="original",
                       help="export original bytes, Compact Opus, or Compact AAC")
    json_option(start)
    json_option(actions.add_parser("status", help="show the latest audio export progress"))
    invite = groups.add_parser("invite", help="print a private link to join this room")
    invite.add_argument("--tailscale", action="store_true", help="require a Tailscale invitation")
    json_option(invite)
    join = groups.add_parser("join", help="open an invitation in your browser")
    join.add_argument("link", nargs="?", help="pairing URL (omit to paste privately)")
    pair = groups.add_parser("pair", aliases=["pairing"], help="list paired devices or manage their access")
    actions = pair.add_subparsers(dest="action", required=True)
    for name, description in (("list", "list devices and their IDs"), ("invite", "print the current invitation"),
                              ("rotate", "replace the invitation and keep paired devices"),
                              ("revoke", "remove access for one paired device"),
                              ("allow-invites", "let a paired browser issue invitations"),
                              ("deny-invites", "withdraw permission to issue invitations")):
        sub = actions.add_parser(name, help=description, description=description)
        json_option(sub)
        if name == "invite":
            sub.add_argument("--tailscale", action="store_true", help="require a Tailscale invitation")
        if name in ("revoke", "allow-invites", "deny-invites"):
            sub.add_argument("device_id", metavar="DEVICE_ID")
    settings = groups.add_parser("settings", help="read or change settings on the running server")
    json_option(settings)
    actions = settings.add_subparsers(dest="action")
    for name, description in (("show", "show current settings"), ("get", "read one setting"),
                              ("set", "change one setting")):
        sub = actions.add_parser(name, help=description)
        json_option(sub)
        if name in ("get", "set"):
            sub.add_argument("key", choices=WRITABLE if name == "set" else READABLE)
        if name == "set":
            sub.add_argument("value", help="on/off, database/files, or a folder on the server")

    extensions = groups.add_parser("extensions", help="manage the room's optional sources and download engines")
    json_option(extensions)
    actions = extensions.add_subparsers(dest="action")
    json_option(actions.add_parser("list", help="show installed and available extensions"))
    json_option(actions.add_parser("health", help="check the selected download sources"))
    radio = actions.add_parser("radio", help="enable or disable an external Radio provider")
    json_option(radio)
    radio.add_argument("provider", choices=("youtube", "somafm"))
    radio.add_argument("state", choices=("on", "off"))
    for action in ENGINE_ACTIONS:
        engine = actions.add_parser(action, help=f"{action.capitalize()} a download engine")
        json_option(engine)
        engine.add_argument("engine")
    select = actions.add_parser("select", help="choose which engines search for and download recordings")
    json_option(select)
    select.add_argument("search", help="ID of an enabled search engine")
    select.add_argument("fetch", help="ID of an enabled fetch engine")
    json_option(actions.add_parser("clear-selection", help="turn off automatic use of download engines"))


def pairing_link(value):
    error = ValueError("Enter a room invitation with an http(s) address and one pairing token.")
    if not value or any(c.isspace() or not c.isprintable() or c == "\\" for c in value):
        raise error
    try:
        url = urlsplit(value)
        query = parse_qs(url.query, keep_blank_values=True, strict_parsing=True)
        port = url.port
        if (url.scheme not in ("http", "https") or not url.hostname or port == 0 or url.username is not None
                or url.path not in ("", "/") or url.fragment or set(query) != {"token"}
                or len(query["token"]) != 1 or not query["token"][0]):
            raise error
    except ValueError:
        raise error from None
    return url


def join(link):
    if link is None:
        if not sys.stdin.isatty():
            raise ValueError("Supply a room invitation or run listening join in a terminal.")
        link = getpass("Paste the room invitation (hidden): ")
    pairing_link(link)
    if not webbrowser.open(link):
        raise RuntimeError("Open the invitation in a browser on your device.")
    print("Opened the room invitation in your browser.")
    return 0


def api(port, route, body=None, *, host="127.0.0.1", owner_token=None):
    timeout = 250 if route.startswith("extensions") and body is not None else 15
    connection = http.client.HTTPConnection(host, port, timeout=timeout)
    headers = {"Content-Type": "application/json", "X-Listening-Local-Control": "1"}
    if owner_token:
        headers["Authorization"] = f"Bearer {owner_token}"
    try:
        connection.request("GET" if body is None else "POST", "/api/" + route,
                           body=json.dumps(body) if body is not None else None,
                           headers=headers)
        response = connection.getresponse()
        if not response.getheader("Server", "").startswith("ListeningRoom/"):
            raise RuntimeError("The selected address did not answer as a Listening server.")
        if response.status in (401, 403):
            raise RuntimeError("Manage the room on its server. If owner sign-in is required, run listening account set-owner.")
        if response.status != 200:
            raise RuntimeError(f"The room refused {route} (HTTP {response.status}). " +
                               display(json.load(response).get("error", "")))
        return json.load(response)
    finally:
        connection.close()


def setting_value(key, value):
    if key == "auto-download":
        if value.lower() not in ("on", "off", "true", "false"):
            raise ValueError("Use on or off for auto-download.")
        return value.lower() in ("on", "true")
    if key == "audio-store" and value not in ("database", "files"):
        raise ValueError("Use database or files for audio-store.")
    return value


def room_action(args, call):
    if args.group == "extensions":
        action = args.action
        if action in (None, "list"):
            return call("extensions")
        if action == "health":
            return call("extensions/health", {})
        if action == "radio":
            return call("extensions", {"action": action, "provider": args.provider, "enabled": args.state == "on"})
        if action in ("select", "clear-selection"):
            selection = {"search": args.search, "fetch": args.fetch} if action == "select" else {}
            return call("extensions", {"action": "select", "selection": selection})
        return call("extensions", {"action": action, "engine": args.engine})
    if args.group == "export":
        if args.action == "status":
            return call("export/status")
        body = {"playlist": args.playlist or ""}
        if args.format != "original":
            body["format"] = args.format
        return {"status": call("export", body)["status"]}
    if args.group == "settings":
        snapshot = call("settings")
        if args.action in (None, "show"):
            return {key.replace("-", "_"): snapshot[key.replace("-", "_")] for key in READABLE}
        key = args.key.replace("-", "_")
        if args.action == "set":
            value = setting_value(args.key, args.value)
            if key == "audio_store" and value == "files" and snapshot["collection"]:
                raise ValueError("Portable rooms store their audio in the database.")
            snapshot = call("settings", {key: value})
        return {key: snapshot[key]}
    if args.group == "invite" or args.action in ("invite", "rotate"):
        link = call("settings")["lan_url"]
        if not link:
            raise ValueError("The server could not determine a pairing address. Check its network.")
        if getattr(args, "action", None) == "rotate":
            link = call("pairing/rotate", {})["lan_url"]
        url = pairing_link(link)
        data = {"invitation": url.geturl()}
        if os.environ.get("LISTENING_ROOM_LOCAL_CONTROL") == "1":
            data["configured_origin"] = bool(configured_origin())
        return data
    if args.action in ("allow-invites", "deny-invites"):
        return call("devices/invites", {"id": args.device_id, "allowed": args.action == "allow-invites"})
    devices = call("devices")["paired_devices"]
    if args.action == "revoke":
        if not any(device["id"] == args.device_id for device in devices):
            raise ValueError("Unknown paired device. Use listening pair list to see device IDs.")
        call("devices/revoke", {"id": args.device_id})
        return {"revoked": args.device_id}
    return {"devices": devices}


def compose_action(command, args):
    words = [args.group]
    if getattr(args, "action", None):
        words.append(args.action)
    for field in ("device_id", "key", "value", "engine", "provider", "state", "search", "fetch"):
        if hasattr(args, field):
            words.append(getattr(args, field))
    if getattr(args, "playlist", None):
        words.extend(["--playlist", args.playlist])
    if getattr(args, "format", "original") != "original":
        words.extend(["--format", args.format])
    if getattr(args, "check_database", False):
        words.append("--check-database")
    result = subprocess.run([*command, "exec", "-T", "-e", "LISTENING_ROOM_LOCAL_CONTROL=1",
                             "listening-room", "/app/listening", *words, "--json"],
                            capture_output=True, text=True, timeout=270 if args.group == "extensions" else 45)
    if result.returncode and not (args.group in {"status", "doctor"} and result.stdout):
        message = result.stderr.strip().removeprefix("listening: ")
        raise RuntimeError(display(message) or "The container command failed. Check the server image version.")
    return json.loads(result.stdout)


def account_command(args, manager, config):
    if manager["kind"] == "compose" and os.environ.get("LISTENING_ROOM_LOCAL_CONTROL") != "1":
        raise ValueError("Run the account command inside the container using docker compose exec listening-room /app/listening.")
    from system.owner_auth import Household, OwnerAuth
    if args.action in {"add-stream-key", "stream-keys", "revoke-stream-key"}:
        auth = OwnerAuth(config.parent)
        if args.action == "add-stream-key":
            identifier, token = auth.create_stream_key(args.name)
            print(f"Key ID: {identifier}\nAPI key: {token}")
        elif args.action == "stream-keys":
            keys = [dict(row) for row in auth.stream_keys()]
            if args.json:
                emit({"keys": keys}, as_json=True)
            else:
                for key in keys:
                    print(key["id"], key["name"])
        elif not auth.revoke_stream_key(args.key_id):
            raise ValueError("Unknown streaming key.")
        else:
            print("Streaming key revoked.")
        return 0
    household = Household(config.parent)
    if args.action == "members":
        accounts = [{"id": account.id, "username": account.username, "role": account.role.value,
                     "enabled": account.enabled} for account in household.accounts()]
        if args.json:
            emit({"accounts": accounts}, as_json=True)
        else:
            for entry in accounts:
                print(entry["id"], entry["username"], entry["role"], "enabled" if entry["enabled"] else "disabled")
        return 0
    if args.action in {"set-owner", "add-member", "reset-member"}:
        if not sys.stdin.isatty():
            raise ValueError("Run the account command in a terminal so the password stays hidden.")
        password = getpass("Password: ")
        if password != getpass("Repeat password: "):
            raise ValueError("The passwords differ.")
        if args.action == "set-owner":
            OwnerAuth(config.parent).set_password(args.username, password)
            print("Owner login saved. Open the room in your browser and sign in.")
        elif args.action == "add-member":
            account = household.add_member(args.username, password)
            print(f"Added member {account.username} ({account.id}).")
        else:
            household.reset_password(args.member_id, password)
            print("Password changed. The member can sign in again.")
    elif args.action == "revoke-member":
        household.revoke_sessions(args.member_id)
        print("Member sessions revoked.")
    else:
        enabled = args.action == "enable-member"
        household.set_enabled(args.member_id, enabled)
        print("Member enabled." if enabled else "Member disabled.")
    return 0


def wants_invite(args):
    return args.group == "invite" or getattr(args, "action", None) in ("invite", "rotate")


def compose_bindings(command, port):
    output = subprocess.check_output([*command, "ps", "--format", "json", "listening-room"], text=True)
    # Compose v2 has emitted both an array and one JSON object per line.
    rows = json.loads(output) if output.lstrip().startswith("[") else [
        json.loads(line) for line in output.splitlines() if line.strip()]
    hosts = [entry["URL"] for row in rows for entry in row.get("Publishers") or []
             if entry.get("TargetPort") == 8765 and entry.get("PublishedPort") == port
             and entry.get("Protocol") == "tcp" and entry.get("URL")]
    if not hosts:
        raise ValueError(f"The Compose service does not publish room port 8765 at configured port {port}.")
    return hosts


def compose_origin(command):
    """Read the operator-selected origin from the resolved service environment."""
    output = subprocess.check_output([*command, "config", "--format", "json"], text=True)
    service = json.loads(output).get("services", {}).get("listening-room", {})
    return configured_origin(service.get("environment", {}).get("LISTENING_ROOM_ORIGIN") or "")


def listener_url(host, port):
    if host == "0.0.0.0":
        host = "127.0.0.1"
    if host == "::":
        host = "::1"
    return f"http://[{host}]:{port}/" if ":" in host else f"http://{host}:{port}/"


def compose_result(args, manager, command, ready):
    if args.group == "status":
        urls = [listener_url(host, manager["port"]) for host in compose_bindings(command, manager["port"])]
        url = next((address for address in urls if ready(address)), None)
        if not url:
            raise RuntimeError("The published Listening server did not answer at " + ", ".join(urls))
        data = compose_action(command, args)
        if data.get("state") == "stopped":
            raise RuntimeError("The container is active but its room listener is unavailable.")
        return {**data, "manager": "compose", "url": url}
    if wants_invite(args):
        hosts = compose_bindings(command, manager["port"])
        endpoints = (invitation_authority(None, manager["port"], host, magicdns=False) for host in hosts)
        if getattr(args, "tailscale", False):
            node = local_identity()
            endpoints = (addr for addr in endpoints if addr and node.matches(urlsplit("//" + addr).hostname))
        authority = next(filter(None, endpoints), "")
        if not authority:
            origin = compose_origin(command)
            if origin and (not getattr(args, "tailscale", False) or is_tailnet_host(urlsplit(origin).hostname)):
                rotating = getattr(args, "action", None) == "rotate"
                preview = argparse.Namespace(**{**vars(args), "action": "invite"}) if rotating else args
                result = compose_action(command, preview)
                link = pairing_link(result["invitation"])
                if configured_origin(f"{link.scheme}://{link.netloc}") != origin:
                    raise ValueError("The running room is using a different origin. Recreate the container first.")
                if rotating:
                    link = pairing_link(compose_action(command, args)["invitation"])
                return {"invitation": link.geturl()}
        if not authority and getattr(args, "tailscale", False):
            raise ValueError("Connect the Docker host to Tailscale and publish the room port on its Tailscale address.")
        if not authority:
            raise ValueError("For invitations, publish the Compose port on a reachable IPv4 or a specific reachable IPv6 address.")
        result = compose_action(command, args)
        link = pairing_link(result["invitation"])
        if result.get("configured_origin"):
            if getattr(args, "tailscale", False) and not is_tailnet_host(link.hostname):
                raise ValueError("Set the room origin to the server's Tailscale address or MagicDNS name.")
            return {"invitation": link.geturl()}
        return {"invitation": link._replace(netloc=authority).geturl()}
    return compose_action(command, args)


def local_result(args, info, kind, ready, *, config=None):
    host, port = info["host"], info["port"]
    url = listener_url(host, port)
    http_host = urlsplit(url).hostname
    if not ipaddress.ip_address(http_host).is_loopback:
        hint = "Owner commands need loopback access. Configure the server to listen on 0.0.0.0."
        if args.group != "status":
            raise RuntimeError(hint)
        if not ready(url):
            raise RuntimeError(f"The room did not answer at {url}")
        return {"state": "running", "manager": kind, "url": url, "detail": hint}
    options = {"host": http_host}
    from system.owner_auth import DATABASE_NAME, OwnerAuth
    if config is not None and (config.parent / DATABASE_NAME).is_file():
        options["owner_token"] = OwnerAuth(config.parent).cli_token()
    call = partial(api, port, **options)
    if args.group == "doctor":
        activity = call("activity")
        watchdog = activity.get("watchdog") or {}
        return {"state": "running", "manager": kind, "url": url,
                "workers": "Active" if activity.get("running") else "Idle",
                "watchdog": "Needs attention" if watchdog.get("last_error") else "OK"}
    if args.group == "status":
        activity = call("activity")
        return {"state": "running", "manager": kind, "url": url,
                "songs": activity["total"], "verified": activity["verified"],
                "queued_requests": activity["queued_requests"], "paired_devices": len(call("devices")["paired_devices"])}
    if wants_invite(args) and ipaddress.ip_address(host).is_loopback:
        saved = json.loads(config.read_text()) if config is not None and config.is_file() else {}
        if not configured_origin(os.environ.get("LISTENING_ROOM_ORIGIN") or saved.get("advertised_origin", "")):
            raise ValueError("A reachable network address is required. Check the server bind address and network.")
    return room_action(args, call)


def show_extensions(data):
    if "checks" in data:
        for check in data["checks"]:
            print(f"{display(check['label'])} [{display(check['state'])}] {display(check['message'])}")
        return
    radio = ", ".join(data["radio"]) or "Off"
    print("Radio providers:", display(radio))
    selection = data["selection"]
    if selection is None:
        print("Downloads: existing configuration")
    elif selection:
        print(f"Downloads: search with {display(selection['search'])}, fetch with {display(selection['fetch'])}")
    else:
        print("Downloads: no engine selected")
    if not data["configured"]:
        print("Engine manager: not configured on this host")
    if data["error"]:
        print("Engine manager:", display(data["error"]))
    installed = {entry["id"] for entry in data["installed"]}
    for engine in data["installed"]:
        state = "enabled" if engine.get("enabled") else "disabled"
        print(display(engine["id"]), display(engine["version"]), state)
    for engine in data["available"]:
        if engine["id"] not in installed:
            print(display(engine["id"]), display(engine["version"]), "available to install")


def live_command(args, manager, listener, service_commands, ready, *, config=None):
    kind = manager["kind"]
    diagnostics = args.group == "doctor"
    status = args.group == "status" or diagnostics
    try:
        if kind != "local":
            command, is_running = service_commands(manager)
            running = is_running()
        else:
            info = listener()
            running = bool(info)
        if not running:
            if not status:
                raise RuntimeError("The room is stopped. Run listening server start.")
            data = {"state": "stopped", "manager": kind}
        elif kind == "compose":
            data = compose_result(args, manager, command, ready)
        else:
            if kind == "systemd":
                info = listener()
                if not info:
                    raise RuntimeError("The service is active but its selected room control socket is unavailable.")
            data = local_result(args, info, kind, ready, config=config)
    except (ValueError, OSError, RuntimeError, subprocess.SubprocessError, http.client.HTTPException) as error:
        if not status:
            raise RuntimeError(str(error)) from error
        detail = "Check the listener and owner setup" if diagnostics else str(error)
        data = {"state": "unhealthy", "manager": kind, "detail": detail}
    if diagnostics and config is not None and kind != "compose":
        data.update(inspect_room(config, deep=args.check_database))
    if getattr(args, "tailscale", False) and kind != "compose":
        name = pairing_link(data["invitation"]).hostname
        node = local_identity()
        if not node.matches(name):
            raise RuntimeError("Connect the server to Tailscale. Check tailscale status and the room’s bind address.")
    if args.group == "extensions" and not args.json:
        show_extensions(data)
    elif "devices" in data and not args.json:
        if not data["devices"]:
            print("No paired devices. Run listening invite to connect one.")
        else:
            print("DEVICE ID                         PRESENCE   NAME")
            for device in data["devices"]:
                presence = "online" if device["present"] else "offline"
                print(f"{display(device['id']):<33} {presence:<10} {display(device['name'])}")
    else:
        single = "invitation" if "invitation" in data else None
        if args.group == "settings" and args.action == "get":
            single = args.key.replace("-", "_")
        emit(data, as_json=args.json, single=single)
    return int(status and data["state"] != "running")
