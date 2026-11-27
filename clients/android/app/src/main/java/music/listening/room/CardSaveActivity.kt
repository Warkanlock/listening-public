package music.listening.room

import android.app.Activity
import android.app.AlertDialog
import android.content.Intent
import android.net.Uri
import android.os.Bundle
import android.view.View
import android.widget.Button
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import java.io.File
import java.io.IOException
import java.util.concurrent.Executors

internal const val OPEN_CAMPING = "open_camping"

class CardSaveActivity : Activity() {
    private data class TripPreview(val roomId: String, val mixes: List<Pair<SavedPlaylist, List<SaveTrack>>>) {
        val songCount = mixes.sumOf { it.second.size }
        val summary get() = "$songCount songs across ${mixes.size} mixes"
    }

    private val ui by lazy { RoomUi(this) }
    private val camping get() = intent.getBooleanExtra(OPEN_CAMPING, false)
    private var operation: CardSaveOperation? = null
    private lateinit var status: TextView
    private lateinit var find: Button
    private lateinit var invitation: Button
    private lateinit var format: Button
    private lateinit var sizes: LinearLayout
    private lateinit var action: Button
    private lateinit var list: LinearLayout
    private var playlist: SavedPlaylist? = null
    private var tripSize: Int? = null
    private var trip: TripPreview? = null
    private var tree: Uri? = null
    private var busy = false

    override fun onCreate(state: Bundle?) {
        super.onCreate(state)
        ui.theme.applyWindow(this, ui.paper)
        val frame = ui.screen()
        frame.addView(ui.row(8).apply {
            addView(ui.iconButton("Back", Mark.BACK, outlined = false) { finish() },
                LinearLayout.LayoutParams(ui.dp(48), ui.dp(48)))
            addView(ui.heading(if (camping) "Camping mode" else "Save for offline", 23f))
        })
        val layout = ui.column(8)
        frame.addView(ScrollView(this).apply {
            if (ui.eInk) overScrollMode = View.OVER_SCROLL_NEVER
            addView(layout)
        },
            LinearLayout.LayoutParams(-1, 0, 1f))
        val instructions = if (camping) "Choose how many songs to take with you."
            else "Connect on home Wi-Fi to choose a playlist to save."
        status = ui.label(instructions, 15f, ui.secondary).apply {
            accessibilityLiveRegion = View.ACCESSIBILITY_LIVE_REGION_POLITE
        }
        layout.addView(status)
        find = ui.button("Connect over Wi-Fi", true) { findRoom() }.apply {
            visibility = if (camping) View.GONE else View.VISIBLE
        }
        invitation = ui.button("Wi-Fi address or invitation") { openPairing() }
        format = ui.button(formatLabel()) { MobileAudio.choose(this) { format.text = formatLabel() } }.apply {
            visibility = View.GONE
        }
        sizes = ui.row(6).apply {
            for (count in tripSizes) {
                addView(ui.button("$count songs") { makeTrip(count) }, LinearLayout.LayoutParams(0, -2, 1f))
            }
            visibility = if (camping) View.VISIBLE else View.GONE
        }
        action = ui.button("Save", true) {
            if (camping) prepareTrip() else playlist?.let(::prepare)
        }.apply { visibility = View.GONE }
        list = ui.column()
        layout.addView(sizes)
        if (camping) layout.addView(list)
        layout.addView(format)
        layout.addView(action)
        if (!camping) {
            layout.addView(list)
            layout.addView(find)
        }
        layout.addView(invitation)
        if (!camping) {
            layout.addView(ui.line())
            layout.addView(ui.button("Camping mode") {
                startActivity(Intent(this, CardSaveActivity::class.java).putExtra(OPEN_CAMPING, true))
            })
        }
        setContentView(frame)
        tree = getSharedPreferences("card", MODE_PRIVATE).getString("tree", null)?.let(Uri::parse)
    }

    private fun formatLabel() = "Mobile audio · " + MobileAudio.load(this).label

    private fun paired(task: CardSaveOperation) =
        Room.load(this, homeWifi = true, onCall = task::register) ?: throw PairingRequired()

    private fun openPairing() {
        val pairing = Intent(this, RoomActivity::class.java)
            .putExtra("home_wifi", true).putExtra("pair_only", true)
        if (camping) {
            startActivityForResult(pairing, CONNECT_FOR_TRIP)
            return
        }
        playlist = null
        action.visibility = View.GONE
        format.visibility = View.GONE
        list.removeAllViews()
        startActivity(pairing)
    }

    private fun findRoom() {
        playlist = null
        action.visibility = View.GONE
        format.visibility = View.GONE
        list.removeAllViews()
        status.text = "Looking for the room on Wi-Fi…"
        work({ task ->
            val room = Room.load(this, homeWifi = true, onCall = task::register)
            if (room == null) throw PairingRequired()
            room.requireCatalog()
            room.get("/api/overview").getJSONArray("playlists")
        }) { rows ->
            find.text = "Refresh playlists"
            val choices = (0 until rows.length()).map { RoomPlaylist(rows.getJSONObject(it)) }
            status.text = if (choices.isEmpty()) "No playlists in this room yet." else "Choose a playlist to save."
            format.visibility = if (choices.isEmpty()) View.GONE else View.VISIBLE
            val style = SongDetailsStyle(ui)
            for (choice in choices) {
                val detail = listOf("${choice.playable} ready", if (choice.linked) choice.syncLabel else "")
                    .filter(String::isNotBlank).joinToString(" · ")
                list.addView(style.link(choice.name, detail) {
                    if (!busy) {
                        if (choice.playable > 0) prepare(SavedPlaylist(choice.key, choice.name, choice.playable))
                        else status.text = "This playlist has no downloaded songs yet."
                    }
                })
            }
        }
    }

