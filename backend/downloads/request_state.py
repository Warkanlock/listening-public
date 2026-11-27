"""Public lifecycle of a song request, projected from its durable queue row."""

COPY_REVIEW_MESSAGE = "Choose a copy to continue"
COPY_UNAVAILABLE_MESSAGE = "The selected copy could not be downloaded or verified. Browse copies again."
COPY_FAILURES = {
    "transfer": "The source could not deliver this file.",
    "transfer_timeout": "The transfer timed out.",
    "handoff": "The download did not produce a complete file.",
    "verification": "The audio did not match the request or meet its format and quality requirements.",
    "decode": "The file did not decode completely.",
    "decode_timeout": "Audio verification timed out.",
}
COPY_REVIEW_STATES = ("choose_copy", "copy_unavailable")
FAILED_SEARCHES = ("not_found", "error", "rejected")
EDITABLE_FORMAT = ("waiting_format", *FAILED_SEARCHES)
REPLACEABLE_RECORDING = ("queued", *EDITABLE_FORMAT)


def request_state(row):
    status = row.get("status")
    selected = bool(row.get("selected_copy_key"))
    if status == "queued":
        if row.get("retry_at"):
            return "retry_copy" if selected else "retry_search"
        return "queued_copy" if selected else "queued_search"
    if status == "searching":
        return {"testing": "trying_copy", "downloading": "downloading",
                "verifying": "verifying"}.get(row.get("phase"), "starting_copy" if selected else "searching")
    if status == "rejected" and row.get("message") == COPY_REVIEW_MESSAGE:
        return "choose_copy"
    if status in FAILED_SEARCHES and (selected or row.get("message") == COPY_UNAVAILABLE_MESSAGE):
        return "copy_unavailable"
    return {
        "waiting_format": "choose_format", "waiting_space": "waiting_space",
        "downloaded": "ready", "not_found": "no_matches", "unavailable": "unavailable", "error": "search_failed",
        "rejected": "verification_failed", "pending": "copy_missing", "missing": "copy_missing",
    }.get(status, "searching")
