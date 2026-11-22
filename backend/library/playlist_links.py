"""Persistent playlist links over immutable source memberships."""
from collections import defaultdict
from contextlib import closing
from itertools import combinations
import json
import unicodedata
import uuid

from system import state_database

SCHEMA = """
CREATE TABLE IF NOT EXISTS playlist_sources (
    key TEXT PRIMARY KEY, name TEXT NOT NULL, provider TEXT NOT NULL,
    mirrored INTEGER NOT NULL, members TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS playlist_groups (id TEXT PRIMARY KEY, name TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS playlist_members (
    source TEXT PRIMARY KEY, group_id TEXT NOT NULL, position INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS playlist_dismissals (
    first TEXT NOT NULL, second TEXT NOT NULL, PRIMARY KEY(first,second)
);
"""
TABLES = ("playlist_sources", "playlist_groups", "playlist_members", "playlist_dismissals")


def _normalized(value):
    text = unicodedata.normalize("NFKC", value or "").casefold()
    return " ".join("".join(c if c.isalnum() else " " for c in text).split())


def _identities(records):
    """Only bridge metadata where it identifies one unambiguous recording."""
    groups = defaultdict(list)
    tokens = {}
    ambiguous = set()
    for row in records:
        idx = row["index"]
        tokens[idx] = row.get("isrc") or row.get("uri") or idx
        tags = tuple(_normalized(row.get(field)) for field in ("artist", "title", "album"))
        if all(tags):
            groups[tags].append(row)
    for tags, rows in groups.items():
        uris = {r["uri"] for r in rows if r.get("uri")}
        isrcs = {r["isrc"] for r in rows if r.get("isrc")}
        durations = [float(r["length"]) for r in rows if r.get("length")]
        if len(uris) > 1 or len(isrcs) > 1 or durations and max(durations) - min(durations) > 5.5:
            ambiguous.update(r["index"] for r in rows)
        else:
            key = next(iter(isrcs), None) or next(iter(uris), None) or tags
            for row in rows:
                tokens[row["index"]] = key
    return tokens, ambiguous


def _sources(db):
    return {key: {"name": name, "provider": provider, "mirrored": bool(mirrored),
                  "members": json.loads(members)} for key, name, provider, mirrored, members
            in db.execute("SELECT key,name,provider,mirrored,members FROM playlist_sources ORDER BY key")}


def _groups(db):
    result = {}
    for group, name, source in db.execute(
            "SELECT g.id,g.name,m.source FROM playlist_groups g JOIN playlist_members m ON g.id=m.group_id "
            "ORDER BY g.id,m.position"):
        result.setdefault(group, {"id": group, "name": name, "sources": []})["sources"].append(source)
    return result


def _join(db, base, extra, name):
    row = db.execute("SELECT group_id FROM playlist_members WHERE source=?", (base,)).fetchone()
    if row:
        group = row[0]
    else:
        group = "linked-" + uuid.uuid4().hex
        db.execute("INSERT INTO playlist_groups VALUES (?,?)", (group, name))
        db.execute("INSERT INTO playlist_members VALUES (?,?,0)", (base, group))
    db.execute("INSERT INTO playlist_members SELECT ?,?,max(position)+1 FROM playlist_members WHERE group_id=?",
               (extra, group, group))


def _auto(db, sources):
    by_provider = defaultdict(list)
    for key, source in sources.items():
        if source["provider"]:
            by_provider[source["provider"]].append(key)
    dismissed = set(db.execute("SELECT first,second FROM playlist_dismissals"))
    members = dict(db.execute("SELECT source,group_id FROM playlist_members"))
    for keys in by_provider.values():
        if len(keys) < 2:
            continue
        keys.sort(key=lambda key: (key not in members, sources[key]["mirrored"], key))
        base = keys[0]
        for extra in keys[1:]:
            if extra not in members and tuple(sorted((base, extra))) not in dismissed:
                _join(db, base, extra, sources[base]["name"])


def project(db, records, playlists, list_names, source_info):
    """Rebuild the logical lists in the same write transaction as the catalog."""
    old = _sources(db)
    db.execute("DELETE FROM playlist_sources")
    for key, entries in playlists.items():
        name, provider, mirrored = source_info[key]
        # A restored mirror carries its last authoritative identity until
        # its adapter reconnects. An arbitrary filename is never authority.
        if not provider and old.get(key, {}).get("mirrored"):
            provider, mirrored = old[key]["provider"], True
        db.execute("INSERT INTO playlist_sources VALUES (?,?,?,?,?)",
                   (key, name, provider, mirrored, json.dumps(entries)))
    sources = _sources(db)
    _auto(db, sources)
    groups = _groups(db)
    for record in records:
        record.pop("linked_recording", None)
    if not groups:
        return
    identities, _ = _identities(records)
    aliases = {}
    for group in groups.values():
        equivalent = defaultdict(set)
        present = [key for key in group["sources"] if key in playlists]
        if not present:
            continue
        order = []
        seen = set()
        for source in present:
            for idx in playlists.pop(source):
                if source == present[0] or idx and identities.get(idx, idx) not in seen:
                    order.append(idx)
                if idx:
                    token = identities.get(idx, idx)
                    seen.add(token)
                    equivalent[token].add(idx)
            list_names.pop(source, None)
        playlists[group["id"]] = order
        list_names[group["id"]] = group["name"]
        # A recording can occur in more than one linked group. Join those
        # sets only through an actual common catalog index.
        for indices in equivalent.values():
            if len(indices) > 1:
                merged = indices.union(*(aliases.get(idx, ()) for idx in indices))
                for idx in merged:
                    aliases[idx] = merged
    for record in records:
        if record["index"] in aliases:
            record["linked_recording"] = min(aliases[record["index"]])


