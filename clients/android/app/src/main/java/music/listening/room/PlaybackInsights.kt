package music.listening.room

import android.content.Context
import android.widget.LinearLayout
import android.widget.ScrollView
import java.text.NumberFormat
import org.json.JSONObject

internal class PlaybackInsights(context: Context, private val ui: RoomUi) : ScrollView(context) {
    private val rates = NumberFormat.getNumberInstance().apply { maximumFractionDigits = 2 }
    private val down = ui.label("— kb/s", 25f, ui.accentInk).apply { typeface = ui.headline }
    private val up = ui.label("— kb/s", 25f, ui.accentInk).apply { typeface = ui.headline }
    private val status = ui.label("", 14f, ui.secondary)
    private val external = ui.label("", 14f, ui.secondary)
    private val count = ui.label("Waiting for the room queue…", 18f)
    private val buffered = ui.label("0 songs buffered on this device.", 14f, ui.secondary)
    private val upcoming = List(5) {
        ui.label("", 16f).apply { setPadding(0, ui.dp(8), 0, ui.dp(8)); visibility = GONE }
    }

    init {
        addView(with(ui) {
            column().apply {
                section(heading("Playback traffic", 20f), gap = 4)
                section(status, gap = 8)
                section(row().apply {
                    gravity = android.view.Gravity.TOP
                    addView(metric("Down from room", down, "Audio and buffering"),
                        LinearLayout.LayoutParams(0, -1, 1f).apply { marginEnd = dp(8) })
                    addView(metric("Up to room", up, "Playback commands"),
                        LinearLayout.LayoutParams(0, -1, 1f))
                })
                section(external)
                section(panel().apply {
                    section(heading("Up next", 20f), gap = 4)
                    addView(count)
                    section(buffered, gap = 6)
                    upcoming.forEach { addView(it) }
                })
            }
        })
        external.visibility = GONE
    }

    private fun metric(title: String, rate: android.widget.TextView, caption: String) = with(ui) {
        panel().apply {
            addView(heading(title, 15f))
            addView(rate)
            addView(label(caption, 13f, secondary))
        }
    }

    fun renderTraffic(sample: JSONObject) {
        val seconds = sample.getInt("window_seconds")
        val scale = 8.0 / (1000 * seconds)
        down.text = "${rates.format(sample.getLong("download_bytes") * scale)} kb/s"
        up.text = "${rates.format(sample.getLong("upload_bytes") * scale)} kb/s"
        status.text = "Last $seconds seconds"
    }

    fun trafficUnavailable(message: String) {
        down.text = "— kb/s"
        up.text = "— kb/s"
        status.text = message
    }

    fun renderBuffer(total: Int) {
        buffered.text = if (total == 1) "1 song buffered on this device." else "$total songs buffered on this device."
    }

    fun renderQueue(session: JSONObject?) {
        val total = session?.optInt("queue_length")
        count.text = when (total) {
            null -> "The room queue will appear when you reconnect."
            1 -> "1 song queued"
            else -> "$total songs queued"
        }
        val tracks = session?.optJSONArray("upcoming")
        for ((index, label) in upcoming.withIndex()) {
            val track = tracks?.optJSONObject(index)
            label.visibility = if (track == null) GONE else VISIBLE
            if (track != null) label.text = "${index + 1}. ${track.getString("title")}\n${track.optString("artist")}"
        }
    }

    fun renderSource(session: JSONObject) {
        val direct = session.optBoolean("radio_live") || session.optBoolean("radio_external")
        external.visibility = if (direct) VISIBLE else GONE
        if (direct) external.text = "This station streams directly from its provider."
    }
}
