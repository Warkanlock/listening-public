package music.listening.room

import android.app.Activity
import android.app.Dialog
import android.content.res.ColorStateList
import android.os.Build
import android.view.KeyEvent
import android.view.View
import android.window.OnBackInvokedCallback
import android.window.OnBackInvokedDispatcher
import android.widget.ImageButton
import android.widget.LinearLayout
import android.widget.RadioButton
import android.widget.RadioGroup
import android.widget.ScrollView
import org.json.JSONObject

internal val CHANGEABLE_FORMAT = setOf("waiting_format", "not_found", "error", "rejected")
internal val RETRYABLE_COPY = setOf("queued", "not_found", "error", "rejected")

internal fun JSONObject.needsCopyReview(): Boolean = when (optString("request_state")) {
    "choose_copy", "copy_unavailable" -> true
    "" -> optBoolean("copy_review")
    else -> false
}

internal fun JSONObject.canReplaceRecording(): Boolean =
    has("request_state") && has("format_revision") &&
        (optString("status") in CHANGEABLE_FORMAT || optString("status") == "queued")

internal fun JSONObject.copyPending(): Boolean = optString("status") in setOf("queued", "searching", "waiting_space")

internal fun JSONObject.copyStatusLabel(): String {
    val specific = when (optString("request_state")) {
        "queued_search" -> "Queued"
        "queued_copy" -> "Copy queued"
        "retry_search" -> "Retry scheduled"
        "retry_copy" -> "Copy retry scheduled"
        "searching" -> "Searching"
        "starting_copy" -> "Starting download"
        "trying_copy" -> transferLabel("Trying a copy")
        "downloading" -> transferLabel("Downloading")
        "verifying" -> "Verifying"
        "choose_copy" -> "Choose a copy"
        "copy_unavailable", "copy_missing" -> "Copy unavailable"
        "preparing" -> "Preparing playback"
        "choose_format" -> "Waiting for format"
        "waiting_space" -> "Waiting for space"
        "ready" -> "Ready"
        "no_matches" -> "No copy found"
        "search_failed" -> "Search failed"
        "verification_failed" -> "Needs review"
        else -> null
    }
    return specific ?: legacyCopyStatusLabel()
}

private fun JSONObject.transferLabel(label: String): String {
    val percent = optInt("download_percent", -1)
    return if (percent in 0..100) "$label · $percent%" else label
}

