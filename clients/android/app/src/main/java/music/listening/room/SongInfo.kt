package music.listening.room

import android.app.Activity
import android.content.ActivityNotFoundException
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.widget.Toast
import android.widget.ScrollView
import org.json.JSONObject

/** A snapshot of the room's catalog entry; opening it does not change playback. */
internal fun RoomUi.showSongInfo(activity: Activity, room: Room, track: JSONObject, copyAvailable: Boolean,
                                 queueAction: ((String) -> Unit)? = null, addToPlaylist: (() -> Unit)? = null,
                                 findCopy: (() -> Unit)? = null, view: SongDetailsView = SongDetailsView.DETAILS,
                                 profilePlayback: ((String, JSONObject) -> Unit)? = null,
                                 creditCache: MutableMap<CreditTarget, Credits> = mutableMapOf()) {
    val canExplore = track.creditTrackId() != null
    val hasAlbum = canExplore && track.optString("album").isNotBlank()
    val openView: (SongDetailsView) -> Unit = { next ->
        showSongInfo(activity, room, track, copyAvailable, queueAction, addToPlaylist, findCopy, next, profilePlayback, creditCache)
    }
    if (view != SongDetailsView.DETAILS) {
        showArtistConnections(activity, room, track, view == SongDetailsView.ALBUM, openView, creditCache)
        return
    }
    val style = SongDetailsStyle(this)
    val content = style.column()
    val context = content.context
    val title = track.getString("title")
    val artist = track.optString("artist")
    val facts = column().apply { section(heading("Recording details", 19f), gap = 8) }
    fun fact(name: String, value: String) {
        if (value.isNotBlank()) facts.addView(style.fact(name, value))
    }
    fact("Album", track.optString("album"))
    fact("Genre", track.optString("genre"))
    val seconds = track.optDouble("duration", 0.0).toInt()
    if (seconds > 0) fact("Duration", clockTime(seconds))
    fact("Availability", when (track.optString("status")) {
        "imported" -> "Saved on this device"
        "verified" -> "Ready to play"
        "waiting_format" -> "Waiting for format"
        "queued" -> "Queued for download"
        "searching" -> "Searching for a copy"
        "waiting_space" -> "Waiting for storage space"
        "not_found" -> "No copy found"
        "rejected" -> "Copy failed verification"
        "unmatched" -> "No matching copy found"
        else -> "Not downloaded"
    })
    fact("Audio codec", track.optString("codec"))
    val actions = column()
    val dialog = style.dialog(activity, title, ScrollView(context).apply { addView(content) }, artist)
    if (canExplore) content.section(style.navigation(view, hasAlbum) {
        dialog.dismiss()
        openView(it)
    })
    val trackId = track.musicProfileRef()
    if (trackId != null && profilePlayback != null) {
        content.section(button("Open artist") {
            dialog.dismiss()
            showMusicProfile(activity, room, "artist/$trackId", profilePlayback)
        }, gap = 6)
        if (track.optString("album").isNotBlank()) content.section(button("Open album track list") {
            dialog.dismiss()
            showMusicProfile(activity, room, "album/$trackId", profilePlayback)
        }, gap = 6)
    }
    content.section(facts)
    if (queueAction != null) {
        actions.section(button("Play next") { dialog.dismiss(); queueAction("play_next") }, gap = 6)
        actions.section(button("Add to queue") { dialog.dismiss(); queueAction("enqueue") }, gap = 6)
    }
    if (addToPlaylist != null) actions.section(button("Add to playlist") { dialog.dismiss(); addToPlaylist() }, gap = 6)
    if (!copyAvailable) {
        if (findCopy != null) {
            val label = if (track.optJSONObject("mp3_retry") == null) "Find a copy" else "Try MP3"
            actions.section(button(label, solid = true) { dialog.dismiss(); findCopy() }, gap = 6)
            actions.section(row().apply {
                addView(iconButton("View extensions", Mark.EXTENSIONS) { activity.showRoomExtensions(room) })
            }, gap = 6)
        }
        actions.section(button("Find on YouTube") { openYouTube(context, artist, title) }, gap = 6)
    }
    if (actions.childCount > 0) content.section(actions, gap = 6)
    dialog.show()
}

internal fun openYouTube(context: Context, artist: String, title: String) {
    val link = Uri.Builder().scheme("https").authority("www.youtube.com").path("/results")
        .appendQueryParameter("search_query", listOf(artist, title).filter(String::isNotBlank).joinToString(" "))
        .build()
    try {
        context.startActivity(Intent(Intent.ACTION_VIEW, link))
    } catch (_: ActivityNotFoundException) {
        Toast.makeText(context, "No app can open YouTube on this device.", Toast.LENGTH_LONG).show()
    }
}
