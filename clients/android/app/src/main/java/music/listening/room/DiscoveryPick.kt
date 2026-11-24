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

internal fun Activity.showDiscoveryPick(room: Room, item: JSONObject, request: JSONObject?,
                                        onPlay: ((Int) -> Unit)?, onRequest: () -> Unit, onTryMp3: (() -> Unit)?,
                                        openView: ((SongDetailsView) -> Unit)?,
                                        onAddToPlaylist: () -> Unit) {
    val status = request?.optString("status")
    val details = listOf(item.optString("version"), item.optString("album"), item.optString("reason"),
        when (status) {
            "queued" -> "Queued for download"
            "searching" -> "Searching for a copy"
            "waiting_space" -> "Waiting for storage space"
            "not_found" -> "No copy found"
            "error", "rejected" -> request.optString("message")
            else -> ""
        }).filter { it.isNotBlank() }.joinToString("\n\n")
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
        val missingDuration = item.has("musicbrainz_id") && item.optDouble("duration", 0.0) <= 0
        val actions = buildList<ImageButton> {
            fun action(name: String, mark: Mark, click: () -> Unit) = add(iconButton(name, mark) {
                window.dismiss()
                click()
            })
            when {
                onPlay != null && status == "downloaded" && (request.optString("audio").isNotBlank() || request.optString("audio_path").isNotBlank()) ->
                    action("Play", Mark.PLAY) { onPlay(request.getInt("id")) }
                status in CHANGEABLE_FORMAT -> action("Choose format", Mark.SETTINGS, onRequest)
                missingDuration -> info.section(label("A duration is needed to verify a room copy."))
                status !in listOf("queued", "searching", "waiting_space", "downloaded") ->
                    action("Find a copy", Mark.DOWNLOAD, onRequest)
            }
            if (onTryMp3 != null) action("Try MP3", Mark.REFRESH, onTryMp3)
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

internal fun Activity.chooseDiscoveryFormat(row: JSONObject, room: Room, confirm: (String) -> Unit) {
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
    with(ui) {
        content.section(choices)
        content.section(line(), gap = 8)
        content.section(actionStrip(listOf(
            iconButton("View extensions", Mark.EXTENSIONS) { showRoomExtensions(room, backLabel = "Back to format") },
            iconButton("Confirm and search", Mark.SEARCH) {
                val selected = options[ids.indexOf(choices.checkedRadioButtonId)].first
                dialog.dismiss()
                confirm(selected)
            },
        ), alignEnd = true))
    }
    dialog.show()
}
