"""Match macOS filesystem paths reported with different spellings."""

from pathlib import Path
from unicodedata import normalize


def comparison_path(path):
    return Path(normalize("NFC", str(path).casefold()))
