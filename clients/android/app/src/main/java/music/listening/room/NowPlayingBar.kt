package music.listening.room

import android.content.Context
import android.graphics.drawable.StateListDrawable
import android.text.TextUtils
import android.view.Gravity
import android.view.View
import android.widget.ImageView
import android.widget.LinearLayout
import org.json.JSONObject

internal enum class PlaybackTransport(val label: String, val operation: String, val mark: Mark = Mark.PLAY) {
    Play("Play", "resume"),
    Pause("Pause", "pause", Mark.PAUSE),
    StartQueue("Play queue", "next"),
    JoinAudio("Join audio", "snapshot"),
    ResumeRadio("Play", "radio_resume"),
    OpenYouTube("Play in YouTube", "radio_resume", Mark.EXTERNAL);

    companion object {
        fun forSnapshot(snapshot: JSONObject): PlaybackTransport {
            if (snapshot.optBoolean("radio_pending")) {
                return if (snapshot.optBoolean("radio_external")) OpenYouTube else ResumeRadio
            }
            val otherOutputsPlaying = snapshot.optBoolean("session_playing") &&
                snapshot.optBoolean("outputs_explicit") && !snapshot.optBoolean("output_selected", true)
            if (otherOutputsPlaying) return Pause
            if (!snapshot.optBoolean("playing")) return Play
            val localTrack = snapshot.optJSONObject("current")?.has("local_hash") == true
            return if (RoomPlayback.following || localTrack || snapshot.optBoolean("radio")) Pause else JoinAudio
        }
    }
}

/** The room and device-file players share one transport affordance while browsing. */
internal class NowPlayingBar(context: Context, private val ui: RoomUi, open: () -> Unit,
                             action: (String) -> Unit) : LinearLayout(context) {
    private var transport = PlaybackTransport.Play
    private val trackName = ui.label("", 15f).apply {
        typeface = ui.headline
        maxLines = 1
        ellipsize = TextUtils.TruncateAt.END
    }
    private val artistName = ui.label("", 13f, ui.secondary).apply {
        maxLines = 1
        ellipsize = TextUtils.TruncateAt.END
    }
    private val summary = ui.row(if (ui.eInk) 6 else 10).apply {
        minimumHeight = ui.dp(if (ui.eInk) 48 else 56)
        val vertical = ui.dp(if (ui.eInk) 3 else 5)
        setPadding(ui.dp(8), vertical, ui.dp(6), vertical)
        background = StateListDrawable().apply {
            val focus = intArrayOf(android.R.attr.state_focused)
            val pressed = intArrayOf(android.R.attr.state_pressed)
            addState(focus, ui.surface(ui.fog, ui.ink))
            addState(pressed, ui.surface(ui.fog, null))
            addState(intArrayOf(), ui.surface(ui.paper, null))
        }
        setOnClickListener { open() }
        isFocusable = true
        val mark = ImageView(context).apply {
            setImageDrawable(ui.mark(Mark.ALBUM, 22))
            scaleType = ImageView.ScaleType.CENTER
            importantForAccessibility = View.IMPORTANT_FOR_ACCESSIBILITY_NO
        }
        addView(mark, LayoutParams(ui.dp(30), ui.dp(36)))
        addView(ui.column().apply {
            importantForAccessibility = View.IMPORTANT_FOR_ACCESSIBILITY_NO_HIDE_DESCENDANTS
            addView(trackName)
            addView(artistName)
        }, LayoutParams(0, -2, 1f))
    }
    private val control = ui.iconButton("Play", Mark.PLAY, outlined = false) { action(transport.operation) }

    init {
        gravity = Gravity.CENTER_VERTICAL
        background = ui.surface(ui.paper)
        setPadding(ui.dp(1), ui.dp(1), ui.dp(3), ui.dp(1))
        addView(summary, LayoutParams(0, -2, 1f))
        addView(control, LayoutParams(ui.dp(48), ui.dp(48)))
        visibility = View.GONE
    }

    fun render(snapshot: JSONObject) {
        val track = snapshot.optJSONObject("current")
        if (track == null && (snapshot.optJSONArray("upcoming")?.length() ?: 0) > 0) {
            render("Playback queue", "", PlaybackTransport.StartQueue)
            return
        }
        render(track?.optString("title").orEmpty(), track?.optString("artist").orEmpty(),
            PlaybackTransport.forSnapshot(snapshot))
    }

    fun render(title: String, artist: String, transport: PlaybackTransport) {
        visibility = if (title.isBlank()) View.GONE else View.VISIBLE
        if (title.isBlank()) return
        if (!TextUtils.equals(trackName.text, title)) trackName.text = title
        if (!TextUtils.equals(artistName.text, artist)) artistName.text = artist
        artistName.visibility = if (artist.isBlank()) View.GONE else View.VISIBLE
        val subtitle = if (artist.isBlank()) title else "$title, $artist"
        val label = "Open now playing: $subtitle"
        if (!TextUtils.equals(summary.contentDescription, label)) summary.contentDescription = label
        if (this.transport == transport) return
        if (this.transport.mark != transport.mark) control.setImageDrawable(ui.controlMark(transport.mark, 23, ui.accentInk))
        this.transport = transport
        control.contentDescription = transport.label
        control.tooltipText = transport.label
    }
}
