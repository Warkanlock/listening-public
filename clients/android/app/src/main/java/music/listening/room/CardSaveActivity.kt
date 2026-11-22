package music.listening.room

import android.app.Activity
import android.app.AlertDialog
import android.content.Intent
import android.net.Uri
import android.os.Bundle
import android.view.View
import android.widget.Button
import android.widget.LinearLayout
import android.widget.TextView
import java.io.File
import java.io.IOException
import java.util.concurrent.Executors

class CardSaveActivity : Activity() {
    private val ui by lazy { RoomUi(this) }
    private var operation: CardSaveOperation? = null
    private lateinit var status: TextView
    private lateinit var find: Button
    private lateinit var invitation: Button
    private lateinit var format: Button
    private lateinit var hiking: Button
    private lateinit var action: Button
    private lateinit var list: LinearLayout
    private var playlist: SavedPlaylist? = null
    private var trip: List<Pair<SavedPlaylist, List<SaveTrack>>> = emptyList()
    private var tree: Uri? = null
    private var busy = false

    override fun onCreate(state: Bundle?) {
        super.onCreate(state)
        val frame = ui.screen()
        frame.addView(ui.row(8).apply {
            addView(ui.iconButton("Back to device files", Mark.BACK) { finish() },
                LinearLayout.LayoutParams(ui.dp(48), ui.dp(48)))
            addView(ui.heading("Save for offline", 23f))
        })
        val layout = ui.column(8)
        frame.addView(android.widget.ScrollView(this).apply { addView(layout) },
            LinearLayout.LayoutParams(-1, 0, 1f))
        status = ui.label("Join your home Wi-Fi to copy a playlist directly to your card.", 15f, ui.secondary).apply {
            accessibilityLiveRegion = View.ACCESSIBILITY_LIVE_REGION_POLITE
        }
        layout.addView(status)
        find = ui.button("Connect over Wi-Fi", true) { findRoom() }
        layout.addView(find)
        invitation = ui.button("Wi-Fi address or invitation") { openPairing() }
        layout.addView(invitation)
        format = ui.button(formatLabel()) { MobileAudio.choose(this) { format.text = formatLabel() } }
        layout.addView(format)
        hiking = ui.button("Camping mode") { chooseTripLength() }
        layout.addView(hiking)
        action = ui.button("Save playlist", true) {
            if (trip.isEmpty()) playlist?.let(::prepare) else prepareTrip()
        }.apply { visibility = View.GONE }
        layout.addView(action)
        list = ui.column(8)
        layout.addView(list)
        setContentView(frame)
        tree = getSharedPreferences("card", MODE_PRIVATE).getString("tree", null)?.let(Uri::parse)
    }

    private fun formatLabel() = "Mobile audio · " + MobileAudio.load(this).label

    private fun paired(task: CardSaveOperation) =
        Room.load(this, homeWifi = true, onCall = task::register) ?: throw PairingRequired()

    private fun openPairing() {
        trip = emptyList()
        playlist = null
        action.visibility = View.GONE
        list.removeAllViews()
        startActivity(Intent(this, RoomActivity::class.java)
            .putExtra("home_wifi", true).putExtra("pair_only", true))
    }

    private fun findRoom() {
        trip = emptyList()
        playlist = null
        status.text = "Looking for the room on Wi-Fi…"
        work({ task ->
            val room = Room.load(this, homeWifi = true, onCall = task::register)
            if (room == null) throw PairingRequired()
            room.requireCatalog()
            room.get("/api/overview").getJSONArray("playlists")
        }) { rows ->
            find.text = "Connect over Wi-Fi"
            action.visibility = View.GONE
            list.removeAllViews()
            val choices = (0 until rows.length()).map { RoomPlaylist(rows.getJSONObject(it)) }
            status.text = if (choices.isEmpty()) "No playlists in this room yet." else "Choose a playlist to save."
            for (choice in choices) list.addView(ui.button("${choice.name} · ${choice.playable} ready") {
                if (choice.playable > 0) prepare(SavedPlaylist(choice.key, choice.name, choice.playable))
                else status.text = "This playlist has no downloaded songs yet."
            }.apply {
                if (choice.linked) {
                    append("\n" + choice.syncLabel)
                    setCompoundDrawablesRelative(ui.mark(Mark.MAGNET, 17, ui.accent), null, null, null)
                    compoundDrawablePadding = ui.dp(6)
                }
            })
        }
    }

