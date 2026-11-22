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
