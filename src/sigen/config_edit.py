"""`sigen config`: read and change the config file without hand-editing its JSON.

Keys are dotted paths: "poll.interval", "devices.inverter.ip", "smart_loads.0.name" (a number picks a list item).
Values are JSON ("15", "true", '{"ip": "192.168.1.100"}'), and anything that isn't JSON is a string, so
`site.timezone America/Edmonton` needs no quotes. A change is checked with the same rules `sigen` loads the file by
(config.parse_config) before anything is written, then written atomically, after a copy of the old file to
<name>.bak, keeping the file's owner and mode (640 root:sigen on the Pi). Secrets (the database URLs' passwords, the
ntfy topic, the cloud's two keys) are masked in everything printed.
"""

import copy
import json
import os
from pathlib import Path
from typing import Any

from sigen.db import redact
from sigen.errors import ConfigError
from sigen.ntfy import masked

SECRETS = {"database.url", "database.test_url", "alerts.ntfy_topic", "cloud.obfuscation_key", "cloud.client_basic"}
MISSING = object()  # what get() returns for a key that isn't set


def parse_key(key: str) -> list[str]:
    """'poll.interval' -> ['poll', 'interval']; ConfigError for an empty key or part."""
    parts = key.split(".")
    if not all(parts):
        raise ConfigError(f"{key!r} isn't a key like poll.interval or devices.inverter.ip")
    return parts


def parse_value(text: str, as_string: bool = False) -> Any:
    """The value `text` means: JSON if it parses ("15" -> 15, "true" -> True), else the text itself. With
    as_string, always the text ("15" stays "15")."""
    if as_string:
        return text
    try:
        return json.loads(text)
    except ValueError:
        return text


def _child(node: Any, part: str) -> Any:
    """node[part] for an object, node[int(part)] for a list; MISSING if there's no such thing."""
    if isinstance(node, dict):
        return node.get(part, MISSING)
    if isinstance(node, list) and part.isdigit() and int(part) < len(node):
        return node[int(part)]
    return MISSING


def get(doc: dict[str, Any], key: str) -> Any:
    """The value at `key` (a section is the whole object), or MISSING."""
    node: Any = doc
    for part in parse_key(key):
        node = _child(node, part)
        if node is MISSING:
            return MISSING
    return node


def set_value(doc: dict[str, Any], key: str, value: Any) -> dict[str, Any]:
    """A copy of `doc` with `key` set to `value`, making any missing sections on the way. A list item can be
    replaced (by number), not added. ConfigError if the path runs through a value that isn't a section."""
    new = copy.deepcopy(doc)
    parts = parse_key(key)
    node: Any = new
    for i, part in enumerate(parts[:-1]):
        child, where = _child(node, part), ".".join(parts[: i + 1])
        if child is MISSING and isinstance(node, dict):
            child = node[part] = {}
        elif child is MISSING:
            raise ConfigError(f"{where}: no such list item")
        elif not isinstance(child, (dict, list)):
            raise ConfigError(f"{where} isn't a section, so {key} can't be set")
        node = child
    last = parts[-1]
    if isinstance(node, dict):
        node[last] = value
    elif isinstance(node, list) and _child(node, last) is not MISSING:
        node[int(last)] = value
    else:
        raise ConfigError(f"{key}: only an existing list item can be set (by its number)")
    return new


def unset(doc: dict[str, Any], key: str) -> dict[str, Any]:
    """A copy of `doc` without `key`; ConfigError if it isn't set."""
    new = copy.deepcopy(doc)
    parts = parse_key(key)
    parent = get(new, ".".join(parts[:-1])) if len(parts) > 1 else new
    last = parts[-1]
    if isinstance(parent, dict) and last in parent:
        del parent[last]
    elif isinstance(parent, list) and _child(parent, last) is not MISSING:
        del parent[int(last)]
    else:
        raise ConfigError(f"{key} isn't set")
    return new


def mask(key: str, value: Any) -> Any:
    """`value` (found at `key`; "" for the whole file) as safe to print: secrets inside it masked."""
    if isinstance(value, dict):
        return {k: mask(f"{key}.{k}" if key else k, v) for k, v in value.items()}
    if isinstance(value, list):
        return [mask(f"{key}.{i}", v) for i, v in enumerate(value)]
    if key not in SECRETS or not isinstance(value, str):
        return value
    if key.startswith("database."):
        return redact(value)
    if key == "alerts.ntfy_topic":
        return masked(value)
    return "*" * 8


def shown(value: Any) -> str:
    """A value as `config get` prints it: a string bare, anything else as JSON; "(not set)" for MISSING."""
    if value is MISSING:
        return "(not set)"
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def write_document(path: Path, doc: dict[str, Any]) -> Path:
    """Replace the file at `path` with `doc` (2-space JSON) atomically, after copying the old file to <name>.bak;
    both keep the file's owner (where this process may set it) and mode, and are never readable by others on the
    way. Returns the backup's path."""
    stat = path.stat()
    backup = path.with_name(path.name + ".bak")
    tmp = path.with_name(path.name + ".tmp")
    _write_like(backup, path.read_bytes(), stat)
    _write_like(tmp, (json.dumps(doc, indent=2, ensure_ascii=False) + "\n").encode("utf-8"), stat)
    os.replace(tmp, path)
    return backup


def _write_like(target: Path, data: bytes, stat: os.stat_result) -> None:
    """Write `data` to `target`, created private (0600), then given `stat`'s mode and, if allowed, its owner."""
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    if hasattr(os, "chown"):  # not on Windows
        try:
            os.chown(target, stat.st_uid, stat.st_gid)
        except PermissionError:
            pass  # not root: the file was ours to begin with
    os.chmod(target, stat.st_mode & 0o7777)
