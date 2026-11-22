"""Validate signed setting declarations and keep values in the worker's private mount."""

import json
import re

from system.private_files import private_write

SETTINGS_FILE = "plugin-settings.json"
KEY = re.compile(r"[a-z][a-z0-9_]{0,39}")
MAX_FIELDS = 12
MAX_VALUE = 512
MAX_FILE = 32 * 1024


def declarations(fields):
    if not isinstance(fields, list) or len(fields) > MAX_FIELDS:
        raise ValueError("Invalid engine settings declaration.")
    keys = set()
    result = []
    for field in fields:
        if not isinstance(field, dict) or set(field) != {"key", "label", "type", "required"}:
            raise ValueError("Invalid engine setting.")
        key, label, kind, required = (field.get(name) for name in ("key", "label", "type", "required"))
        if (not isinstance(key, str) or not KEY.fullmatch(key) or key in keys
                or not isinstance(label, str) or not 0 < len(label.strip()) <= 80
                or not label.isprintable() or kind not in ("text", "secret") or type(required) is not bool):
            raise ValueError("Invalid engine setting.")
        result.append({"key": key, "label": label.strip(), "type": kind, "required": required})
        keys.add(key)
    return result


def valid_value(value):
    return (isinstance(value, str) and bool(value.strip()) and value.isprintable()
            and len(value.encode("utf-8", errors="surrogatepass")) <= MAX_VALUE)


def read(directory):
    path = directory / SETTINGS_FILE
    try:
        with path.open(encoding="utf-8") as source:
            data = json.loads(source.read(MAX_FILE + 1))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {key: value for key, value in data.items() if KEY.fullmatch(key) and valid_value(value)}


def saved_keys(directory, fields):
    keys = read(directory)
    return [field["key"] for field in fields if field["key"] in keys]


def write(directory, fields, updates):
    keys = {field["key"] for field in fields}
    if (not isinstance(updates, dict) or not updates or not set(updates) <= keys
            or any(value is not None and not valid_value(value) for value in updates.values())):
        raise ValueError("Enter values for the settings declared by this engine.")
    values = read(directory)
    for key, value in updates.items():
        if value is None:
            values.pop(key, None)
        else:
            values[key] = value
    payload = json.dumps(values, ensure_ascii=False)
    if len(payload.encode("utf-8")) > MAX_FILE:
        raise ValueError("The engine stores too many settings.")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    private_write(directory / SETTINGS_FILE, payload)