    private fun makeTrip(count: Int) {
        if (busy) return
        tripSize = count
        for ((index, size) in tripSizes.withIndex()) sizes.getChildAt(index).isSelected = size == count
        trip = null
        action.visibility = View.GONE
        format.visibility = View.GONE
        list.removeAllViews()
        status.text = "Finding ready songs…"
        work({ task ->
            val room = paired(task)
            val plan = room.get("/api/hiking?songs=$count")
            val mixes = plan.getJSONArray("mixes")
            val previews = (0 until mixes.length()).map { index ->
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
            TripPreview(room.id, previews)
        }) { preview ->
            val canSave = preview.songCount > 0
            trip = preview.takeIf { canSave }
            status.text = if (canSave) preview.summary else "No ready songs in this room."
            for ((mix, tracks) in preview.mixes) {
                list.addView(ui.row(10).apply {
                    minimumHeight = ui.dp(48)
                    val title = ui.label(mix.name.removePrefix("Camping - "))
                    val count = ui.label("${tracks.size} songs", 14f, ui.secondary)
                    addView(title, LinearLayout.LayoutParams(0, -2, 1f))
                    addView(count)
                })
                list.addView(ui.line())
            }
            action.text = "Save trip"
            format.visibility = if (canSave) View.VISIBLE else View.GONE
            action.visibility = if (canSave) View.VISIBLE else View.GONE
        }
    }

    private fun prepareTrip() {
        if (busy) return
        val selected = trip ?: return
        val card = writableFolder() ?: return
        val profile = MobileAudio.load(this)
        val total = selected.songCount
        status.text = "Saving 0 of $total…"
        work({ task ->
            val room = paired(task)
            check(room.id == selected.roomId) { "The room changed. Choose a trip size to make a new preview." }
            val saver = CardSaves(contentResolver, card, File(cacheDir, "card-transfers"), task)
            var previous = 0
            selected.mixes.forEach { (mix, tracks) ->
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
                Intent.FLAG_GRANT_PERSISTABLE_URI_PERMISSION), PICK_FOLDER)
        return null
    }

    private fun hasWriteAccess(uri: Uri) =
        contentResolver.persistedUriPermissions.any { it.uri == uri && it.isReadPermission && it.isWritePermission }

    private fun prepare(choice: SavedPlaylist) {
        if (busy) return
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
            action.text = "Check for playlist updates"
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
        action.isEnabled = false
        for (index in 0 until sizes.childCount) sizes.getChildAt(index).isEnabled = false
        worker.execute {
            val outcome = runCatching { task.check(); block(task) }
            runOnUiThread {
                if (isDestroyed || isFinishing) return@runOnUiThread
                busy = false
                operation = null
                find.isEnabled = true
                invitation.isEnabled = true
                format.isEnabled = true
                action.isEnabled = true
                for (index in 0 until sizes.childCount) sizes.getChildAt(index).isEnabled = true
                outcome.onSuccess(success).onFailure { error ->
                    status.text = if (task.cancelled) "Transfer paused."
                        else error.message ?: "Could not save the playlist. Check Wi-Fi and the card."
                    if (error is PairingRequired) {
                        find.text = "Connect over Wi-Fi"
                        AlertDialog.Builder(this).setTitle("Pair on home Wi-Fi")
                            .setMessage("Enter your paired room’s Wi-Fi address or use a fresh invitation from Devices settings.")
                            .setNegativeButton("Cancel", null)
                            .setPositiveButton("Connect") { _, _ -> openPairing() }.show()
                    } else if (playlist != null || trip != null) {
                        action.text = "Retry save"
                        action.visibility = View.VISIBLE
                    } else if (camping) {
                        status.text = "${status.text}\nChoose a trip size or change the Wi-Fi address below."
                    } else {
                        find.text = "Retry"
                    }
                }
            }
        }
    }

    @Deprecated("Uses the platform folder picker and connection form")
    override fun onActivityResult(request: Int, result: Int, data: Intent?) {
        super.onActivityResult(request, result, data)
        if (result != RESULT_OK) return
        if (request == CONNECT_FOR_TRIP) {
            val preview = trip
            val room = Room.load(this, homeWifi = true)
            if (preview != null && preview.roomId == room?.id) {
                if (action.visibility == View.VISIBLE) status.text = preview.summary
            } else {
                val count = tripSize
                if (count != null) makeTrip(count)
                else status.text = "Connected. Choose how many songs to take with you."
            }
            return
        }
        if (request != PICK_FOLDER) return
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
            if (camping) prepareTrip() else playlist?.let(::prepare)
        } catch (_: SecurityException) {
            status.text = "Select the folder again and allow write access."
        }
    }

    override fun onStop() {
        operation?.cancel()
        super.onStop()
    }

    private companion object {
        const val PICK_FOLDER = 1
        const val CONNECT_FOR_TRIP = 2
        val tripSizes = intArrayOf(30, 60, 90)
        val worker = Executors.newSingleThreadExecutor()
    }
}
