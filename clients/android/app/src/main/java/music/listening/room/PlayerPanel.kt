package music.listening.room

import android.app.Activity
import android.app.AlertDialog
import android.content.res.ColorStateList
import android.view.View
import android.view.ViewGroup
import android.widget.FrameLayout
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.SeekBar
import android.widget.TextView
import android.text.TextUtils
import org.json.JSONArray
import org.json.JSONObject
import java.util.Locale

internal fun clockTime(seconds: Int): String =
    String.format(Locale.getDefault(), "%d:%02d", seconds.coerceAtLeast(0) / 60, seconds.coerceAtLeast(0) % 60)

/** Keep the scrubber and controls in place between shared-session updates. */
internal class PlayerPanel(private val activity: Activity, private val ui: RoomUi, private val room: Room,
                           private val action: (String, JSONObject) -> Unit, private val addToPlaylist: ((JSONObject) -> Unit)?,
                           private val saveFavorite: ((FavoriteSong, Boolean, (Boolean?) -> Unit) -> Unit)?,
                           private val positionOpened: () -> Unit = {},
                           library: () -> Unit) : FrameLayout(activity) {
    private val scroll = if (ui.eInk) null else ScrollView(activity)
    private var current: JSONObject? = null
    private var lyricSheet: PlayerLyrics? = null
    private var positionDialog: AlertDialog? = null
    val needsLivePosition: Boolean get() = positionDialog != null || lyricSheet != null

    private fun showLyrics() {
        val track = current ?: return
        if (track.lyricRef() == null) return
        val sheet = PlayerLyrics(activity, ui, room, { id, time ->
            if (!snapshot.optBoolean("radio_pending") && current?.lyricRef() == id)
                action("seek", JSONObject().put("position", time).put("expected_ref", snapshot.get("current_ref"))
                    .put("lyric_radio", snapshot.optBoolean("radio")))
        }) { lyricSheet = null }
        lyricSheet = sheet
        sheet.show(track, seconds, !snapshot.optBoolean("radio_pending"))
        positionOpened()
    }
    override fun onDetachedFromWindow() {
        lyricSheet?.close()
        positionDialog?.dismiss()
        super.onDetachedFromWindow()
    }

    override fun onMeasure(widthMeasureSpec: Int, heightMeasureSpec: Int) {
        if (scroll != null && MeasureSpec.getMode(heightMeasureSpec) != MeasureSpec.UNSPECIFIED) {
            // Leave room for the caption, transport and queue on compact screens.
            sleeve.limitArtworkHeight(MeasureSpec.getSize(heightMeasureSpec) - ui.dp(236))
        }
        super.onMeasure(widthMeasureSpec, heightMeasureSpec)
    }
    private var snapshot = JSONObject()
    private var playing = false
    private var dragging = false
    private var seconds = 0
    private var duration = 0
    private val timeline = ui.column().apply {
        visibility = View.GONE
        if (ui.eInk) setPadding(ui.dp(16), 0, ui.dp(16), ui.dp(12))
    }
    private val position = ui.iconButton("Show position", Mark.POSITION, action = ::togglePosition)

    private fun togglePosition() {
        if (ui.eInk) {
            if (positionDialog != null) return
            val dialog = SongDetailsStyle(ui).dialog(activity, "Playback position", timeline,
                closeLabel = "Close position")
            positionDialog = dialog
            timeline.visibility = View.VISIBLE
            dialog.setOnDismissListener {
                dragging = false
                positionDialog = null
                timeline.visibility = View.GONE
                (timeline.parent as? ViewGroup)?.removeView(timeline)
                updatePosition()
            }
            updatePosition()
            dialog.show()
            positionOpened()
            return
        }
        val open = timeline.visibility != View.VISIBLE
        timeline.visibility = if (open) View.VISIBLE else View.GONE
        updatePosition()
        if (open && scroll != null) timeline.post {
            timeline.requestRectangleOnScreen(android.graphics.Rect(0, 0, timeline.width, timeline.height), true)
        }
    }

    private val emptyHint = ui.label("Choose a song to start playback.")
    private val playQueue = ui.button("Play queue", true) { action("next", JSONObject()) }
    private val idleDevices = ui.button("Playback devices", action = ::showDevices).apply { visibility = View.GONE }
    private val empty = ui.playerContent(
        ui.column(8).apply {
            addView(emptyHint)
            addView(ui.button("Browse songs", true, library))
        },
        ui.column(8).apply {
            addView(playQueue)
            addView(ui.queueButton(::showQueue))
            addView(idleDevices)
        },
    )
    private val details: LinearLayout
    private val more = ui.iconButton("More playback options", Mark.MORE, action = ::showOptions)
    private var savingFavorite = false
    private val heart = ui.iconButton("Add to favorites", Mark.HEART, outlined = false, action = ::toggleFavorite)
        .apply { visibility = View.GONE }
    private val sleeve = RecordSleeve(context, ui, favoriteAction = heart.takeIf { saveFavorite != null })
    private val queue = ui.queueButton(::showQueue).apply {
        maxLines = 2
        ellipsize = TextUtils.TruncateAt.END
    }
    private val elapsed = ui.label("", 14f, ui.secondary)
    private val length = ui.label("", 14f, ui.secondary)

    private fun toggleFavorite() {
        val save = saveFavorite ?: return
        val track = current ?: return
        val song = FavoriteSong.from(track) ?: return
        if (savingFavorite || snapshot.optBoolean("radio")) return
        savingFavorite = true
        renderFavorite()
        save(song, !track.getBoolean("favorite")) { favorite ->
            current?.takeIf { it.optString("id") == song.ref }?.let {
                if (favorite != null) it.put("favorite", favorite)
            }
            savingFavorite = false
            renderFavorite()
        }
    }

    private fun renderFavorite() {
        val track = current ?: return
        val available = saveFavorite != null && !snapshot.optBoolean("radio") && FavoriteSong.from(track) != null
        heart.visibility = if (available) View.VISIBLE else View.GONE
        heart.isEnabled = available && !savingFavorite
        if (!available) return
        val favorite = track.getBoolean("favorite")
        val label = if (favorite) "Remove ${track.optString("title")} from favorites" else "Favorite ${track.optString("title")}"
        if (!TextUtils.equals(heart.contentDescription, label)) {
            heart.contentDescription = label
            heart.tooltipText = label
        }
        if (heart.isSelected != favorite) {
            heart.isSelected = favorite
            heart.setImageDrawable(ui.mark(if (favorite) Mark.HEART_FILLED else Mark.HEART, 23, ui.accentInk))
        }
    }

    private fun showSongDetails() {
        val track = current ?: return
        ui.showSongInfo(activity, room, track, copyAvailable = true,
            addToPlaylist = addToPlaylist?.let { add -> { add(track) } },
            profilePlayback = if (track.has("local_hash")) null else action)
    }

    private fun profileAction(track: JSONObject, kind: String): (() -> Unit)? {
        if (track.has("local_hash") || snapshot.optBoolean("radio_external")) return null
        val ref = track.musicProfileRef() ?: return null
        return { ui.showMusicProfile(activity, room, "$kind/$ref", action) }
    }

    private fun showOptions() {
        val options = buildList {
            add(Entry("Song information", open = ::showSongDetails))
            current?.let { track ->
                if (track.optString("album").isNotBlank()) {
                    profileAction(track, "album")?.let { add(Entry("Album track list", open = it)) }
                }
                profileAction(track, "artist")?.let { add(Entry("Artist", open = it)) }
            }
            if (current?.lyricRef() != null) add(Entry("Lyrics", open = ::showLyrics))
            if (snapshot.has("shuffle")) {
                val label = if (snapshot.optBoolean("shuffle")) "Turn shuffle off" else "Turn shuffle on"
                add(Entry(label) { action("shuffle", JSONObject()) })
            }
            if (hasDevices()) add(Entry("Playback devices", open = ::showDevices))
            if (snapshot.optBoolean("radio")) {
                val label = if (snapshot.optBoolean("radio_external")) "Forget station" else "Stop radio"
                add(Entry(label) { action("radio_stop", JSONObject()) })
            }
        }
        ui.choices(activity, "Playback options", options)
    }
    private val transport = ui.iconButton("Play", Mark.PLAY, true) {
        val op = when {
            snapshot.optBoolean("radio_pending") -> "radio_resume"
            controlsRemote() || (playing && (RoomPlayback.following || snapshot.optBoolean("radio"))) -> "pause"
            playing -> "snapshot"
            else -> "resume"
        }
        action(op, JSONObject())
    }
    private val previous = ui.iconButton("Previous track", Mark.PREVIOUS) {
        action("previous", JSONObject())
    }
    private val next = ui.iconButton("Next track", Mark.NEXT) { action("next", JSONObject()) }
    private val bar = SeekBar(context).apply {
        contentDescription = "Playback position"
        progressTintList = ColorStateList.valueOf(ui.ink); thumbTintList = ColorStateList.valueOf(ui.ink)
        minimumHeight = ui.dp(48)
        setOnSeekBarChangeListener(object : SeekBar.OnSeekBarChangeListener {
            override fun onStartTrackingTouch(bar: SeekBar) { dragging = true }
            override fun onProgressChanged(bar: SeekBar, value: Int, fromUser: Boolean) {
                if (fromUser) {
                    elapsed.text = clockTime(value)
                    if (!dragging) action("seek", JSONObject().put("position", value))
                }
            }
            override fun onStopTrackingTouch(bar: SeekBar) {
                dragging = false
                action("seek", JSONObject().put("position", bar.progress))
            }
        })
    }

    init {
        val body = ui.column(8)
        if (scroll == null) {
            setBackgroundColor(ui.paper)
            addView(body, LayoutParams(-1, -2))
        } else {
            scroll.addView(body)
            addView(scroll, LayoutParams(-1, -2))
        }
        val controls = ui.column(8).apply {
            addView(ui.transport(previous, transport, next), LinearLayout.LayoutParams(-1, -2))
            addView(ui.line())
            addView(ui.row(6).apply {
                addView(queue, LinearLayout.LayoutParams(0, -2, 1f))
                addView(position, LinearLayout.LayoutParams(ui.dp(48), ui.dp(48)))
                addView(more, LinearLayout.LayoutParams(ui.dp(48), ui.dp(48)))
            })
            if (!ui.eInk) addView(timeline)
        }
        details = ui.playerContent(sleeve, controls)
        details.visibility = View.GONE
        empty.visibility = View.GONE
        body.addView(empty)
        body.addView(details, LinearLayout.LayoutParams(-1, -2))
        timeline.addView(bar, LinearLayout.LayoutParams(-1, ui.dp(48)))
        val times = ui.row()
        times.addView(elapsed, LinearLayout.LayoutParams(0, -2, 1f)); times.addView(length)
        timeline.addView(times)
    }

    private fun controlsRemote() = snapshot.optBoolean("session_playing") && snapshot.optBoolean("outputs_explicit") &&
        !snapshot.optBoolean("output_selected", true)

    private fun hasDevices() = (snapshot.optJSONArray("outputs")?.length() ?: 0) > 0

    private fun showDevices() {
        val outputs = snapshot.optJSONArray("outputs") ?: return
        val entries = (0 until outputs.length()).map { outputs.getJSONObject(it) }
        if (entries.isEmpty()) return
        val labels = entries.map {
            it.getString("name") + if (it.getString("id") == room.id) " (This device)" else ""
        }.toTypedArray()
        val checked = BooleanArray(entries.size) { entries[it].optBoolean("selected") }
        fun use(ids: List<String>) = action("outputs", JSONObject().put("devices", JSONArray(ids)))
        val dialog = AlertDialog.Builder(context).setTitle("Playback devices")
            .setMultiChoiceItems(labels, checked) { choice, index, selected ->
                checked[index] = selected
                (choice as AlertDialog).getButton(AlertDialog.BUTTON_POSITIVE).isEnabled = checked.any { it }
            }
            .setPositiveButton("Use selected devices") { _, _ ->
                use(entries.filterIndexed { index, _ -> checked[index] }.map { it.getString("id") })
            }
            .setNeutralButton("Listen here only") { _, _ -> use(listOf(room.id)) }
            .setNegativeButton("Cancel", null).show()
        dialog.getButton(AlertDialog.BUTTON_POSITIVE).isEnabled = checked.any { it }
    }

    private fun showQueue() {
        val upcoming = snapshot.optJSONArray("upcoming") ?: JSONArray()
        val version = snapshot.optLong("version")
        val radio = snapshot.optBoolean("radio")
        val entries = (0 until upcoming.length()).mapNotNull { index ->
            val item = upcoming.optJSONObject(index) ?: return@mapNotNull null
            Entry(item.optString("title"), item.optString("artist")) {
                val fields = if (radio) JSONObject().put("radio_ref", item.getString("id"))
                    else JSONObject().put("index", index).put("expected_version", version)
                action("jump", fields)
            }
        }
        ui.showQueue(activity, entries)
    }

    private fun TextView.updateLabel(value: String) {
        if (!TextUtils.equals(text, value)) text = value
    }

    private fun updatePosition() {
        val open = timeline.visibility == View.VISIBLE
        if (position.isSelected != open) {
            position.isSelected = open
            val label = if (open) "Hide position" else "Show position"
            position.contentDescription = label
            position.tooltipText = label
        }
        if (!open || dragging) return
        bar.max = duration.coerceAtLeast(1)
        bar.progress = seconds
        elapsed.updateLabel(clockTime(seconds))
        length.updateLabel(clockTime(duration))
    }

    fun render(snapshot: JSONObject) {
        this.snapshot = snapshot
        val track = snapshot.optJSONObject("current")
        current = track
        idleDevices.visibility = if (track == null && hasDevices()) View.VISIBLE else View.GONE
        val hasLyricsSource = track?.lyricRef() != null
        if (track != null && hasLyricsSource) lyricSheet?.update(track, snapshot.optInt("position"), !snapshot.optBoolean("radio_pending"))
        else lyricSheet?.close()
        empty.visibility = if (track == null) View.VISIBLE else View.GONE
        details.visibility = if (track == null) View.GONE else View.VISIBLE
        val upcoming = snapshot.optJSONArray("upcoming")
        val hasQueue = (upcoming?.length() ?: 0) > 0
        playQueue.visibility = if (hasQueue) View.VISIBLE else View.GONE
        emptyHint.updateLabel(if (hasQueue) "Queue ready to play." else "Choose a song to start playback.")
        if (track == null) {
            positionDialog?.dismiss()
            sleeve.clear()
            return
        }
        val nextTitle = upcoming?.optJSONObject(0)?.optString("title").orEmpty()
        val queueLabel = if (nextTitle.isBlank()) "Queue" else "Queue\n${nextTitle.lineSequence().first()}"
        queue.updateLabel(queueLabel)
        val ref = snapshot.optString("current_ref")
        sleeve.bind(track, ref, RoomPlayback.artwork(room.id, ref))
        sleeve.linkProfiles(profileAction(track, "artist"), profileAction(track, "album"))
        renderFavorite()
        duration = track.optDouble("duration", 0.0).toInt()
        seconds = snapshot.getDouble("position").toInt()
        position.visibility = if (duration > 0) View.VISIBLE else View.GONE
        if (duration <= 0) {
            positionDialog?.dismiss()
            timeline.visibility = View.GONE
        }
        updatePosition()
        playing = snapshot.getBoolean("playing")
        val pause = controlsRemote() || (playing && (RoomPlayback.following || snapshot.optBoolean("radio")))
        val label = when {
            snapshot.optBoolean("radio_external") -> "Play in YouTube"
            pause -> "Pause"
            playing && snapshot.has("output_selected") -> "Join audio"
            else -> "Play"
        }
        ui.playbackIcon(transport, label, pause)
        previous.isEnabled = !snapshot.optBoolean("radio_pending") && !snapshot.optBoolean("radio_live")
        next.isEnabled = previous.isEnabled
    }
}
