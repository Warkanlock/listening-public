package music.listening.room

import android.app.Activity
import android.content.res.ColorStateList
import android.view.Gravity
import android.view.View
import android.widget.LinearLayout
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
    fun action(label: String, click: () -> Unit) = with(ui) {
        info.section(button(label) { window.dismiss(); click() }, gap = 6)
    }
    with(ui) {
        if (openView != null) {
            val hasAlbum = item.creditTrackId() != null && item.optString("album").isNotBlank()
            info.section(style.navigation(SongDetailsView.DETAILS, hasAlbum) {
                window.dismiss()
                openView(it)
            })
        }
        if (details.isNotBlank()) info.section(label(details))
    }
    action("Add to playlist", onAddToPlaylist)
    if (status != "downloaded") {
        action("Find on YouTube") { openYouTube(this, item.optString("artist"), item.getString("title")) }
    }
    if (onTryMp3 != null) action("Try MP3", onTryMp3)
    when {
        onPlay != null && status == "downloaded" && (request.optString("audio").isNotBlank() || request.optString("audio_path").isNotBlank()) -> action("Play") { onPlay(request.getInt("id")) }
        status in CHANGEABLE_FORMAT -> action("Choose format", onRequest)
        item.has("musicbrainz_id") && item.optDouble("duration", 0.0) <= 0 -> with(ui) {
            info.section(label("A duration is needed to verify a room copy."))
        }
        status !in listOf("queued", "searching", "waiting_space", "downloaded") -> action("Find a copy", onRequest)
    }
    if (status != "downloaded") with(ui) {
        info.section(row().apply {
            addView(iconButton("View extensions", Mark.EXTENSIONS) { showRoomExtensions(room) })
        }, gap = 6)
    }
    window.show()
}

internal fun Activity.chooseDiscoveryFormat(row: JSONObject, room: Room, confirm: (String) -> Unit) {
    val options = listOf(
        "any_lossless" to "Any lossless (prefer FLAC)", "flac" to "FLAC only",
        "alac" to "Apple Lossless (ALAC) only", "mp3" to "MP3 only",
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
    choices.check(ids[options.indexOfFirst { it.first == row.optString("requested_format") }.coerceAtLeast(0)])
    with(ui) {
        content.section(label("Audio format to search for", color = secondary), gap = 4)
        content.section(choices)
        content.section(row(8).apply {
            addView(label("Download sources are configured in Extensions.", 14f, secondary), LinearLayout.LayoutParams(0, -2, 1f))
            addView(iconButton("View extensions", Mark.EXTENSIONS) { showRoomExtensions(room, backLabel = "Back to format") })
        })
        content.section(line(), gap = 8)
        content.section(row().apply {
            gravity = Gravity.END or Gravity.CENTER_VERTICAL
            addView(button("Search", solid = true) {
                val selected = options[ids.indexOf(choices.checkedRadioButtonId)].first
                dialog.dismiss()
                confirm(selected)
            }.apply {
                contentDescription = "Confirm and search"
                val search = mark(Mark.SEARCH, 18, onAccent).apply { setTintList(textColors) }
                setCompoundDrawablesRelative(search, null, null, null)
                compoundDrawablePadding = dp(6)
            })
        })
    }
    dialog.show()
}
