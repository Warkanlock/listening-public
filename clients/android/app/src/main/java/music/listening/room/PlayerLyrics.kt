package music.listening.room

import android.app.Activity
import android.widget.ScrollView
import android.widget.TextView
import org.json.JSONObject

private val playableRef = Regex("(?:custom-)?[1-9][0-9]{0,17}")
internal fun JSONObject.lyricRef() = optString("id").takeIf(playableRef::matches)

private val timestamp = Regex("""\[(\d+):(\d+(?:\.\d+)?)\]""")

private fun timedLines(value: String): List<Pair<Double, String>> = value.lineSequence().flatMap { line ->
    val text = timestamp.replace(line, "").trim().ifEmpty { "♪" }
    timestamp.findAll(line).map { stamp ->
        (stamp.groupValues[1].toDouble() * 60 + stamp.groupValues[2].toDouble()) to text
    }
}.sortedBy { it.first }.toList()

/** One sheet follows the recording while the player remains on screen. */
internal class PlayerLyrics(
    private val activity: Activity, private val ui: RoomUi, private val room: Room,
    private val seek: (String, Double) -> Unit, private val closed: () -> Unit,
) {
    private val scroll = ScrollView(activity)
    private val style = SongDetailsStyle(ui)
    private val body = style.column()
    private val song = ui.heading("", 24f)
    private val artist = ui.label("", color = ui.secondary)
    private val content = ui.column()
    private val dialog = style.dialog(activity, "Lyrics", scroll)
    private var pending: MetadataRequest? = null
    private var subject: String? = null
    private var position = 0
    private var seekable = false
    private var active = -1
    private val lines = mutableListOf<Pair<Double, TextView>>()

    fun show(track: JSONObject, seconds: Int, canSeek: Boolean) {
        body.addView(song)
        body.addView(artist)
        body.addView(content)
        scroll.addView(body)
        dialog.setOnDismissListener { pending?.cancel(); pending = null; closed() }
        dialog.show()
        update(track, seconds, canSeek)
    }

    fun close() = dialog.dismiss()

    fun update(track: JSONObject, seconds: Int, canSeek: Boolean) {
        val id = track.lyricRef() ?: return close()
        position = seconds
        if (seekable != canSeek) {
            seekable = canSeek
            lines.forEach { (_, view) -> view.isClickable = canSeek; view.isFocusable = canSeek }
        }
        if (subject == id) { highlight(); return }
        pending?.cancel()
        subject = id
        song.text = track.optString("title")
        artist.text = track.optString("artist")
        lines.clear()
        active = -1
        content.removeAllViews()
        scroll.scrollTo(0, 0)
        content.addView(ui.label("Looking for lyrics…"))
        val request = MetadataRequest()
        pending = request
        request.future = metadataRequests.submit {
            val result = runCatching { room.metadata("/api/lyrics/$id", request::bind) }.getOrNull()
            activity.runOnUiThread {
                if (pending !== request || !dialog.isShowing || activity.isDestroyed) return@runOnUiThread
                pending = null
                content.removeAllViews()
                if (result == null) {
                    content.addView(ui.label("Lyrics are unavailable right now."))
                    content.addView(ui.button("Retry") { subject = null; update(track, position, seekable) })
                    return@runOnUiThread
                }
                val timed = if (result.optBoolean("found")) timedLines(result.optString("synced")) else emptyList()
                if (timed.isEmpty()) {
                    val plain = result.optString("plain").takeIf { result.optBoolean("found") && it.isNotBlank() }
                    content.addView(ui.label(plain ?: "No lyrics found for this song.", 19f).apply { setTextIsSelectable(true) })
                    return@runOnUiThread
                }
                for ((time, text) in timed) {
                    val view = ui.label(text, 20f, ui.secondary).apply {
                        minHeight = ui.dp(44)
                        gravity = android.view.Gravity.CENTER_VERTICAL
                        setOnClickListener { if (seekable && subject == id) seek(id, time) }
                        isClickable = seekable
                        isFocusable = seekable
                    }
                    lines.add(time to view)
                    content.addView(view)
                }
                highlight()
            }
        }
    }

    private fun highlight() {
        val next = lines.indexOfLast { it.first <= position }
        if (next == active) return
        lines.getOrNull(active)?.second?.apply { setTextColor(ui.secondary); isSelected = false }
        active = next
        lines.getOrNull(next)?.second?.let { line ->
            line.setTextColor(ui.accentInk)
            line.isSelected = true
            if (!ui.eInk) scroll.post {
                if (dialog.isShowing && content.indexOfChild(line) == active)
                    scroll.smoothScrollTo(0, (content.top + line.top - scroll.height / 2 + line.height / 2).coerceAtLeast(0))
            }
        }
    }
}
