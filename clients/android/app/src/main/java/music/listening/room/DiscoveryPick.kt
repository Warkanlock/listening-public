package music.listening.room

import android.app.Activity
import android.app.Dialog
import android.content.res.ColorStateList
import android.view.View
import android.widget.ImageButton
import android.widget.RadioButton
import android.widget.RadioGroup
import android.widget.ScrollView
import org.json.JSONObject

internal val CHANGEABLE_FORMAT = setOf("waiting_format", "not_found", "error", "rejected")
internal val RETRYABLE_COPY = setOf("queued", "not_found", "error", "rejected")

internal fun JSONObject.needsCopyReview(): Boolean = optBoolean("copy_review") ||
    optString("request_state") in setOf("choose_copy", "copy_unavailable")

internal fun JSONObject.canReplaceRecording(): Boolean =
    has("request_state") && has("format_revision") &&
        (optString("status") in CHANGEABLE_FORMAT || optString("status") == "queued")

internal fun JSONObject.copyPending(): Boolean = optString("status") in setOf("queued", "searching", "waiting_space")

internal fun JSONObject.copyStatusLabel(): String {
    val specific = when (optString("request_state")) {
        "queued_copy" -> "Copy queued"
        "retry_copy" -> "Copy retry scheduled"
        "choose_copy" -> "Choose a copy"
        "copy_unavailable", "copy_missing" -> "Copy unavailable"
        "preparing" -> "Preparing playback"
        else -> null
    }
    return specific ?: legacyCopyStatusLabel()
}

private fun JSONObject.legacyCopyStatusLabel(): String = when (optString("status")) {
    "queued" -> if (optDouble("retry_at", 0.0) > 0) "Retry scheduled" else "Queued"
    "searching" -> when (optString("phase")) {
        "testing", "downloading" -> {
            val label = if (optString("phase") == "testing") "Trying a copy" else "Downloading"
            val percent = optInt("download_percent", -1)
            if (percent in 0..100) "$label · $percent%" else label
        }
        "verifying" -> "Verifying"
        else -> "Searching"
    }
    "waiting_space" -> "Waiting for space"
    "waiting_format" -> "Choose format"
    "not_found" -> "No copy found"
    "error" -> "Search failed"
    "rejected" -> if (optBoolean("copy_review")) "Choose a copy" else "Needs review"
    "downloaded" -> "Ready"
    "missing" -> "Copy unavailable"
    else -> ""
}

internal fun Activity.showDiscoveryPick(room: Room, item: JSONObject, request: JSONObject?,
                                        onPlay: (Int) -> Unit, onRequest: (JSONObject?) -> Unit, onTryMp3: (JSONObject) -> Unit,
                                        openView: ((SongDetailsView, JSONObject?) -> Unit)?,
                                        onRetry: (JSONObject) -> Unit, onVersions: (JSONObject?) -> Unit,
                                        onAddToPlaylist: () -> Unit, watch: (Dialog, (JSONObject) -> Unit) -> Unit) {
    val ui = RoomUi(this)
    val style = SongDetailsStyle(ui)
    val info = style.column()
    val window = style.dialog(this, item.getString("title"), ScrollView(this).apply { addView(info) }, artist = item.optString("artist"))
    var current = request
    with(ui) {
        if (openView != null) {
            val hasAlbum = item.creditTrackId() != null && item.optString("album").isNotBlank()
            info.section(style.navigation(SongDetailsView.DETAILS, hasAlbum) {
                window.dismiss()
                openView(it, current)
            })
        }
        val details = listOf(item.optString("version"), item.optString("album"), item.optString("reason"))
            .filter(String::isNotBlank).joinToString("\n\n")
        if (details.isNotBlank()) info.section(label(details))
        val feedback = label("", 14f, secondary)
        val statusChip = statusChip("")
        info.section(feedback)
        info.section(row().apply { addView(statusChip) })
        val controls = style.column()
        info.section(controls, gap = 6)
        val missingDuration = item.has("musicbrainz_id") && item.optDouble("duration", 0.0) <= 0
        var actionKey: List<Any?>? = null
        fun render(row: JSONObject?) {
            current = row
            val status = row?.optString("status")
            val stage = row?.optString("request_state")
            val retry = row?.optJSONObject("mp3_retry")
            val canPlay = row != null && status == "downloaded" && stage != "copy_missing" &&
                (row.optString("audio").isNotBlank() || row.optString("audio_path").isNotBlank())
            val detail = when {
                stage == "copy_unavailable" -> "Choose another copy to continue."
                row?.needsCopyReview() == true -> "No copy was saved."
                else -> row?.optString("message").orEmpty().takeUnless { it == row?.copyStatusLabel() }.orEmpty()
            }
            feedback.text = detail
            feedback.visibility = if (detail.isBlank()) View.GONE else View.VISIBLE
            statusChip.text = row?.copyStatusLabel().orEmpty()
            statusChip.visibility = if (statusChip.text.isBlank()) View.GONE else View.VISIBLE
            val key = listOf(status, stage, row?.needsCopyReview(), row?.optLong("format_revision"),
                row?.optString("musicbrainz_id"), canPlay, retry?.toString())
            if (key == actionKey) return
            actionKey = key
            controls.removeAllViews()
            val actions = buildList<ImageButton> {
                fun action(name: String, mark: Mark, click: () -> Unit) = add(iconButton(name, mark) {
                    window.dismiss()
                    click()
                })
                when {
                    canPlay && row != null -> action("Play", Mark.PLAY) { onPlay(row.getInt("id")) }
                    row?.needsCopyReview() == true -> action("Review copies", Mark.BROWSE) { onRequest(current) }
                    status in CHANGEABLE_FORMAT -> action("Choose format", Mark.SETTINGS) { onRequest(current) }
                    missingDuration -> controls.section(label("A duration is needed to verify a room copy."))
                    status !in listOf("queued", "searching", "waiting_space", "downloaded") ->
                        action("Find a copy", Mark.DOWNLOAD) { onRequest(current) }
                }
                if (retry != null) action("Try MP3", Mark.REFRESH) { onTryMp3(retry) }
                if (row != null && status in RETRYABLE_COPY && !row.needsCopyReview())
                    action("Retry now", Mark.REFRESH) { onRetry(row) }
                if (row == null || row.canReplaceRecording() || canPlay)
                    action("Choose a recording", Mark.ALBUM) { onVersions(row) }
                action("Add to playlist", Mark.PLAYLIST_ADD, onAddToPlaylist)
                if (status != "downloaded") {
                    action("Find on YouTube", Mark.YOUTUBE) { openYouTube(this@showDiscoveryPick, item.optString("artist"), item.getString("title")) }
                    add(iconButton("View extensions", Mark.EXTENSIONS) { showRoomExtensions(room) })
                }
            }
            controls.addView(actionStrip(actions))
        }
        render(request)
        window.show()
        if (request?.copyPending() == true) watch(window, ::render)
    }
}

