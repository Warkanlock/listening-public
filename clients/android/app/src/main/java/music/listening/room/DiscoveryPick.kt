package music.listening.room

import android.app.Activity
import android.content.res.ColorStateList
import android.view.View
import android.widget.ImageButton
import android.widget.RadioButton
import android.widget.RadioGroup
import android.widget.ScrollView
import org.json.JSONObject

internal val CHANGEABLE_FORMAT = setOf("waiting_format", "not_found", "error", "rejected")
internal val RETRYABLE_COPY = setOf("queued", "not_found", "error", "rejected")

internal fun JSONObject.copyStatusLabel(): String = when (optString("status")) {
    "queued" -> if (optDouble("retry_at", 0.0) > 0) "Retry scheduled" else "Queued"
    "searching" -> when (optString("phase")) {
        "downloading" -> "Downloading"
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
                                        onPlay: ((Int) -> Unit)?, onRequest: () -> Unit, onTryMp3: (() -> Unit)?,
                                        openView: ((SongDetailsView) -> Unit)?,
                                        onRetry: (() -> Unit)?, onVersions: () -> Unit,
                                        onAddToPlaylist: () -> Unit) {
    val status = request?.optString("status")
    val failure = request?.optString("message").orEmpty().takeIf {
        it != request?.copyStatusLabel() && request?.optBoolean("copy_review") != true
    }.orEmpty()
    val details = listOf(item.optString("version"), item.optString("album"), item.optString("reason"), failure)
        .filter { it.isNotBlank() }.joinToString("\n\n")
    val ui = RoomUi(this)
    val style = SongDetailsStyle(ui)
    val info = style.column()
    val window = style.dialog(this, item.getString("title"), ScrollView(this).apply { addView(info) }, artist = item.optString("artist"))
    with(ui) {
        if (openView != null) {
            val hasAlbum = item.creditTrackId() != null && item.optString("album").isNotBlank()
            info.section(style.navigation(SongDetailsView.DETAILS, hasAlbum) {
                window.dismiss()
                openView(it)
            })
        }
        if (details.isNotBlank()) info.section(label(details))
        request?.copyStatusLabel()?.takeIf(String::isNotBlank)?.let { status ->
            info.section(row().apply { addView(statusChip(status)) })
        }
        val missingDuration = item.has("musicbrainz_id") && item.optDouble("duration", 0.0) <= 0
        val actions = buildList<ImageButton> {
            fun action(name: String, mark: Mark, click: () -> Unit) = add(iconButton(name, mark) {
                window.dismiss()
                click()
            })
            when {
                onPlay != null && status == "downloaded" && (request.optString("audio").isNotBlank() || request.optString("audio_path").isNotBlank()) ->
                    action("Play", Mark.PLAY) { onPlay(request.getInt("id")) }
                request?.optBoolean("copy_review") == true -> action("Review copies", Mark.BROWSE, onRequest)
                status in CHANGEABLE_FORMAT -> action("Choose format", Mark.SETTINGS, onRequest)
                missingDuration -> info.section(label("A duration is needed to verify a room copy."))
                status !in listOf("queued", "searching", "waiting_space", "downloaded") ->
                    action("Find a copy", Mark.DOWNLOAD, onRequest)
            }
            if (onTryMp3 != null) action("Try MP3", Mark.REFRESH, onTryMp3)
            if (onRetry != null) action("Retry now", Mark.REFRESH, onRetry)
            action("Choose a recording", Mark.ALBUM, onVersions)
            action("Add to playlist", Mark.PLAYLIST_ADD, onAddToPlaylist)
            if (status != "downloaded") {
                action("Find on YouTube", Mark.YOUTUBE) { openYouTube(this@showDiscoveryPick, item.optString("artist"), item.getString("title")) }
                add(iconButton("View extensions", Mark.EXTENSIONS) { showRoomExtensions(room) })
            }
        }
        info.section(actionStrip(actions), gap = 6)
    }
    window.show()
}

internal fun Activity.chooseDiscoveryFormat(row: JSONObject, room: Room,
                                            browseFirst: Boolean = row.optBoolean("copy_review"),
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
