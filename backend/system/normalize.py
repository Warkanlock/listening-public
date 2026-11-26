"""The one title normalization, shared by search and by verification.

Server search and the runner's download verification must agree on when
two titles are the same song, so both use this function. Its semantics
are the verification side's, unchanged: case-folded, accents stripped,
tokens of ASCII letters and digits only.
"""

import re
import unicodedata


def normalize_title(value):
    plain = "".join(char for char in unicodedata.normalize("NFKD", (value or "").casefold())
                    if not unicodedata.combining(char))
    return " ".join(re.findall(r"[a-z0-9]+", plain))


def credit_key(value):
    value = value.strip()[:180] if isinstance(value, str) else ""
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return " ".join("".join(char if char.isalnum() or unicodedata.category(char).startswith("M")
                            else " " for char in normalized).split())


_FEATURE_CREDIT = re.compile(r"\s+[\[(]?(?:feat(?:uring)?|ft)\.?\s+", re.IGNORECASE)


def song_key(artist, title):
    """Identify a song credit for discovery, where guest credits can differ by edition."""
    primary = _FEATURE_CREDIT.split(artist or "", maxsplit=1)[0].strip()
    return credit_key(primary) or primary.casefold(), credit_key(title) or (title or "").strip().casefold()