    private fun chooseTripLength() {
        val sizes = intArrayOf(30, 60, 90)
        AlertDialog.Builder(this).setTitle("Songs for your trip")
            .setItems(sizes.map { "$it songs" }.toTypedArray()) { _, index -> makeTrip(sizes[index]) }
            .setNegativeButton("Cancel", null).show()
    }

    private fun makeTrip(count: Int) {
        playlist = null
        trip = emptyList()
        action.visibility = View.GONE
        list.removeAllViews()
        status.text = "Finding ready songs…"
        work({ task ->
            val plan = paired(task).get("/api/hiking?songs=$count")
            val mixes = plan.getJSONArray("mixes")
            (0 until mixes.length()).map { index ->
                val mix = mixes.getJSONObject(index)
                val rows = mix.getJSONArray("tracks")
                val tracks = (0 until rows.length()).map { trackIndex ->
                    val row = rows.getJSONObject(trackIndex)
                    val label = listOf(row.optString("artist"), row.getString("title"))
                        .filter(String::isNotBlank).joinToString(" - ")
                    SaveTrack(row.getString("audio"), label, row.optDouble("duration", 0.0))
                }
                SavedPlaylist("hike-${plan.getString("id")}-$index", "Camping - ${mix.getString("name")}", tracks.size) to tracks
            }
        }) { mixes ->
            trip = mixes
            val total = mixes.sumOf { it.second.size }
            status.text = if (total == 0) "No ready songs in this room." else "$total songs across ${mixes.size} mixes"
            mixes.forEach { (mix, tracks) -> list.addView(ui.label("${mix.name} · ${tracks.size} songs", 16f)) }
            action.text = "Save trip"
            action.visibility = if (total > 0) View.VISIBLE else View.GONE
        }
    }

    private fun prepareTrip() {
        if (busy || trip.isEmpty()) return
        val card = writableFolder() ?: return
        val selected = trip
        val profile = MobileAudio.load(this)
        val total = selected.sumOf { it.second.size }
        status.text = "Saving 0 of $total…"
        work({ task ->
            val room = paired(task)
            val saver = CardSaves(contentResolver, card, File(cacheDir, "card-transfers"), task)
            var previous = 0
            selected.forEach { (mix, tracks) ->
                saver.save(room, mix, tracks, profile) { completed ->
                    val done = previous + completed
                    runOnUiThread { if (!isDestroyed && !isFinishing) status.text = "Saved $done of $total" }
                }
                previous += tracks.size
            }
        }) {
            status.text = "Trip ready in Device files → Saved playlists."
            action.visibility = View.GONE
        }
    }

