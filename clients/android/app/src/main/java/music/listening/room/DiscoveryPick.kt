package music.listening.room

import android.app.Activity
import android.app.AlertDialog
import android.widget.ScrollView
import org.json.JSONObject

internal val CHANGEABLE_FORMAT = setOf("waiting_format", "not_found", "error", "rejected")

internal fun Activity.showDiscoveryPick(room: Room, item: JSONObject, request: JSONObject?,
                                        onPlay: ((Int) -> Unit)?, onRequest: () -> Unit, openView: ((SongDetailsView) -> Unit)?,
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
    when {
        onPlay != null && status == "downloaded" && (request.optString("audio").isNotBlank() || request.optString("audio_path").isNotBlank()) -> action("Play") { onPlay(request.getInt("id")) }
        status in CHANGEABLE_FORMAT -> action("Choose format", onRequest)
        item.has("musicbrainz_id") && item.optDouble("duration", 0.0) <= 0 -> with(ui) {
            info.section(label("A duration is needed to verify a room copy."))
        }
        status !in listOf("queued", "searching", "waiting_space", "downloaded") -> action("Find a copy", onRequest)
    }
    if (status != "downloaded") with(ui) {
        info.section(button("View download sources") { showRoomExtensions(room) }, gap = 6)
    }
    window.show()
}

internal fun Activity.chooseDiscoveryFormat(row: JSONObject, room: Room, confirm: (String) -> Unit) {
    val keys = listOf("any_lossless", "flac", "alac")
    var selected = keys.indexOf(row.optString("requested_format")).coerceAtLeast(0)
    AlertDialog.Builder(this).setTitle("Choose format")
        .setSingleChoiceItems(arrayOf("Any lossless (prefer FLAC)", "FLAC only", "Apple Lossless (ALAC) only"), selected) { _, i -> selected = i }
        .setNeutralButton("Extensions", null)
        .setNegativeButton("Later", null)
        .setPositiveButton("Confirm and search") { _, _ -> confirm(keys[selected]) }.show().apply {
            // Keep the chosen format and the request open while checking sources.
            getButton(AlertDialog.BUTTON_NEUTRAL).setOnClickListener { showRoomExtensions(room) }
        }
}
