"""Assemble a trip from ready recordings already in the room."""
import hashlib
import random
import secrets
from collections import defaultdict


COUNTS = (30, 60, 90)


def recording_binding(collection, record):
    return hashlib.sha256((collection + "\0" + record["key"]).encode()).hexdigest()


def plan(catalog, count, collection):
    """Sample distinct recordings, with up to two genre mixes and a shuffle."""
    by_recording = {}
    for index in range(1, len(catalog.records) + 1):
        record = catalog.active_record_at(index)
        if record and catalog.status_of(index) == "verified":
            linked = record.get("linked_recording")
            identity = ("linked", linked) if linked is not None else ("key", record["key"])
            by_recording.setdefault(identity, (index, record))

    pool = list(by_recording.values())
    total = min(count, len(pool))
    genres = defaultdict(list)
    for entry in pool:
        genre = catalog.genre_canonical(entry[1])
        if genre:
            genres[genre].append(entry)

    eligible = [genre for genre, tracks in genres.items() if len(tracks) >= max(2, total // 3)]
    selected = random.sample(eligible, min(2, len(eligible)))
    mixes = []
    used = set()

    def add_mix(name, tracks):
        mixes.append({"name": name, "tracks": [
            {"id": index, "title": record["title"], "artist": record["artist"],
             "duration": record["length"] or 0,
             "audio": f"/api/audio/{index}?recording={recording_binding(collection, record)}"}
            for index, record in tracks
        ]})
        used.update(index for index, _ in tracks)

    mix_size = total // (len(selected) + 1 if selected else 2)
    for genre in selected:
        add_mix(genre, random.sample(genres[genre], min(mix_size, len(genres[genre]))))
    remaining = [entry for entry in pool if entry[0] not in used]
    shuffle = random.sample(remaining, total - len(used))
    if shuffle:
        if selected or len(shuffle) == 1:
            add_mix("Library shuffle", shuffle)
        else:
            split = len(shuffle) // 2
            add_mix("Library shuffle", shuffle[:split])
            add_mix("Second shuffle", shuffle[split:])
    return {"id": secrets.token_hex(16), "requested": count, "total": total, "mixes": mixes}
