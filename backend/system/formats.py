"""Audio containers accepted by the library and by new Soulseek searches."""

# Sockseek filters advertised filename extensions. The downloaded stream
# must also match the codec and container identified by ffprobe.
LOSSLESS_DOWNLOAD_FORMATS = {".flac": ("flac", "flac"), ".m4a": ("alac", "mov")}
AUDIO_FORMATS = frozenset({".mp3", ".aac", *LOSSLESS_DOWNLOAD_FORMATS})

# Keep the advertised suffixes in preference order for Sockseek.
REQUEST_SUFFIXES = {"flac": (".flac",), "alac": (".m4a",),
                    "any_lossless": tuple(LOSSLESS_DOWNLOAD_FORMATS), "mp3": (".mp3",)}
REQUEST_FORMAT_NAMES = {"flac": "FLAC", "alac": "Apple Lossless (ALAC)",
                        "any_lossless": "FLAC or Apple Lossless (ALAC)", "mp3": "MP3"}


DEFAULT_BULK_DOWNLOAD_FORMAT = "mp3_preferred"
# Each policy supplies the formats Sockseek can accept and the one it ranks first.
BULK_DOWNLOAD_FORMATS = {
    "mp3_preferred": ((".mp3", *LOSSLESS_DOWNLOAD_FORMATS), "mp3"),
    "mp3_only": ((".mp3",), "mp3"),
    "lossless_only": (tuple(LOSSLESS_DOWNLOAD_FORMATS), "flac"),
}


def bulk_download_format(value):
    return value if isinstance(value, str) and value in BULK_DOWNLOAD_FORMATS else DEFAULT_BULK_DOWNLOAD_FORMAT
