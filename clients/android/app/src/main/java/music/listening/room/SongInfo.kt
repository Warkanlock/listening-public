package music.listening.room

import android.app.Activity
import android.content.ActivityNotFoundException
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.widget.LinearLayout
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
    val facts = column()
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
    val dialog = style.dialog(activity, title, ScrollView(context).apply {
        if (eInk) overScrollMode = android.view.View.OVER_SCROLL_NEVER
        addView(content)
    }, artist)
    if (canExplore) content.section(style.navigation(view, hasAlbum) {
        dialog.dismiss()
        openView(it)
    })
    val trackId = track.musicProfileRef()
    val actions = buildList<Entry> {
        fun action(name: String, mark: Mark, click: () -> Unit) = add(Entry(name, mark = mark) {
            dialog.dismiss()
            click()
        })
        if (queueAction != null) {
            action("Play next", Mark.QUEUE_NEXT) { queueAction("play_next") }
            action("Add to queue", Mark.QUEUE_ADD) { queueAction("enqueue") }
        }
        if (addToPlaylist != null) action("Add to playlist", Mark.PLAYLIST_ADD, addToPlaylist)
        if (!copyAvailable) {
            add(Entry("Find on YouTube", mark = Mark.YOUTUBE) { openYouTube(context, artist, title) })
            if (findCopy != null) add(Entry("View extensions", mark = Mark.EXTENSIONS) { activity.showRoomExtensions(room) })
        }
        if (trackId != null && profilePlayback != null) {
            action("Open artist", Mark.ARTIST) { showMusicProfile(activity, room, "artist/$trackId", profilePlayback) }
            if (track.optString("album").isNotBlank()) action("Open album track list", Mark.ALBUM) {
                showMusicProfile(activity, room, "album/$trackId", profilePlayback)
            }
        }
    }
    val copyAction = if (copyAvailable || findCopy == null) null else button(when {
        track.optJSONObject("mp3_retry") != null -> "Try MP3"
        track.optString("status") == "waiting_format" -> "Choose format"
        else -> "Find a copy"
    }, solid = true) {
        dialog.dismiss()
        findCopy()
    }
    if (copyAction != null || actions.isNotEmpty()) content.section(row(8).apply {
        if (copyAction != null) addView(copyAction, LinearLayout.LayoutParams(0, -2, 1f))
        if (actions.isNotEmpty()) addView(button("More actions") { choices(activity, title, actions) },
            LinearLayout.LayoutParams(0, -2, 1f))
    })
    content.section(facts)
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
