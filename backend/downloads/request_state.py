"""Public lifecycle of a song request, projected from its durable queue row."""

COPY_REVIEW_MESSAGE = "Choose a copy to continue"
COPY_UNAVAILABLE_MESSAGE = "The selected copy could not be downloaded or verified. Browse copies again."
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
                "verifying": "verifying"}.get(row.get("phase"), "searching")
    if status == "rejected" and row.get("message") == COPY_REVIEW_MESSAGE:
        return "choose_copy"
    if status in FAILED_SEARCHES and (selected or row.get("message") == COPY_UNAVAILABLE_MESSAGE):
        return "copy_unavailable"
    return {
        "waiting_format": "choose_format", "waiting_space": "waiting_space",
        "downloaded": "ready", "not_found": "no_matches", "error": "search_failed",
        "rejected": "verification_failed", "pending": "copy_missing", "missing": "copy_missing",
    }.get(status, "searching")