def _candidates(db, sources, groups, records):
    identities, ambiguous = _identities(records)
    by_idx = {r["index"]: r for r in records}
    memberships = {key: group["sources"][0] for group in groups.values() for key in group["sources"]}
    excluded = set(db.execute("SELECT first,second FROM playlist_dismissals"))
    sets = {key: {identities.get(i, i) for i in s["members"] if i} for key, s in sources.items()}
    candidates = []
    def labels(indices):
        return [f"{by_idx[i]['artist']} — {by_idx[i]['title']}" for i in dict.fromkeys(indices) if i in by_idx]
    for left, right in combinations(sources, 2):
        if (left, right) in excluded:
            continue
        if left in memberships and right in memberships:
            continue
        if memberships.get(left, left) != left or memberships.get(right, right) != right:
            continue
        shared = sets[left] & sets[right]
        exact_small = (bool(shared) and sets[left] == sets[right]
                       and _normalized(sources[left]["name"]) == _normalized(sources[right]["name"]))
        if not exact_small and (len(shared) < 3 or len(shared) / len(sets[left] | sets[right]) < .8):
            continue
        if right in memberships or sources[left]["mirrored"] and left not in memberships:
            left, right = right, left
        a, b = sources[left]["members"], sources[right]["members"]
        candidates.append(dict(base=left, source=right, fixed_base=left in memberships, matched=len(shared),
                               matching=labels(i for i in a if identities.get(i) in shared),
                               base_only=labels(i for i in a if identities.get(i) not in shared),
                               source_only=labels(i for i in b if identities.get(i) not in shared),
                               ambiguous=labels(i for i in a + b if i in ambiguous)))
    return candidates


def is_group(database, key):
    with closing(state_database.connect(database, SCHEMA)) as db:
        return db.execute("SELECT 1 FROM playlist_groups WHERE id=?", (key,)).fetchone() is not None


def grouped_sources(database):
    """Current, present source keys behind each projected playlist, in base order."""
    with closing(state_database.connect(database, SCHEMA)) as db:
        groups = defaultdict(list)
        for group, key in db.execute(
                "SELECT group_id,source FROM playlist_members JOIN playlist_sources ON key=source "
                "ORDER BY group_id,position"):
            groups[group].append(key)
        return groups


def review(database, records, action=None):
    with closing(state_database.connect(database, SCHEMA)) as db:
        db.execute("BEGIN IMMEDIATE" if action is not None else "BEGIN")
        sources = _sources(db)
        groups = _groups(db)
        candidates = _candidates(db, sources, groups, records)
        if action is None:
            dismissed = list(db.execute("SELECT first,second FROM playlist_dismissals ORDER BY first,second"))
            return {"dismissed": dismissed, "sources": {k: v["name"] for k, v in sources.items()},
                    "links": list(groups.values()), "candidates": candidates}
        op = action.get("op")
        if any(not isinstance(action.get(field, ""), str) for field in ("id", "base", "source", "name")):
            raise ValueError("Invalid playlist choice")
        if op == "reconsider":
            db.execute("DELETE FROM playlist_dismissals WHERE first=? AND second=?",
                       sorted((action.get("base", ""), action.get("source", ""))))
        elif op == "group":
            base, extra = action.get("base"), action.get("source")
            if base not in sources or extra not in sources or base == extra:
                raise ValueError("Choose two different playlist sources")
            grouped = {source for group in groups.values() for source in group["sources"]}
            if base in grouped or extra in grouped:
                raise ValueError("A playlist is already grouped. Refresh the playlist matches.")
            _join(db, base, extra, sources[base]["name"])
            db.execute("DELETE FROM playlist_dismissals WHERE first=? AND second=?", sorted((base, extra)))
        elif op == "unlink":
            group = groups.get(action.get("id"))
            if not group:
                raise ValueError("Playlist link no longer exists")
            for pair in combinations(group["sources"], 2):
                db.execute("INSERT OR IGNORE INTO playlist_dismissals VALUES (?,?)", sorted(pair))
            db.execute("DELETE FROM playlist_members WHERE group_id=?", (group["id"],))
            db.execute("DELETE FROM playlist_groups WHERE id=?", (group["id"],))
        else:
            base, extra = action.get("base"), action.get("source")
            candidate = next((c for c in candidates if {c["base"], c["source"]} == {base, extra}), None)
            if candidate is None:
                raise ValueError("The suggested match has changed. Refresh and review the lists.")
            if op == "dismiss":
                db.execute("INSERT OR IGNORE INTO playlist_dismissals VALUES (?,?)", sorted((base, extra)))
            elif op == "confirm" and (not candidate["fixed_base"] or base == candidate["base"]):
                name = action.get("name") or sources[base]["name"]
                if not isinstance(name, str) or not name.strip() or len(name) > 120:
                    raise ValueError("Choose a playlist name of up to 120 characters")
                _join(db, base, extra, name.strip())
            else:
                raise ValueError("Unknown playlist action or invalid base list")
        db.commit()