private fun JSONObject.legacyCopyStatusLabel(): String = when (optString("status")) {
    "queued" -> if (optDouble("retry_at", 0.0) > 0) "Retry scheduled" else "Queued"
    "searching" -> when (optString("phase")) {
        "testing" -> transferLabel("Trying a copy")
        "downloading" -> transferLabel("Downloading")
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
                if (row?.needsCopyReview() != true && (row == null || row.canReplaceRecording() || canPlay))
                    action("Other recordings", Mark.ALBUM) { onVersions(row) }
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

internal fun RoomUi.recordingChoice(version: JSONObject, onChoose: () -> Unit): View {
    val duration = version.getDouble("duration").toInt()
    val detail = listOf("artist", "version", "performance", "album").map(version::optString)
        .filter(String::isNotBlank) + "${duration / 60}:${(duration % 60).toString().padStart(2, '0')}"
    return button(version.getString("title") + "\n" + detail.joinToString(" · "), action = onChoose)
}

internal fun Activity.chooseDiscoveryFormat(row: JSONObject, room: Room, confirmed: (JSONObject) -> Unit) {
    val options = listOf(
        "any_format" to "Any supported format (FLAC, ALAC, MP3 or AAC)",
        "any_lossless" to "Any lossless (prefer FLAC)", "flac" to "FLAC only",
        "alac" to "Apple Lossless (ALAC) only",
        "any_lossy" to "MP3 or AAC (prefer MP3)", "mp3" to "MP3 only",
    )
    val ui = RoomUi(this)
    val style = SongDetailsStyle(ui)
    val content = style.column()
    val formatPanel = ui.column()
    val copyPanel = ui.column()
    val recordingsPanel = ui.column(8)
    var saving = false
    val feedback = ui.label("", 14f, ui.secondary).apply {
        visibility = View.GONE
        accessibilityLiveRegion = View.ACCESSIBILITY_LIVE_REGION_POLITE
    }
    val openingTitle = if (row.needsCopyReview()) "Choose a copy" else "Choose format"
    val dialogHeading = ui.heading(openingTitle, 23f)
    val dialog = style.dialog(this, openingTitle, ScrollView(this).apply { addView(content) },
        artist = listOf(row.optString("title"), row.optString("artist")).filter(String::isNotBlank).joinToString(" · "),
        titleView = dialogHeading, canDismiss = { !saving })
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
    var copyListing = 0
    var recordingListing = 0
    var copyMessage = ""
    var currentPanel: View? = null

    fun selectedFormat() = options[ids.indexOf(choices.checkedRadioButtonId)].first
    fun message(text: String) {
        feedback.text = text
        feedback.visibility = if (text.isEmpty()) View.GONE else View.VISIBLE
    }
    fun showPanel(title: String, panel: View) {
        currentPanel = panel
        dialogHeading.text = title
        dialog.window?.setTitle(title)
        content.removeAllViews()
        with(ui) {
            content.section(feedback)
            content.section(panel, gap = 0)
        }
    }
    fun backToCopies() {
        if (saving) return
        recordingListing++
        showPanel("Choose a copy", copyPanel)
        message(copyMessage)
    }
    fun handleBack(): Boolean {
        if (saving) return true
        if (currentPanel !== recordingsPanel) return false
        backToCopies()
        return true
    }
    fun submit(text: String, perform: () -> JSONObject) {
        if (saving) return
        saving = true
        dialog.setCancelable(false)
        message(text)
        Thread {
            val result = runCatching(perform)
            runOnUiThread {
                if (isDestroyed || !dialog.isShowing) return@runOnUiThread
                saving = false
                dialog.setCancelable(true)
                result.onSuccess {
                    dialog.dismiss()
                    confirmed(it)
                }.onFailure { message(it.message ?: "Could not queue this request.") }
            }
        }.start()
    }
    fun showRecordings() {
        if (saving) return
        val ticket = ++recordingListing
        recordingsPanel.removeAllViews()
        recordingsPanel.addView(ui.iconButton("Back to copies", Mark.BACK) { backToCopies() })
        showPanel("Other recordings", recordingsPanel)
        message("Looking for versions…")
        Thread {
            val result = runCatching { room.recordingVersions(row) }
            runOnUiThread {
                if (isDestroyed || !dialog.isShowing || ticket != recordingListing) return@runOnUiThread
                result.onSuccess { versions ->
                    message(if (versions.isEmpty()) "No other recordings found." else "")
                    for (version in versions) {
                        recordingsPanel.addView(ui.recordingChoice(version) {
                            submit("Changing recording…") { room.requestCopy(version, row) }
                        })
                    }
                }.onFailure {
                    message("Couldn’t load versions.")
                    recordingsPanel.addView(ui.iconButton("Try again", Mark.REFRESH) { showRecordings() })
                }
            }
        }.start()
    }
    fun showCopies(fresh: Boolean = false) {
        if (saving) return
        val ticket = ++copyListing
        val format = selectedFormat()
        copyPanel.removeAllViews()
        showPanel("Choose a copy", copyPanel)
        copyMessage = "Searching for copies…"
        message(copyMessage)
        with(ui) {
            val search = iconButton("Search again", Mark.REFRESH) { showCopies(fresh = true) }
            search.isEnabled = false
            copyPanel.section(actionStrip(buildList {
                if (row.needsCopyReview() && row.canReplaceRecording()) {
                    add(iconButton("Other recordings", Mark.ALBUM) { showRecordings() })
                }
                add(iconButton("Change format", Mark.SETTINGS) {
                    if (!saving) {
                        copyListing++
                        message("")
                        showPanel("Choose format", formatPanel)
                    }
                })
                add(search)
            }, alignEnd = true))
            Thread {
                val result = runCatching { room.browseCopies(row, format, fresh).getJSONArray("matches") }
                runOnUiThread {
                    if (isDestroyed || !dialog.isShowing || ticket != copyListing) return@runOnUiThread
                    search.isEnabled = true
                    result.onSuccess { matches ->
                        copyMessage = if (matches.length() == 0) "No copies found in this format." else ""
                        for (index in 0 until matches.length()) {
                            val match = matches.getJSONObject(index)
                            val name = match.getString("label")
                            val failure = match.optString("failure")
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
                            copyPanel.section(ui.row().apply {
                                addView(column().apply {
                                    addView(heading(name, 15f))
                                    if (detail.isNotEmpty()) addView(label(detail, 13f, secondary))
                                    if (failure.isNotEmpty()) addView(label(failure, 13f, ink))
                                }, LinearLayout.LayoutParams(0, -2, 1f))
                                val action = if (failure.isEmpty()) "Download" else "Retry"
                                addView(iconButton("$action $name", if (failure.isEmpty()) Mark.DOWNLOAD else Mark.REFRESH) {
                                    submit("Queueing copy…") { room.confirmFormat(row, format, match.getString("key")) }
                                })
                            }, gap = 8)
                            copyPanel.section(line(), gap = 4)
                        }
                    }.onFailure { copyMessage = it.message ?: "Could not search for copies." }
                    if (currentPanel === copyPanel) message(copyMessage)
                }
            }.start()
        }
    }
    with(ui) {
        formatPanel.section(choices)
        formatPanel.section(actionStrip(listOf(
            iconButton("View extensions", Mark.EXTENSIONS) { showRoomExtensions(room, backLabel = "Back to format") },
            iconButton("Browse copies", Mark.BROWSE) { showCopies() },
            iconButton("Confirm and search", Mark.SEARCH) {
                val format = selectedFormat()
                submit("Starting search…") { room.confirmFormat(row, format) }
            },
        ), alignEnd = true))
    }
    dialog.setOnKeyListener { _, key, event ->
        key == KeyEvent.KEYCODE_BACK && event.action == KeyEvent.ACTION_UP && handleBack()
    }
    dialog.show()
    if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
        val dispatcher = dialog.onBackInvokedDispatcher
        val callback = OnBackInvokedCallback { if (!handleBack()) dialog.cancel() }
        dispatcher.registerOnBackInvokedCallback(OnBackInvokedDispatcher.PRIORITY_DEFAULT, callback)
        dialog.setOnDismissListener { dispatcher.unregisterOnBackInvokedCallback(callback) }
    }
    if (row.needsCopyReview()) showCopies() else showPanel("Choose format", formatPanel)
}
