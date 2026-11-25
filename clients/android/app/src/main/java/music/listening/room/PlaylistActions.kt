package music.listening.room

import android.app.Activity
import android.text.Editable
import android.text.TextWatcher
import android.widget.EditText
import android.widget.ImageButton
import android.widget.ScrollView
import org.json.JSONObject

/** Playlist capabilities come from the paired room, including its current Spotify grant. */
internal class RoomPlaylist(val row: JSONObject, private val link: JSONObject? = row.optJSONObject("spotify_link")) {
    val key = row.getString("id")
    val name = row.getString("name")
    val playable = row.optInt("playable")
    val canAdd = row.optBoolean("can_add")
    val source = row.optString("source")
    val needsWritePermission = link?.let { destination ->
        val targets = destination.optJSONArray("targets")
        destination.optBoolean("needs_write_permission") ||
            (targets != null && (0 until targets.length()).any {
                targets.getJSONObject(it).optBoolean("needs_write_permission")
            })
    } == true
    val spotify = link != null || source in setOf("spotify", "spotify-paused")
    val grouped get() = link?.optString("mode") == "multiple"
    val targetName get() = link?.optString("name").orEmpty()
    val providerPlaylistId get() = link?.optString("playlist_id").orEmpty()
    fun writableTargets(): List<RoomPlaylist> {
        val targets = link?.optJSONArray("targets") ?: return emptyList()
        return (0 until targets.length()).map(targets::getJSONObject).filter {
            it.optString("mode") == "two_way" && !it.isNull("account_id") && !it.isNull("playlist_id")
        }.map { RoomPlaylist(row, it) }
    }
    val syncLabel = when (link?.optString("mode")) {
        "two_way" -> "Spotify · Two-way sync"
        "paused" -> "Spotify · Sync paused"
        "read_only" -> "Spotify · Import only"
        "disconnected" -> "Spotify · Disconnected"
        "multiple" -> "Spotify · Multiple sources"
        else -> when (row.optString("source")) {
            "spotify-paused" -> "Spotify · Sync paused"
            "spotify" -> "Spotify · Linked"
            "linked" -> "Linked source"
            "watched" -> "Watched folder"
            else -> ""
        }
    }
    val linked get() = syncLabel.isNotEmpty()

    fun sameEdition(other: RoomPlaylist): Boolean =
        row.optString("revision") == other.row.optString("revision") &&
            row.optInt("entries") == other.row.optInt("entries") && name == other.name &&
            playable == other.playable && canAdd == other.canAdd && syncLabel == other.syncLabel &&
            link?.toString() == other.link?.toString()

    fun addBody(artist: String, title: String, album: String, spotifyTrack: String, trackId: Int?) =
        JSONObject().put("playlist", key).put("artist", artist).put("title", title).apply {
            if (album.isNotEmpty()) put("album", album)
            if (spotifyTrack.isNotEmpty()) put("track", spotifyTrack)
            if (trackId != null) put("track_id", trackId)
            if (link != null) {
                for (field in listOf("playlist_id", "account_id")) {
                    if (!link.isNull(field)) put(field, link.getString(field))
                }
            }
        }
}

internal data class SongDraft(val artist: String = "", val title: String = "", val album: String = "",
                              val trackId: Int? = null, val spotifyTrack: String = "") {
    companion object {
        fun from(track: JSONObject, catalogTrack: Boolean = false) = SongDraft(track.optString("artist"),
            track.optString("title"), track.optString("album"),
            if (catalogTrack) track.optInt("id").takeIf { it > 0 } else null,
            track.optString("spotify_url").takeUnless { it == "null" }.orEmpty())
    }
}

internal fun RoomUi.addSongForm(activity: Activity, destination: RoomPlaylist, draft: SongDraft,
                                submit: (JSONObject, (String?) -> Unit) -> Unit) {
    val style = SongDetailsStyle(this)
    val body = style.column()
    val dialog = style.dialog(activity, "Add song", ScrollView(activity).apply { addView(body) })
    body.section(heading(destination.name, 19f), gap = 4)
    if (destination.targetName.isNotBlank() && destination.targetName != destination.name)
        body.section(label(destination.targetName), gap = 4)
    if (destination.linked) body.section(label(destination.syncLabel, 14f, secondary).apply {
        setCompoundDrawablesRelative(mark(Mark.MAGNET, 17, accent), null, null, null)
        compoundDrawablePadding = dp(6)
    })
    body.section(label("Save the song details now. You can find an audio copy later.", 14f, secondary))
    fun input(labelText: String, value: String, limit: Int): EditText {
        val input = field(labelText).apply {
            id = android.view.View.generateViewId()
            filters = arrayOf(android.text.InputFilter.LengthFilter(limit))
            setText(value)
        }
        body.section(label(labelText, 14f, secondary).apply { labelFor = input.id }, gap = 4)
        body.section(input)
        return input
    }
    val limit = if (destination.spotify) 250 else 512
    val artist = input("Artist", draft.artist, limit)
    val title = input("Song title", draft.title, limit)
    val album = input("Album (optional)", draft.album, limit)
    val spotify = if (destination.spotify) input("Spotify track link (optional)", draft.spotifyTrack, 512) else null
    if (spotify != null && draft.spotifyTrack.isNotBlank()) {
        val releaseIdentity = object : TextWatcher {
            override fun beforeTextChanged(s: CharSequence?, start: Int, count: Int, after: Int) = Unit
            override fun onTextChanged(s: CharSequence?, start: Int, before: Int, count: Int) {
                if (spotify.text.toString().trim() == draft.spotifyTrack) spotify.setText("")
            }
            override fun afterTextChanged(s: Editable?) = Unit
        }
        listOf(artist, title, album).forEach { it.addTextChangedListener(releaseIdentity) }
    }
    val feedback = label("", 14f, secondary).apply {
        accessibilityLiveRegion = android.view.View.ACCESSIBILITY_LIVE_REGION_POLITE
    }
    body.section(feedback, gap = 4)
    lateinit var save: ImageButton
    save = iconButton("Add to playlist", Mark.PLAYLIST_ADD) {
        val artistName = artist.text.toString().trim()
        val song = title.text.toString().trim()
        if (artistName.isEmpty() || song.isEmpty()) {
            feedback.text = "Enter the artist and song title."
        } else {
            val release = album.text.toString().trim()
            val explicitLink = spotify?.text?.toString()?.trim().orEmpty()
            val sameTrack = draft.trackId.takeIf {
                artistName == draft.artist && song == draft.title && release == draft.album &&
                    (explicitLink.isEmpty() || explicitLink == draft.spotifyTrack)
            }
            val payload = destination.addBody(artistName, song, release, explicitLink, sameTrack)
            save.isEnabled = false
            feedback.text = "Adding song…"
            submit(payload) { error ->
                if (dialog.isShowing) {
                    if (error == null) dialog.dismiss() else {
                        save.isEnabled = true
                        feedback.text = error
                    }
                }
            }
        }
    }
    body.section(actionStrip(listOf(save), alignEnd = true), gap = 0)
    dialog.show()
}