    private fun writableFolder(): Uri? {
        val selected = tree
        if (selected != null && hasWriteAccess(selected)) return selected
        status.text = "Choose Music on your microSD card or phone storage and allow write access."
        startActivityForResult(Intent(Intent.ACTION_OPEN_DOCUMENT_TREE)
            .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_GRANT_WRITE_URI_PERMISSION or
                Intent.FLAG_GRANT_PERSISTABLE_URI_PERMISSION), 1)
        return null
    }

    private fun hasWriteAccess(uri: Uri) =
        contentResolver.persistedUriPermissions.any { it.uri == uri && it.isReadPermission && it.isWritePermission }

    private fun prepare(choice: SavedPlaylist) {
        if (busy) return
        trip = emptyList()
        playlist = choice
        action.visibility = View.GONE
        val card = writableFolder() ?: return
        val profile = MobileAudio.load(this)
        status.text = "Checking ${choice.name}…"
        work({ task ->
            CardSaves.tracks(paired(task), choice)
        }) { tracks ->
            if (tracks.isEmpty()) {
                status.text = "This playlist has no downloaded songs yet."
                return@work
            }
            val unique = tracks.distinctBy { it.url }
            val seconds = unique.sumOf { it.seconds.coerceAtLeast(0.0) }
            val unknown = unique.count { it.seconds <= 0 }
            val megabytes = kotlin.math.ceil(seconds * profile.kbps / 8 / 1000).toInt()
            val space = when {
                profile.kbps == 0 -> "Original file sizes vary."
                seconds == 0.0 -> "Size unavailable until the room prepares the audio."
                unknown > 0 -> "About $megabytes MB for tracks with known durations, plus $unknown without an estimate."
                else -> "About $megabytes MB before reusing saved tracks."
            }
            val count = if (tracks.size == 1) "1 song" else "${tracks.size} songs"
            status.text = "Ready to save $count."
            AlertDialog.Builder(this).setTitle(choice.name)
                .setMessage("$count · ${profile.label}\n$space")
                .setNegativeButton("Cancel", null)
                .setPositiveButton("Save to card") { _, _ -> save(card, choice, tracks, profile) }.show()
        }
    }

    private fun save(card: Uri, choice: SavedPlaylist, tracks: List<SaveTrack>, profile: MobileAudio) {
        status.text = "Saving 0 of ${tracks.size}…"
        work({ task ->
            CardSaves(contentResolver, card, File(cacheDir, "card-transfers"), task)
                .save(paired(task), choice, tracks, profile) { done ->
                    runOnUiThread { if (!isDestroyed && !isFinishing) status.text = "Saved $done of ${tracks.size}" }
                }
        }) {
            status.text = "${choice.name} is ready in Device files → Saved playlists."
            action.text = "Check for updates"
            action.visibility = View.VISIBLE
        }
    }

    private fun <T> work(block: (CardSaveOperation) -> T, success: (T) -> Unit) {
        if (busy) return
        busy = true
        val task = CardSaveOperation()
        operation = task
        find.isEnabled = false
        invitation.isEnabled = false
        format.isEnabled = false
        hiking.isEnabled = false
        worker.execute {
            val outcome = runCatching { task.check(); block(task) }
            runOnUiThread {
                if (isDestroyed || isFinishing) return@runOnUiThread
                busy = false
                operation = null
                find.isEnabled = true
                invitation.isEnabled = true
                format.isEnabled = true
                hiking.isEnabled = true
                outcome.onSuccess(success).onFailure { error ->
                    status.text = if (task.cancelled) "Save paused. Tap Retry to continue."
                        else error.message ?: "Could not save the playlist. Check Wi-Fi and the card."
                    if (error is PairingRequired) {
                        find.text = "Connect over Wi-Fi"
                        AlertDialog.Builder(this).setTitle("Pair on home Wi-Fi")
                            .setMessage("Enter your paired room’s Wi-Fi address or use a fresh invitation from Devices settings.")
                            .setNegativeButton("Cancel", null)
                            .setPositiveButton("Connect") { _, _ -> openPairing() }.show()
                    } else if (playlist != null || trip.isNotEmpty()) {
                        action.text = "Retry save"
                        action.visibility = View.VISIBLE
                    } else {
                        find.text = "Retry"
                    }
                }
            }
        }
    }

    @Deprecated("Uses the platform folder picker")
    override fun onActivityResult(request: Int, result: Int, data: Intent?) {
        super.onActivityResult(request, result, data)
        if (request != 1 || result != RESULT_OK) return
        val selected = data?.data ?: return
        if (selected.authority != "com.android.externalstorage.documents") {
            status.text = "Choose a Music folder in local storage or on your SD card."
            return
        }
        try {
            contentResolver.takePersistableUriPermission(selected,
                Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_GRANT_WRITE_URI_PERMISSION)
            tree = selected
            getSharedPreferences("card", MODE_PRIVATE).edit().putString("tree", selected.toString()).apply()
            if (trip.isEmpty()) playlist?.let(::prepare) else prepareTrip()
        } catch (_: SecurityException) {
            status.text = "Select the folder again and allow write access."
        }
    }

    override fun onStop() {
        operation?.cancel()
        super.onStop()
    }

    private companion object {
        val worker = Executors.newSingleThreadExecutor()
    }
}
