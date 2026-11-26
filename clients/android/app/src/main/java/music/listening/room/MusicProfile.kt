package music.listening.room

import android.app.Activity
import android.view.View
import android.widget.ScrollView
import android.widget.Toast
import org.json.JSONArray
import org.json.JSONObject

private val customProfileId = Regex("custom-[1-9][0-9]{0,17}")

internal fun JSONObject.musicProfileRef(): String? =
    creditTrackId() ?: optString("id").takeIf(customProfileId::matches)

private fun playableProfileRef(track: JSONObject): Any? {
    if (track.optBoolean("ready") && track.optInt("track_id") > 0) return track.getInt("track_id")
    val request = track.optJSONObject("request") ?: return null
    return if (request.optString("status") == "downloaded" && request.optString("audio").isNotBlank())
        "custom-${request.getInt("id")}" else null
}

internal fun RoomUi.showMusicProfile(
    activity: Activity, room: Room, target: String,
    playback: (String, JSONObject) -> Unit, previous: (() -> Unit)? = null,
) {
    val style = SongDetailsStyle(this)
    val content = style.column()
    val scroll = ScrollView(activity).apply { addView(content) }
    val dialog = style.dialog(activity, "Music profile", scroll)
    var pending: MetadataRequest? = null
    dialog.setOnDismissListener { pending?.cancel() }

    fun <T> load(work: (MetadataRequest) -> T, done: (Result<T>) -> Unit) {
        pending?.cancel()
        val request = MetadataRequest()
        pending = request
        request.future = metadataRequests.submit {
            val result = runCatching { work(request) }
            activity.runOnUiThread {
                if (dialog.isShowing && !activity.isDestroyed && pending === request) {
                    pending = null
                    done(result)
                }
            }
        }
    }

    fun requestCopy(track: JSONObject, action: View) {
        if (pending != null) return
        action.isEnabled = false
        val indicator = style.progress("Preparing request…")
        content.addView(indicator, content.indexOfChild(action) + 1)
        load({
            val id = track.optInt("track_id")
            if (id > 0) room.requestCatalogCopy(id)
            else room.pickRequest(track) ?: room.requestCopy(track)
        }) { outcome ->
            action.isEnabled = true
            content.removeView(indicator)
            outcome.onSuccess { request ->
                when {
                    request.optString("status") == "downloaded" && request.optString("audio").isNotBlank() -> {
                        dialog.dismiss()
                        val id = track.optInt("track_id")
                        if (id > 0) playback("play", JSONObject().put("track_id", id).put("playlist", "all"))
                        else playback("play_request", JSONObject().put("request_id", request.getInt("id")))
                    }
                    request.optString("status") in CHANGEABLE_FORMAT -> activity.chooseDiscoveryFormat(request, room) { updated ->
                        Toast.makeText(activity, updated.copyStatusLabel(), Toast.LENGTH_SHORT).show()
                    }
                    else -> Toast.makeText(activity, request.optString("message").ifBlank { "Request saved." },
                        Toast.LENGTH_SHORT).show()
                }
            }.onFailure {
                Toast.makeText(activity, "The copy could not be requested.", Toast.LENGTH_SHORT).show()
            }
        }
    }

    fun beginPage() {
        content.removeAllViews()
        scroll.scrollTo(0, 0)
        if (previous != null) content.section(button("Back") { dialog.dismiss(); previous() })
    }

    fun render(profile: JSONObject, retry: () -> Unit) {
        beginPage()
        content.section(heading(profile.optString("title").ifBlank { "Music profile" }, 24f))
        val caption = listOf("artist", "detail", "year", "area", "description")
            .map(profile::optString).filter(String::isNotBlank).joinToString(" · ")
        if (caption.isNotBlank()) content.section(label(caption, color = secondary))
        if (profile.optBoolean("unavailable")) {
            content.section(label("MusicBrainz is temporarily unavailable. Showing your library matches."))
            content.section(button("Retry MusicBrainz", action = retry), gap = 4)
        }
        val tracks = profile.optJSONArray("tracks") ?: JSONArray()
        val album = profile.optString("kind") == "album"
        val ready = (0 until tracks.length()).map(tracks::getJSONObject).mapNotNull(::playableProfileRef)
        if (album && ready.size > 1) content.section(button("Play available tracks", solid = true) {
            dialog.dismiss()
            playback("play_album", JSONObject().put("track_ids", JSONArray(ready)))
        })

        fun showRelated(section: String, property: String, kind: String, field: String) {
            val rows = profile.optJSONArray(property) ?: return
            if (rows.length() == 0) return
            content.section(heading(section, 19f))
            for (index in 0 until rows.length()) {
                val entry = rows.getJSONObject(index)
                val id = entry.optString(field)
                if (id.isBlank()) continue
                content.section(button(entry.getString("title")) {
                    dialog.dismiss()
                    showMusicProfile(activity, room, "$kind/$id", playback) {
                        showMusicProfile(activity, room, target, playback, previous)
                    }
                }, gap = 4)
            }
        }
        showRelated("Discography", "releases", "release-group", "id")
        showRelated("Albums in your library", "library_albums", "album", "track_id")
        if (tracks.length() > 0) content.section(heading(if (album) "Track list" else "Songs in your library", 19f))
        for (index in 0 until tracks.length()) {
            val track = tracks.getJSONObject(index)
            val ref = playableProfileRef(track)
            val number = if (track.optBoolean("library_only")) "Library" else track.optString("number")
            val title = listOf(number, track.getString("title")).filter(String::isNotBlank).joinToString(" · ")
            content.section(label(title), gap = 10)
            val request = track.optJSONObject("request")
            val copyStatus = request?.optString("status") ?: track.optString("copy_status")
            val retry = request?.optJSONObject("mp3_retry") ?: track.optJSONObject("mp3_retry")
            if (ref == null && copyStatus == "not_found")
                content.section(label("No copy found", color = secondary), gap = 4)
            when {
                ref != null -> content.section(button("Play") {
                    dialog.dismiss()
                    if (album) playback("play_album", JSONObject().put("track_ids", JSONArray(ready)).put("track_id", ref))
                    else if (ref is Int) playback("play", JSONObject().put("track_id", ref).put("playlist", "all"))
                }, gap = 4)
                retry != null -> content.section(button("Try MP3") {}.apply {
                    setOnClickListener {
                        isEnabled = false
                        text = "Requesting MP3…"
                        load({ room.confirmFormat(retry, "mp3") }) { result ->
                            text = if (result.isSuccess) "MP3 requested" else "Try MP3"
                            isEnabled = result.isFailure
                            if (result.isFailure) Toast.makeText(activity, "The MP3 search could not be started.", Toast.LENGTH_SHORT).show()
                        }
                    }
                }, gap = 4)
                track.optInt("track_id") > 0 || track.optDouble("duration", 0.0) > 0 ->
                    content.section(button("Find a copy") {}.apply {
                        setOnClickListener { requestCopy(track, it) }
                    }, gap = 4)
                else -> content.section(label("Duration unavailable", color = secondary), gap = 4)
            }
        }
        showRelated("Bands and related artists", "connections", "artist", "id")
        if (tracks.length() == 0 && (profile.optJSONArray("releases")?.length() ?: 0) == 0)
            content.section(label("No tracks are listed yet."))
    }

    fun fetch() {
        beginPage()
        content.section(style.loading(if (target.startsWith("artist/")) "Loading artist…" else "Loading album…"))
        load({ room.metadata("/api/music-profile/$target", it::bind) }) { result ->
            result.onSuccess { render(it, ::fetch) }.onFailure {
                beginPage()
                content.section(label("The profile could not load."))
                content.section(button("Try again", action = ::fetch))
            }
        }
    }

    dialog.show()
    fetch()
}
