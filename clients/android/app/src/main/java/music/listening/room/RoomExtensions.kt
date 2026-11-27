package music.listening.room

import android.app.Activity
import android.content.ActivityNotFoundException
import android.content.Intent
import android.net.Uri
import android.view.View
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.Toast
import java.text.DateFormat
import java.util.Date
import org.json.JSONObject

private fun JSONObject.retryTime(): String? {
    val millis = optLong("retry_at") * 1000
    if (millis <= System.currentTimeMillis()) return null
    val nextMinute = (millis + 59_999) / 60_000 * 60_000
    return DateFormat.getTimeInstance(DateFormat.SHORT).format(Date(nextMinute))
}

/** Installed sources belong to the room. Only its owner manages them. */
internal fun Activity.showRoomExtensions(room: Room, backLabel: String? = null) {
    val ui = RoomUi(this)
    SongDetailsStyle(ui).dialog(this, "Extensions", roomExtensionsPanel(room, ui), backLabel = backLabel).show()
}

internal fun Activity.roomExtensionsPanel(room: Room, ui: RoomUi): View {
    val style = SongDetailsStyle(ui)
    val content = style.column()
    val panel = ScrollView(this).apply {
        if (ui.eInk) overScrollMode = View.OVER_SCROLL_NEVER
        addView(content)
    }
    val status = ui.column().apply { accessibilityLiveRegion = View.ACCESSIBILITY_LIVE_REGION_POLITE }
    val health = ui.column().apply { accessibilityLiveRegion = View.ACCESSIBILITY_LIVE_REGION_POLITE }
    val checkButton = ui.button("Run health check") {}
    var pending: MetadataRequest? = null
    var checking: MetadataRequest? = null
    fun refresh() {
        pending?.cancel()
        val request = MetadataRequest()
        pending = request
        status.removeAllViews()
        status.addView(style.progress("Checking room extensions…"))
        request.future = metadataRequests.submit {
            val response = runCatching { room.metadata("/api/extensions/sources", request::bind) }
            runOnUiThread {
                if (!panel.isAttachedToWindow || isDestroyed || pending !== request) return@runOnUiThread
                pending = null
                status.removeAllViews()
                response.onSuccess { sources ->
                    with(ui) {
                        val explanation = when (sources.optString("state")) {
                            "ready" -> null
                            "cooldown" -> sources.retryTime()?.let {
                                "A source couldn’t sign in. Retry after $it."
                            } ?: "Refresh to check the download source."
                            "legacy" -> "Find copy uses the room’s existing downloader."
                            "unconfigured" -> "The room owner can choose search and download sources in Extensions."
                            else -> "A download source is unavailable. Requests stay queued."
                        }
                        explanation?.let { status.section(label(it)) }
                        sources.optJSONObject("selection")?.let { selected ->
                            for ((key, name) in listOf("search" to "Search with", "fetch" to "Download with")) {
                                selected.optString(key).takeIf { it.isNotBlank() }?.let {
                                    status.section(style.fact(name, it))
                                }
                            }
                        }
                        status.section(heading("Installed in this room", 19f))
                        val installed = sources.optJSONArray("installed")
                        if (installed == null || installed.length() == 0) {
                            status.section(label(if (!sources.optBoolean("inventory_available")) "Installation status unavailable."
                                else "No download extensions installed.", color = secondary))
                        } else for (index in 0 until installed.length()) {
                            val plugin = installed.getJSONObject(index)
                            val retryTime = plugin.retryTime()
                            val enabled = plugin.optBoolean("enabled")
                            val state = when {
                                !enabled && retryTime != null -> "Disabled · can try enabling after $retryTime"
                                !enabled -> "Disabled"
                                retryTime != null -> "Waiting to retry until $retryTime"
                                plugin.optBoolean("ready") -> "Enabled"
                                else -> "Unavailable"
                            }
                            status.section(style.fact(plugin.getString("id"), listOf(plugin.optString("version"), state)
                                .filter { it.isNotBlank() }.joinToString(" · ")), gap = 6)
                        }
                    }
                }.onFailure { error ->
                    val message = when (error) {
                        is PairingRequired, is RoomUnreachable -> error.message ?: "Connect to your room and try again."
                        else -> "This room could not show its installed extensions. Check the connection and server version."
                    }
                    status.addView(ui.label(message))
                }
            }
        }
    }
    with(ui) {
        checkButton.setOnClickListener {
            checkButton.isEnabled = false
            val request = MetadataRequest()
            checking = request
            health.removeAllViews()
            health.addView(style.progress("Checking download sources…"))
            request.future = metadataRequests.submit {
                val result = runCatching { room.extensionHealth(request::bind).getJSONArray("checks") }
                runOnUiThread {
                    if (!panel.isAttachedToWindow || isDestroyed || checking !== request) return@runOnUiThread
                    checking = null
                    checkButton.isEnabled = true
                    health.removeAllViews()
                    result.onSuccess { checks ->
                        for (index in 0 until checks.length()) {
                            val check = checks.getJSONObject(index)
                            val state = when (check.optString("state")) {
                                "ready" -> "Passed"
                                "failed" -> "Needs attention"
                                else -> "Unverified"
                            }
                            health.section(style.fact(check.optString("label"), "$state · ${check.optString("message")}"))
                        }
                        refresh()
                    }.onFailure { error ->
                        health.addView(label(when (error) {
                            is PairingRequired, is RoomUnreachable -> error.message ?: "Connect to your room and try again."
                            else -> "Couldn’t run the check. Check your room server version and try again."
                        }))
                    }
                }
            }
        }
        content.addView(row(8).apply {
            addView(heading("Download sources", 19f), LinearLayout.LayoutParams(0, -2, 1f))
            addView(iconButton("Refresh extensions", Mark.REFRESH, outlined = false) { refresh() })
        })
        content.section(status)
        content.section(line())
        content.section(checkButton)
        content.section(health)
        content.section(button("Open room extensions in browser") {
            try {
                startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(room.origin + "/#extensions")))
            } catch (_: ActivityNotFoundException) {
                Toast.makeText(this@roomExtensionsPanel, "Open ${room.origin}/#extensions in a browser.", Toast.LENGTH_LONG).show()
            }
        })
    }
    panel.addOnAttachStateChangeListener(object : View.OnAttachStateChangeListener {
        override fun onViewAttachedToWindow(view: View) = refresh()
        override fun onViewDetachedFromWindow(view: View) {
            pending?.cancel()
            pending = null
            if (checking != null) {
                checking?.cancel()
                checking = null
                checkButton.isEnabled = true
                health.removeAllViews()
            }
        }
    })
    return panel
}
