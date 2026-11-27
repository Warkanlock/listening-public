package music.listening.room

import android.content.Context
import android.text.TextUtils
import android.view.View
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import java.text.NumberFormat
import org.json.JSONObject

internal class PlaybackInsights(context: Context, private val ui: RoomUi) : ScrollView(context) {
    private val rates = NumberFormat.getNumberInstance().apply { maximumFractionDigits = 2 }
    private val down = ui.label("—", 18f)
    private val up = ui.label("—", 18f)
    private val status = ui.label("", 14f, ui.secondary)
    private val external = ui.label("This station streams directly from its provider.", 14f, ui.secondary)
        .apply { visibility = GONE }
    private val count = ui.label("—", 18f)
    private val buffered = ui.label("0", 18f)

    init {
        if (ui.eInk) overScrollMode = View.OVER_SCROLL_NEVER
        addView(with(ui) {
            column().apply {
                section(heading("Room traffic", 20f), gap = 3)
                section(status, gap = 8)
                addView(metric("Download", down))
                addView(line())
                addView(metric("Upload", up))
                addView(line())
                section(external, gap = 0)
                section(heading("Playback", 20f).apply { setPadding(0, dp(20), 0, 0) }, gap = 3)
                addView(metric("Songs in room queue", count))
                addView(line())
                addView(metric("Songs buffered here", buffered))
                addView(line())
            }
        })
    }

    private fun metric(title: String, value: TextView) = with(ui) {
        row(12).apply {
            minimumHeight = dp(48)
            setPadding(0, dp(6), 0, dp(6))
            val name = label(title)
            addView(name, LinearLayout.LayoutParams(0, -2, 1f))
            addView(value, LinearLayout.LayoutParams(-2, -2))
        }
    }

    private fun TextView.update(value: String) {
        if (!TextUtils.equals(text, value)) text = value
    }

    fun renderTraffic(sample: JSONObject) {
        val seconds = sample.getInt("window_seconds")
        val scale = 8.0 / (1000L * seconds.coerceAtLeast(1))
        down.update("${rates.format(sample.getLong("download_bytes") * scale)} kb/s")
        up.update("${rates.format(sample.getLong("upload_bytes") * scale)} kb/s")
        status.update("Last $seconds seconds")
    }

    fun trafficUnavailable(message: String) {
        down.update("—")
        up.update("—")
        status.update(message)
    }

    fun renderBuffer(total: Int) {
        buffered.update(total.toString())
    }

    fun renderQueue(session: JSONObject?) {
        count.update(session?.optInt("queue_length")?.toString() ?: "—")
    }

    fun renderSource(session: JSONObject) {
        val direct = session.optBoolean("radio_live") || session.optBoolean("radio_external")
        external.visibility = if (direct) VISIBLE else GONE
    }
}
