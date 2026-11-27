"""Audio containers accepted by the library and by download requests."""

# Verification checks the suffix, codec, and container of each download.
FLAC = (".flac", "flac", "flac")
ALAC = (".m4a", "alac", "mov")
MP3 = (".mp3", "mp3", "mp3")
AAC = (".m4a", "aac", "mov")
LOSSLESS_DOWNLOAD_FORMATS = (FLAC[0], ALAC[0])
AUDIO_FORMATS = frozenset({MP3[0], ".aac", *LOSSLESS_DOWNLOAD_FORMATS})

# The first suffix is the search preference sent to the configured provider.
REQUEST_AUDIO_FORMATS = {
    "flac": (FLAC,),
    "alac": (ALAC,),
    "any_lossless": (FLAC, ALAC),
    "mp3": (MP3,),
    "any_lossy": (MP3, AAC),
    "any_format": (FLAC, ALAC, MP3, AAC),
}
REQUEST_SUFFIXES = {
    choice: tuple(dict.fromkeys(suffix for suffix, _, _ in formats))
    for choice, formats in REQUEST_AUDIO_FORMATS.items()
}
REQUEST_FORMAT_NAMES = {"flac": "FLAC", "alac": "Apple Lossless (ALAC)",
                        "any_lossless": "FLAC or Apple Lossless (ALAC)", "mp3": "MP3",
                        "any_lossy": "MP3 or AAC", "any_format": "FLAC, Apple Lossless (ALAC), MP3 or AAC"}


DEFAULT_BULK_DOWNLOAD_FORMAT = "mp3_preferred"
BULK_DOWNLOAD_FORMATS = {
    "mp3_preferred": ((".mp3", *LOSSLESS_DOWNLOAD_FORMATS), "mp3"),
    "mp3_only": ((".mp3",), "mp3"),
    "lossless_only": (LOSSLESS_DOWNLOAD_FORMATS, "flac"),
}


def bulk_download_format(value):
    return value if isinstance(value, str) and value in BULK_DOWNLOAD_FORMATS else DEFAULT_BULK_DOWNLOAD_FORMAT
