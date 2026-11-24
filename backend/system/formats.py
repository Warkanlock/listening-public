"""Audio containers accepted by the library and by download requests."""

# Sockseek filters advertised filename extensions. The downloaded stream
# must also match the codec and container identified by ffprobe.
LOSSLESS_DOWNLOAD_FORMATS = {".flac": ("flac", "flac"), ".m4a": ("alac", "mov")}
LOSSY_DOWNLOAD_FORMATS = {".mp3": ("mp3", "mp3"), ".m4a": ("aac", "mov")}
AUDIO_FORMATS = frozenset({".mp3", ".aac", *LOSSLESS_DOWNLOAD_FORMATS})

# The first suffix is the search preference sent to the configured provider.
REQUEST_AUDIO_FORMATS = {
    "flac": {".flac": LOSSLESS_DOWNLOAD_FORMATS[".flac"]},
    "alac": {".m4a": LOSSLESS_DOWNLOAD_FORMATS[".m4a"]},
    "any_lossless": LOSSLESS_DOWNLOAD_FORMATS,
    "mp3": {".mp3": LOSSY_DOWNLOAD_FORMATS[".mp3"]},
    "any_lossy": LOSSY_DOWNLOAD_FORMATS,
}
REQUEST_SUFFIXES = {choice: tuple(formats) for choice, formats in REQUEST_AUDIO_FORMATS.items()}
REQUEST_FORMAT_NAMES = {"flac": "FLAC", "alac": "Apple Lossless (ALAC)",
                        "any_lossless": "FLAC or Apple Lossless (ALAC)", "mp3": "MP3",
                        "any_lossy": "MP3 or AAC"}


DEFAULT_BULK_DOWNLOAD_FORMAT = "mp3_preferred"
# Each policy supplies the formats Sockseek can accept and the one it ranks first.
BULK_DOWNLOAD_FORMATS = {
    "mp3_preferred": ((".mp3", *LOSSLESS_DOWNLOAD_FORMATS), "mp3"),
    "mp3_only": ((".mp3",), "mp3"),
    "lossless_only": (tuple(LOSSLESS_DOWNLOAD_FORMATS), "flac"),
}


def bulk_download_format(value):
    return value if isinstance(value, str) and value in BULK_DOWNLOAD_FORMATS else DEFAULT_BULK_DOWNLOAD_FORMAT