internal fun Activity.chooseDiscoveryFormat(row: JSONObject, room: Room,
                                            browseFirst: Boolean = row.needsCopyReview(),
                                            confirm: (String, String?) -> Unit) {
    val options = listOf(
        "any_format" to "Any supported format (FLAC, ALAC, MP3 or AAC)",
        "any_lossless" to "Any lossless (prefer FLAC)", "flac" to "FLAC only",
        "alac" to "Apple Lossless (ALAC) only",
        "any_lossy" to "MP3 or AAC (prefer MP3)", "mp3" to "MP3 only",
    )
    val ui = RoomUi(this)
    val style = SongDetailsStyle(ui)
    val content = style.column()
    val dialog = style.dialog(this, "Choose format", ScrollView(this).apply { addView(content) },
        artist = listOf(row.optString("title"), row.optString("artist")).filter { it.isNotBlank() }.joinToString(" · "),
        closeLabel = "Later")
    val choices = RadioGroup(this)
    val ids = options.map { View.generateViewId() }
    for ((index, option) in options.withIndex()) choices.addView(RadioButton(this).apply {
        id = ids[index]
        text = option.second
        textSize = 16f
        setTextColor(ui.ink)
        buttonTintList = ColorStateList.valueOf(ui.accent)
        minimumHeight = ui.dp(48)
    }, RadioGroup.LayoutParams(-1, -2))
    val current = row.optString("requested_format").takeIf { value -> options.any { it.first == value } } ?: "any_lossless"
    choices.check(ids[options.indexOfFirst { it.first == current }])
    fun selectedFormat() = options[ids.indexOf(choices.checkedRadioButtonId)].first
    val feedback = ui.label("", 14f, ui.secondary).apply {
        visibility = View.GONE
        accessibilityLiveRegion = View.ACCESSIBILITY_LIVE_REGION_POLITE
    }
    val browse = ui.iconButton("Browse copies", Mark.BROWSE) {}
    with(ui) {
        content.section(choices)
        content.section(line(), gap = 8)
        content.section(feedback, gap = 8)
        browse.setOnClickListener {
            val format = selectedFormat()
            browse.isEnabled = false
            feedback.visibility = View.VISIBLE
            feedback.text = "Searching for copies…"
            Thread {
                val result = runCatching { room.browseCopies(row, format).getJSONArray("matches") }
                runOnUiThread {
                    if (isDestroyed || !dialog.isShowing) return@runOnUiThread
                    browse.isEnabled = true
                    result.onSuccess { matches ->
                        if (matches.length() == 0) {
                            feedback.text = "No copies found in this format."
                            return@onSuccess
                        }
                        val entries = (0 until matches.length()).map { index ->
                            val match = matches.getJSONObject(index)
                            val detail = listOf(
                                match.optString("format").uppercase(),
                                match.optInt("duration").takeIf { it > 0 }?.let { "${it / 60}:${(it % 60).toString().padStart(2, '0')}" }.orEmpty(),
                                match.optInt("bitrate").takeIf { it > 0 }?.let { "$it kbps" }.orEmpty(),
                                when {
                                    match.optBoolean("recommended") -> "Recommended"
                                    match.has("recommended") -> "Check match"
                                    else -> ""
                                },
                            ).filter(String::isNotBlank).joinToString(" · ")
                            Entry(match.getString("label"), detail, mark = Mark.DOWNLOAD) {
                                dialog.dismiss()
                                confirm(format, match.getString("key"))
                            }
                        }
                        feedback.visibility = View.GONE
                        ui.choices(this@chooseDiscoveryFormat, "Choose a copy", entries)
                    }.onFailure { feedback.text = it.message ?: "Couldn’t search for copies." }
                }
            }.start()
        }
        content.section(actionStrip(listOf(
            iconButton("View extensions", Mark.EXTENSIONS) { showRoomExtensions(room, backLabel = "Back to format") },
            browse,
            iconButton("Confirm and search", Mark.SEARCH) {
                dialog.dismiss()
                confirm(selectedFormat(), null)
            },
        ), alignEnd = true))
    }
    dialog.show()
    if (browseFirst) browse.performClick()
}
