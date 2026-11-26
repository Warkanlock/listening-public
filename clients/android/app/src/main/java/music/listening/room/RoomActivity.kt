package music.listening.room

import android.app.Activity
import android.app.AlertDialog
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.graphics.Color
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.os.SystemClock
import android.text.Editable
import android.text.InputType
import android.text.TextWatcher
import android.view.KeyEvent
import android.view.inputmethod.InputMethodManager
import android.graphics.Typeface
import android.view.View
import android.view.inputmethod.EditorInfo
import android.widget.*
import java.text.NumberFormat
import org.json.JSONArray
import org.json.JSONObject
import java.util.concurrent.Executors

class RoomActivity : Activity() {
    private enum class Screen { LIBRARY, EXPLORE, ACTIVITIES, DISCOVER, WEEKLY, CATALOGS, RADIO, PLAYER, INSIGHTS }
    private lateinit var ui: RoomUi
    private val io = Executors.newSingleThreadExecutor()
    private val catalogIo = Executors.newSingleThreadExecutor()
    private val handler = Handler(Looper.getMainLooper())
    private var active = false
    private var generation = 0
    private var room: Room? = null
    private var screen = Screen.LIBRARY
    private var offset = 0
    private var query = ""
    private var playlist = "all"
    private var playlistDetail: RoomPlaylist? = null
    private var playlistSearch: EditText? = null
    private var playlistRefreshGeneration: Int? = null
    private var genre = ""
    private var mood = ""
    private var filterName = "all"
    private var selection = ""
    private lateinit var content: LinearLayout
    private lateinit var retry: Button
    private lateinit var status: TextView
    private var presenceError = false
    private var playerPanel: PlayerPanel? = null
    private var insightsPanel: PlaybackInsights? = null
    private var insightsRequestGeneration: Int? = null
    private var playerShortcut: Button? = null
    private var downloadsShelf: LinearLayout? = null
    private var downloadRoom: String? = null
    private var recentDownloads = "[]"
    private var downloadActivityLoading = false
    private var autoJoinAttempt: Pair<String, Long>? = null
    private var authorizedExternalRadio: Pair<String, RadioHistory>? = null
    private fun phone() = room?.let { PhoneLibrary.get(this, it.replicaId) }
    private var syncRoom: String? = null
    private var syncAfter = 0L
    private fun sync(target: Room, library: PhoneLibrary) {
        syncRoom = target.id
        syncAfter = SystemClock.elapsedRealtime() + 5 * 60_000
        ReplicaSync.start(target, library) {
            runOnUiThread {
                if (active && !isDestroyed && room?.id == target.id && screen == Screen.LIBRARY && library.local) show()
            }
        }
    }
    private val presence = object : Runnable {
        override fun run() {
            val target = room ?: return
            if (!active) return
            // The playback service keeps its own heartbeat with the screen off.
            val library = phone()!!
            if (library.enabled && !ReplicaSync.busy &&
                (syncRoom != target.id || SystemClock.elapsedRealtime() >= syncAfter))
                sync(target, library)
            if (!library.local && !RoomPlayback.following) load({ target.heartbeat() }, fromPresence = true) {}
            if (!library.local) {
                refreshPlaylist(target)
                verifySavedRadio(target)
                refreshDownloadActivity(target)
            }
            handler.postDelayed(this, 20000)
        }
    }
    private val poll = Runnable { if (active && room != null) refreshPlayer() }
    private var weeklyScroll: ScrollView? = null
    private val refreshDiscover = object : Runnable {
        override fun run() {
            if (!active || screen !in setOf(Screen.DISCOVER, Screen.WEEKLY)) return
            if (screen == Screen.WEEKLY && !hasWindowFocus()) handler.postDelayed(this, 8_000L)
            else show()
        }
    }
    private val errors = object : BroadcastReceiver() {
        override fun onReceive(context: Context, intent: Intent) {
            if (Room.load(this@RoomActivity) == null) {
                room = null
                show()
            }
            (intent.getStringExtra("error") ?: intent.getStringExtra("notice"))?.let { message(it) }
        }
    }

    override fun onCreate(saved: Bundle?) {
        super.onCreate(saved)
        ui = RoomUi(this)
        room = if (pairOnly) null
            else Room.load(this, homeWifi = homeWifi)
        window.navigationBarColor = Color.BLACK
        ui.theme.applyWindow(this, ui.paper)
        show()
    }

    override fun onStart() {
        super.onStart()
        active = true
        if (Build.VERSION.SDK_INT >= 33) {
            registerReceiver(errors, IntentFilter(RoomPlayback.UPDATE), RECEIVER_NOT_EXPORTED)
        } else {
            @Suppress("DEPRECATION")
            registerReceiver(errors, IntentFilter(RoomPlayback.UPDATE))
        }
        if (pairOnly) return
        handler.post(presence)
        if (room != null && retry.visibility == View.VISIBLE) show()
        else if (room != null && screen in setOf(Screen.DISCOVER, Screen.WEEKLY, Screen.ACTIVITIES, Screen.RADIO)) show()
        else if (room != null) refreshPlayer()
    }

    override fun onStop() {
        active = false
        authorizedExternalRadio = null
        handler.removeCallbacks(presence)
        handler.removeCallbacks(poll)
        handler.removeCallbacks(refreshDiscover)
        unregisterReceiver(errors)
        super.onStop()
    }

    override fun onDestroy() {
        generation++
        handler.removeCallbacksAndMessages(null)
        io.shutdownNow()
        catalogIo.shutdownNow()
        super.onDestroy()
    }

    @Deprecated("Uses the platform activity history")
    override fun onBackPressed() {
        if (screen == Screen.WEEKLY) {
            screen = Screen.DISCOVER
            show()
        } else if (screen in setOf(Screen.CATALOGS, Screen.DISCOVER, Screen.ACTIVITIES) || screen == Screen.LIBRARY && playlistDetail != null) {
            resetLibrary()
            screen = Screen.EXPLORE
            show()
        } else super.onBackPressed()
    }

    private fun LinearLayout.part(view: View) =
        addView(view, LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f))

    private fun message(text: String = "") {
        status.text = text
        status.visibility = if (text.isEmpty()) View.GONE else View.VISIBLE
    }

    private fun chooseAppearance(onChanged: () -> Unit = {}) {
        ui.choices(this, "Appearance", RoomTheme.entries.map { theme ->
            Entry(theme.label, mark = if (theme == ui.theme) Mark.CHECK else null, open = {
                if (theme != ui.theme) {
                    theme.save(this)
                    ui = RoomUi(this)
                    ui.theme.applyWindow(this, ui.paper)
                    onChanged()
                    show()
                }
            })
        })
    }

    private fun connection(target: Room) {
        val info = ui.column(8).apply {
            setPadding(ui.dp(24), ui.dp(18), ui.dp(24), ui.dp(8))
        }
        lateinit var dialog: AlertDialog
        fun section(title: String) {
            if (info.childCount > 0) info.addView(ui.line())
            info.addView(ui.heading(title, 19f))
        }
        section("Appearance")
        if (ui.eInk) info.addView(ui.label("Paper", 15f, ui.secondary))
        else info.addView(ui.button("Theme · " + ui.theme.label) { chooseAppearance { dialog.dismiss() } })
        section("Playback")
        lateinit var audio: Button
        audio = ui.button("Mobile audio · " + MobileAudio.load(this).label) {
            MobileAudio.choose(this) { audio.text = "Mobile audio · " + it.label }
        }
        info.addView(audio)
        fun bufferLabel(count: Int) = "Buffer ahead · $count " + if (count == 1) "song" else "songs"
        lateinit var buffer: Button
        buffer = ui.button(bufferLabel(QueueAudioBuffer.ahead(this))) {
            AlertDialog.Builder(this).setTitle("Buffer ahead")
                .setSingleChoiceItems(arrayOf("Next song", "Next 2 songs", "Next 3 songs"),
                    QueueAudioBuffer.ahead(this) - 1) { picker, index ->
                    QueueAudioBuffer.setAhead(this, index + 1)
                    buffer.text = bufferLabel(index + 1)
                    picker.dismiss()
                }.setNegativeButton("Cancel", null).show()
        }
        info.addView(buffer)
        section("Extensions")
        info.addView(ui.button("View room extensions") { showRoomExtensions(target) })
        section("Offline library")
        val library = PhoneLibrary.get(this, target.replicaId)
        val summary = ui.label("", 15f, ui.secondary)
        fun mode(local: Boolean) {
            stopService(Intent(this, RoomPlayback::class.java))
            library.local = local
            resetLibrary()
            dialog.dismiss()
            show()
        }
        fun button(label: String, action: () -> Unit) = info.addView(ui.button(label, false, action))
        if (library.enabled) {
            info.addView(summary)
            val syncButton = ui.button("Sync now") { sync(target, library) }
            info.addView(syncButton)
            val importButton = ui.button("Add song from phone") {
                dialog.dismiss()
                startActivityForResult(Intent(Intent.ACTION_OPEN_DOCUMENT).setType("audio/*")
                    .addCategory(Intent.CATEGORY_OPENABLE), 81)
            }
            info.addView(importButton)
            val update = object : Runnable {
                override fun run() {
                    if (!dialog.isShowing) return
                    val (count, bytes) = library.stats()
                    val status = if (ReplicaSync.busy) ReplicaSync.progress else library.options.getString("status", "").orEmpty()
                    summary.text = "$count saved · ${android.text.format.Formatter.formatShortFileSize(this@RoomActivity, bytes)}\n$status"
                    syncButton.isEnabled = !ReplicaSync.busy
                    importButton.isEnabled = !ReplicaSync.importing && library.options.contains("collection")
                    if (!library.options.contains("collection")) summary.append("\nConnect once to enable phone imports.")
                    handler.postDelayed(this, 1000)
                }
            }
            info.post(update)
            button(if (library.local) "Listen with the room" else "Listen on this phone") { mode(!library.local) }
            button("Turn off offline sync") { library.enabled = false; mode(false) }
        } else {
            button("Keep an offline library") {
                AlertDialog.Builder(this).setTitle("Copy this room to your phone?")
                    .setMessage("Copies the catalog and original audio into phone storage. Songs you add here sync back to the room. Allow offline sync in the server’s Devices settings first.")
                    .setNegativeButton("Cancel", null).setPositiveButton("Enable and sync") { _, _ ->
                        library.enabled = true
                        mode(true)
                        sync(target, library)
                    }.show()
            }
        }
        section("Server")
        info.addView(ui.label(target.origin).apply { setTextIsSelectable(true) })
        info.addView(ui.label(target.network.label, 15f, ui.secondary))
        if (library.options.contains("enabled")) button("Restore room access") {
            if (ReplicaSync.busy || ReplicaSync.importing) {
                Toast.makeText(this, "Wait for the current transfer to finish.", Toast.LENGTH_SHORT).show()
            } else {
                dialog.dismiss()
                restoreAccess(library)
            }
        }
        button("Disconnect") {
            if (ReplicaSync.busy || ReplicaSync.importing) {
                Toast.makeText(this, "Turn off sync and wait for the current transfer to finish.", Toast.LENGTH_LONG).show()
                return@button
            }
            fun disconnect() {
                dialog.dismiss()
                stopService(Intent(this, RoomPlayback::class.java))
                Room.forget(this)
                PhoneLibrary.remove(this, target.replicaId)
                room = null
                resetLibrary()
                show()
            }
            if (library.options.contains("collection") || library.options.contains("enabled")) {
                AlertDialog.Builder(this).setTitle("Remove this phone’s room copy?")
                    .setMessage("Disconnecting erases the offline library, including imports still waiting to sync. Files you selected from the phone’s storage stay there.")
                    .setNegativeButton("Keep library", null).setPositiveButton("Remove and disconnect") { _, _ -> disconnect() }.show()
            } else disconnect()
        }
        dialog = SongDetailsStyle(ui).dialog(this, "Settings", ScrollView(this).apply { addView(info) },
            closeLabel = "Close settings")
        dialog.show()
    }

    private fun restoreAccess(library: PhoneLibrary) {
        val pending = Room.load(this, recovering = true)
        val link = ui.field("New pairing link").apply {
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_PASSWORD
        }
        val form = AlertDialog.Builder(this).setTitle("Restore room access")
            .setMessage(if (pending == null) "Paste an invitation from the same room to keep your offline songs."
                else "Allow offline sync for the new pairing in the server’s Devices settings. Leave the link blank to finish pairing.")
            .setView(link).setNegativeButton("Cancel", null)
            .setPositiveButton(if (pending == null) "Pair again" else "Continue") { _, _ ->
                val value = link.text.toString().trim()
                if (value.isNotEmpty() || pending == null) {
                    load({ Room.pair(this, value, library) }) { restoreAccess(library) }
                } else {
                    load({ Room.finishRecovery(this, library) }) { replacement ->
                        stopService(Intent(this, RoomPlayback::class.java))
                        room = replacement
                        resetLibrary()
                        show()
                        if (library.enabled) sync(replacement, library)
                    }
                }
            }
        form.show()
    }

    @Deprecated("Uses the platform document picker")
    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        super.onActivityResult(requestCode, resultCode, data)
        if (requestCode != 81 || resultCode != RESULT_OK) return
        val target = room ?: return
        val library = phone() ?: return
        val uri = data?.data ?: return
        if (library.enabled) {
            ReplicaSync.importSong(target, library, uri) { text ->
                runOnUiThread {
                    if (!isDestroyed && room?.id == target.id) { show(); message(text) }
                }
            }
        }
    }

    private fun show(listPosition: Pair<Int, Int>? = null) {
        generation++
        if (screen != Screen.WEEKLY) weeklyScroll = null
        playlistSearch = null
        handler.removeCallbacks(poll)
        handler.removeCallbacks(refreshDiscover)
        playerPanel = null
        insightsPanel = null
        playerShortcut = null
        downloadsShelf = null
        val outer = ui.screen().apply { isFocusableInTouchMode = true }
        setContentView(outer)
        val target = room
        val weekly = target != null && screen == Screen.WEEKLY
        val heading = when {
            target == null -> "Connect to a room"
            screen == Screen.LIBRARY -> selection.ifBlank { if (phone()?.local == true) "On this phone" else "Songs" }
            screen == Screen.EXPLORE -> "Explore"
            screen == Screen.ACTIVITIES -> "Activities"
            screen == Screen.DISCOVER -> "Deep Dive"
            screen == Screen.WEEKLY -> "Weekly Discovery"
            screen == Screen.CATALOGS -> "Browse catalogs"
            screen == Screen.RADIO -> "Radio"
            screen == Screen.INSIGHTS -> "Insights"
            screen == Screen.PLAYER -> "Now playing"
            else -> "Listening Room"
        }
        val files = ui.button("Device files") { startActivity(Intent(this, CardActivity::class.java)) }
        outer.addView(ui.row(6).apply {
            if (weekly) addView(ui.iconButton("Back to Deep Dive", Mark.BACK) { onBackPressed() })
            else if (screen == Screen.ACTIVITIES) addView(ui.iconButton("Back to Explore", Mark.BACK) { onBackPressed() })
            else addView(ImageView(context).apply {
                setImageDrawable(ui.mark(Mark.ROOM, 23, ui.accent))
                importantForAccessibility = View.IMPORTANT_FOR_ACCESSIBILITY_NO
            }, LinearLayout.LayoutParams(ui.dp(23), ui.dp(28)))
            part(ui.heading(heading, if (target == null) 26f else 23f).apply {
                maxLines = 2
                ellipsize = android.text.TextUtils.TruncateAt.END
            })
            if (weekly) addView(ui.iconButton("Device files", Mark.FOLDER) {
                startActivity(Intent(this@RoomActivity, CardActivity::class.java))
            })
            else if (target != null && !(ui.eInk && screen == Screen.PLAYER)) addView(files)
            if (target != null) addView(ui.iconButton("Settings", Mark.SETTINGS) { connection(target) },
                LinearLayout.LayoutParams(ui.dp(48), ui.dp(48)))
            else if (!ui.eInk) addView(ui.iconButton("Appearance", Mark.SETTINGS) { chooseAppearance() },
                LinearLayout.LayoutParams(ui.dp(48), ui.dp(48)))
        })
        status = ui.label("", 15f, ui.secondary).apply {
            accessibilityLiveRegion = View.ACCESSIBILITY_LIVE_REGION_POLITE
        }
        outer.addView(status)
        presenceError = false
        retry = ui.button("Retry") { show() }.apply { visibility = View.GONE }
        if (weekly) outer.addView(retry, LinearLayout.LayoutParams(-2, -2))
        else outer.addView(retry)
        message()
        content = ui.column()
        outer.addView(content, LinearLayout.LayoutParams(-1, 0, 1f))
        if (target == null) {
            if (!pairOnly) outer.addView(files)
            connect()
            return
        }
        if (screen != Screen.PLAYER) {
            playerShortcut = ui.button("") { screen = Screen.PLAYER; show() }.apply {
                gravity = android.view.Gravity.CENTER_VERTICAL
                setCompoundDrawablesRelative(ui.mark(Mark.PLAYING, 18), null, ui.mark(Mark.BROWSE, 16), null)
                compoundDrawablePadding = ui.dp(10)
                isSingleLine = true
                ellipsize = android.text.TextUtils.TruncateAt.END
                visibility = View.GONE
            }
            outer.addView(playerShortcut, LinearLayout.LayoutParams(-1, -2))
        }
        val nav = ui.row(4)
        for ((title, destination) in listOf("Library" to Screen.LIBRARY, "Explore" to Screen.EXPLORE,
            "Radio" to Screen.RADIO, "Playing" to Screen.PLAYER, "Insights" to Screen.INSIGHTS)) {
            if (phone()?.local == true && destination in listOf(Screen.EXPLORE, Screen.RADIO)) continue
            val selected = destination == screen || (screen in setOf(Screen.ACTIVITIES, Screen.DISCOVER, Screen.WEEKLY, Screen.CATALOGS) && destination == Screen.EXPLORE)
            val glyph = when (destination) {
                Screen.LIBRARY -> Mark.LIBRARY
                Screen.EXPLORE, Screen.ACTIVITIES, Screen.DISCOVER, Screen.WEEKLY, Screen.CATALOGS -> Mark.EXPLORE
                Screen.RADIO -> Mark.RADIO
                Screen.PLAYER -> Mark.PLAYING
                Screen.INSIGHTS -> Mark.INSIGHTS
            }
            nav.part(ui.tab(title, glyph, selected) {
                if (destination == Screen.LIBRARY) resetLibrary()
                screen = destination
                show()
            }.apply { isSelected = selected })
        }
        outer.addView(nav)
        when (screen) {
            Screen.LIBRARY -> library(listPosition)
            Screen.EXPLORE -> explore()
            Screen.ACTIVITIES -> activityMixes(target)
            Screen.CATALOGS -> load({ target.get("/api/discovery/sources") }) { sources ->
                content.addView(CatalogBrowser(this, ui, sources, { query, onCall, done ->
                    load({ target.searchCatalog(query, onCall) }, failed = { done(Result.failure(it)) }, executor = catalogIo, reportErrors = false) {
                        done(Result.success(it))
                    }
                }, { pickPlaylist(target, it) }, { item, done ->
                    load({ target.importCatalogSong(item) }, failed = { done(Result.failure(it)) }, executor = catalogIo, reportErrors = false) {
                        done(Result.success(it))
                    }
                }, { id -> requestCopy(target, id) }))
            }
            Screen.DISCOVER -> deepDive(target)
            Screen.WEEKLY -> weeklyDiscovery(target)
            Screen.RADIO -> radio(target)
            Screen.INSIGHTS -> {
                val panel = PlaybackInsights(this, ui)
                insightsPanel = panel
                content.addView(panel)
            }
            Screen.PLAYER -> {
                playerPanel = PlayerPanel(this, ui, target, ::send,
                    addToPlaylist = if (phone()?.local == true) null else { item -> pickPlaylist(target, SongDraft.from(item, catalogTrack = true)) }) {
                    resetLibrary(); show()
                }
                content.addView(playerPanel, LinearLayout.LayoutParams(-1, if (ui.eInk) -1 else -2))
            }
        }
        if (active) refreshPlayer()
    }

    private fun requestCopy(target: Room, id: Int) {
        load({ target.requestCatalogCopy(id) }) { request ->
            if (request.optString("status") in CHANGEABLE_FORMAT) chooseDiscoveryFormat(request, target) { updated ->
                message(updated.copyStatusLabel())
            } else message(request.optString("message").ifBlank { "Request saved." })
        }
    }

    private fun requestMp3(target: Room, retry: JSONObject) {
        load({ target.confirmFormat(retry, "mp3") }) {
            message("MP3 search requested.")
            show()
        }
    }

    private fun resetLibrary() {
        screen = Screen.LIBRARY
        playlistDetail = null
        query = ""; genre = ""; mood = ""; playlist = "all"; filterName = "all"; selection = ""; offset = 0
    }

    private fun refreshDownloadActivity(target: Room) {
        if (downloadActivityLoading) return
        downloadActivityLoading = true
        io.execute {
            val result = runCatching { target.get("/api/requests?activity=1") }.getOrNull()
            runOnUiThread {
                downloadActivityLoading = false
                if (!active || isDestroyed || room?.id != target.id || result == null) return@runOnUiThread
                val outcomes = result.optJSONArray("outcomes") ?: return@runOnUiThread
                val rows = (0 until outcomes.length()).map(outcomes::getJSONObject)
                val preferences = getSharedPreferences("download_activity", MODE_PRIVATE)
                val previous = preferences.getStringSet(target.id, null)
                val current = rows.map { it.getString("key") }
                if (previous != null) {
                    val fresh = rows.filter { it.getString("key") !in previous }
                    val latest = fresh.firstOrNull()
                    val notice = when {
                        fresh.size > 1 -> {
                            val ready = fresh.count { it.optString("status") == "downloaded" }
                            val missing = fresh.count { it.optString("status") == "not_found" }
                            val review = fresh.size - ready - missing
                            listOfNotNull("$ready ready to play".takeIf { ready > 0 },
                                "$missing no copy found".takeIf { missing > 0 },
                                "$review need review".takeIf { review > 0 }).joinToString(" · ")
                        }
                        latest == null -> null
                        latest.optString("status") == "downloaded" -> "${latest.optString("title")} · Ready to play"
                        latest.optString("status") == "not_found" -> "${latest.optString("title")} · No copy found"
                        else -> "${latest.optString("title")} · Download needs review"
                    }
                    notice?.let { Toast.makeText(this, it, Toast.LENGTH_LONG).show() }
                }
                val remembered = (current + previous.orEmpty()).take(96).toSet()
                if (remembered != previous) preferences.edit().putStringSet(target.id, remembered).apply()
                val downloads = result.optJSONArray("downloads")?.toString() ?: "[]"
                if (downloadRoom != target.id || recentDownloads != downloads) {
                    downloadRoom = target.id
                    recentDownloads = downloads
                    renderRecentDownloads(target)
                }
            }
        }
    }

    private fun renderRecentDownloads(target: Room) {
        val shelf = downloadsShelf ?: return
        val tracks = JSONArray(if (downloadRoom == target.id) recentDownloads else "[]")
        shelf.removeAllViews()
        shelf.visibility = if (tracks.length() > 0) View.VISIBLE else View.GONE
        if (tracks.length() == 0) return
        shelf.addView(ui.heading("Recent downloads", 17f))
        val cards = ui.row(8)
        for (index in 0 until tracks.length()) {
            val track = tracks.getJSONObject(index)
            val title = track.getString("title")
            cards.addView(ui.row(6).apply {
                background = ui.surface(ui.card)
                setPadding(ui.dp(8), ui.dp(4), ui.dp(4), ui.dp(4))
                part(ui.column().apply {
                    addView(ui.heading(title, 15f).apply { maxLines = 2 })
                    addView(ui.label(track.optString("artist"), 13f, ui.secondary).apply { maxLines = 1 })
                })
                addView(ui.iconButton("Play $title", Mark.PLAY) {
                    val catalogId = track.optInt("track_index")
                    if (catalogId > 0) send("play", JSONObject().put("track_id", catalogId).put("playlist", "all"))
                    else send("play_request", JSONObject().put("request_id", track.getInt("id")))
                    screen = Screen.PLAYER
                    show()
                })
            }, LinearLayout.LayoutParams(ui.dp(202), -2))
        }
        shelf.addView(HorizontalScrollView(this).apply { addView(cards) })
    }

    private fun <T> load(job: () -> T, finished: () -> Unit = {},
                         failed: (Throwable) -> Unit = {}, fromPresence: Boolean = false,
                         reportErrors: Boolean = true, executor: java.util.concurrent.Executor = io,
                         then: (T) -> Unit) {
        val myGeneration = generation
        executor.execute {
            val value = runCatching(job)
            runOnUiThread {
                if (myGeneration != generation || isDestroyed) return@runOnUiThread
                finished()
                value.onSuccess { response ->
                    if (fromPresence && presenceError) {
                        retry.visibility = View.GONE
                        presenceError = false
                        message()
                    }
                    then(response)
                }.onFailure { error ->
                    if (!reportErrors && error !is PairingRequired) {
                        failed(error)
                        return@onFailure
                    }
                    presenceError = fromPresence
                    message(error.message ?: "Room unavailable. Try again.")
                    failed(error)
                    if (room != null) {
                        retry.text = if (error is PairingRequired) "Pair again" else "Retry"
                        retry.setOnClickListener {
                            if (error is PairingRequired) pairAgain() else show()
                        }
                        retry.visibility = View.VISIBLE
                    }
                }
            }
        }
    }

    private fun pairAgain() {
        val library = phone()
        if (library != null && (library.options.contains("enabled") || library.options.contains("collection"))) {
            restoreAccess(library)
            return
        }
        stopService(Intent(this, RoomPlayback::class.java))
        room = null
        resetLibrary()
        show()
        message("Enter your server URL or paste a fresh pairing link.")
    }

    private val homeWifi get() = intent.getBooleanExtra("home_wifi", false)
    private val pairOnly get() = intent.getBooleanExtra("pair_only", false)

    private fun connect(server: String? = null, draft: String = "") {
        content.removeAllViews()
        val form = ui.column(8)
        content.addView(ScrollView(this).apply { addView(form) })
        val label = when {
            server != null -> "Pairing link"
            homeWifi -> "Wi-Fi address or pairing link"
            else -> "Server URL"
        }
        val help = when {
            server != null -> "Connected to $server. Paste a pairing link from Settings → Devices on that server."
            homeWifi -> "Enter your paired room’s Wi-Fi address, including http:// and the port, or paste a pairing link."
            else -> "Enter the URL you use to reach Listening Room on Wi-Fi or Tailscale. You can also paste a pairing link."
        }
        form.addView(ui.label(help))
        form.addView(ui.heading(label, 16f))
        val link = ui.field(if (server == null) "http://your-server:8765" else label).apply {
            contentDescription = label
            inputType = InputType.TYPE_CLASS_TEXT or if (server == null)
                InputType.TYPE_TEXT_VARIATION_URI else InputType.TYPE_TEXT_VARIATION_PASSWORD
            importantForAutofill = View.IMPORTANT_FOR_AUTOFILL_NO
            typeface = Typeface.DEFAULT
            setText(draft)
        }
        form.addView(link)
        lateinit var connectButton: Button
        val changeAddress = if (server != null) ui.button("Change server URL") {
            message()
            connect(draft = server)
        } else null
        connectButton = ui.button(if (server == null) "Connect" else "Pair device", true) {
            connectButton.isEnabled = false
            changeAddress?.isEnabled = false
            message("Connecting…")
            val value = link.text.toString()
            load({ Room.connect(this, value, homeWifi, server) }, {
                connectButton.isEnabled = true
                changeAddress?.isEnabled = true
            }) { result ->
                when (result) {
                    is Room.Connection.NeedsInvitation -> {
                        message()
                        connect(result.origin)
                    }
                    is Room.Connection.Paired -> {
                        if (pairOnly) {
                            finish()
                            return@load
                        }
                        room = result.room
                        handler.removeCallbacks(presence)
                        handler.post(presence)
                        resetLibrary()
                        show()
                    }
                }
            }
        }
        form.addView(connectButton)
        changeAddress?.let(form::addView)
        link.imeOptions = android.view.inputmethod.EditorInfo.IME_ACTION_GO
        link.setOnEditorActionListener { _, action, _ ->
            if (action != android.view.inputmethod.EditorInfo.IME_ACTION_GO) return@setOnEditorActionListener false
            connectButton.performClick()
            true
        }
    }

    private fun refreshPlaylist(target: Room) {
        val selected = playlistDetail ?: return
        // A dialog owns the window while the user chooses a link or edits a song.
        // Keep its captured destination intact until the server validates the submission.
        fun visible() = active && screen == Screen.LIBRARY && hasWindowFocus() &&
            playlistSearch?.text?.toString() == query
        if (!visible() || playlistRefreshGeneration == generation) return
        playlistRefreshGeneration = generation
        load({ target.get("/api/overview").getJSONArray("playlists") },
            finished = { playlistRefreshGeneration = null }, reportErrors = false) { rows ->
            if (!visible()) return@load
            val row = (0 until rows.length()).asSequence().map(rows::getJSONObject)
                .firstOrNull { it.optString("id") == selected.key }
            if (row == null) {
                resetLibrary()
                screen = Screen.EXPLORE
                show()
                message("This playlist is no longer available.")
                return@load
            }
            val latest = RoomPlaylist(row)
            if (selected.sameEdition(latest)) return@load
            val list = (0 until content.childCount).asSequence().map(content::getChildAt)
                .filterIsInstance<ListView>().firstOrNull()
            val position = list?.let { it.firstVisiblePosition to (it.getChildAt(0)?.top ?: 0) }
            playlistDetail = latest
            selection = latest.name
            offset = minOf(offset, (row.optInt("entries") - 1).coerceAtLeast(0) / 20 * 20)
            show(position)
        }
    }

    private fun library(listPosition: Pair<Int, Int>?) {
        val target = room ?: return
        if (phone()?.local != true) playlistDetail?.let { selected ->
            val header = ui.column()
            val buttons = ui.row(6)
            if (selected.linked) header.addView(ui.label(selected.syncLabel, 14f, ui.accentInk).apply {
                setCompoundDrawablesRelative(ui.mark(Mark.MAGNET, 17, ui.accent), null, null, null)
                compoundDrawablePadding = ui.dp(6)
            })
            buttons.part(ui.button("Play playlist", true) {
                send("play_available", JSONObject().put("playlist", selected.key))
                screen = Screen.PLAYER
                show()
            }.apply { isEnabled = selected.playable > 0 })
            if (selected.canAdd) buttons.part(ui.button("Add song") { addSong(target, selected, SongDraft()) })
            header.addView(buttons)
            if (!selected.canAdd) header.addView(ui.label(
                when {
                    selected.needsWritePermission -> "Reconnect Spotify in the room’s Settings to allow playlist edits."
                    selected.spotify -> "Check the Spotify link and device permissions in the room’s Settings."
                    selected.linked -> "Edit this playlist at its source."
                    else -> "The room owner can allow this device to add songs."
                }, 14f, ui.secondary))
            content.addView(header, LinearLayout.LayoutParams(-1, -2).apply { bottomMargin = ui.dp(8) })
        }
        val search = ui.field("Song, artist or album").apply {
            setText(query)
            imeOptions = EditorInfo.IME_ACTION_SEARCH
        }
        if (playlistDetail != null) playlistSearch = search
        fun submit() {
            getSystemService(InputMethodManager::class.java).hideSoftInputFromWindow(search.windowToken, 0)
            query = search.text.toString().trim()
            offset = 0
            show()
        }
        search.setOnEditorActionListener { _, action, event ->
            val enter = event?.keyCode == KeyEvent.KEYCODE_ENTER
            if (action == EditorInfo.IME_ACTION_SEARCH || (enter && event?.action == KeyEvent.ACTION_DOWN)) {
                submit()
                true
            } else enter
        }
        val form = ui.row()
        form.addView(search, LinearLayout.LayoutParams(0, ui.dp(48), 1f))
        form.addView(ui.button("Search", true, ::submit), LinearLayout.LayoutParams(-2, -2).apply { leftMargin = ui.dp(6) })
        content.addView(form)
        val count = ui.label("", 14f, ui.secondary).apply { setPadding(0, ui.dp(9), 0, ui.dp(4)) }
        content.addView(count)
        val params = listOf("playlist" to playlist, "genre" to genre, "mood" to mood, "filter" to filterName,
            "q" to query, "offset" to offset.toString(), "limit" to "20")
            .joinToString("&") { (k, v) -> "$k=${Room.encode(v)}" }
        val library = phone()
        val local = library?.local == true
        if (!local && playlist == "all" && filterName == "all" && query.isBlank() && genre.isBlank() && mood.isBlank() && offset == 0) {
            downloadsShelf = ui.column(4).also { content.addView(it, content.indexOfChild(count)) }
            renderRecentDownloads(target)
            refreshDownloadActivity(target)
        }
        load({
            if (local) {
                val (total, rows) = requireNotNull(library).songs(query, offset)
                JSONObject().put("total", total).put("tracks", JSONArray(rows))
            } else {
                target.requireCatalog()
                target.get("/api/tracks?$params")
            }
        }) { data ->
            val tracks = data.getJSONArray("tracks")
            val rows = (0 until tracks.length()).map { n ->
                val track = tracks.getJSONObject(n)
                val ready = if (local) !track.isNull("local_hash") else track.optString("status") == "verified"
                val artist = track.optString("artist")
                val duration = track.optDouble("duration", 0.0).toInt()
                val availability = when {
                    ready -> ""
                    local -> "Not saved on phone"
                    track.optString("status") == "not_found" -> "No copy found"
                    else -> "Not downloaded"
                }
                val subtitle = listOf(artist, availability).filter(String::isNotEmpty).joinToString(" · ")
                val queueAction: ((String) -> Unit)? = if (ready && !local) { op ->
                    load({ target.action(op, JSONObject().put("track_id", track.getInt("id"))) }) { snapshot ->
                        if (radioSnapshot(target.id) == null) renderPlayer(snapshot)
                        message(if (op == "play_next") "Playing next" else "Added to queue")
                    }
                } else null
                val retry = track.optJSONObject("mp3_retry")
                val info = { ui.showSongInfo(this, target, track, copyAvailable = ready, queueAction = queueAction,
                    findCopy = if (local || ready) null else { {
                        if (retry == null) requestCopy(target, track.getInt("id")) else requestMp3(target, retry)
                    } },
                    addToPlaylist = if (local) null else { { pickPlaylist(target, SongDraft.from(track, catalogTrack = true)) } },
                    profilePlayback = if (local) null else ::send) }
                val ref = track.musicProfileRef()
                val album = if (local || ref == null) "" else track.optString("album")
                fun profile(kind: String, name: String): (() -> Unit)? =
                    if (local || ref == null || name.isBlank()) null
                    else { { ui.showMusicProfile(this, target, "$kind/$ref", ::send) } }
                Entry(track.getString("title"), subtitle, if (duration > 0) clockTime(duration) else "",
                    info = if (ready) info else null, openArtist = profile("artist", artist),
                    album = album, openAlbum = profile("album", album)) {
                    if (ready) {
                        send("play", if (local) JSONObject().put("key", track.getString("key"))
                            else JSONObject().put("track_id", track.getInt("id")).put("playlist", playlist))
                        screen = Screen.PLAYER
                        show()
                    } else info()
                }
            }
            content.addView(ui.index(rows).apply {
                listPosition?.let { (first, top) -> setSelectionFromTop(first, top) }
            }, LinearLayout.LayoutParams(-1, 0, 1f))
            val total = data.getInt("total")
            val number = NumberFormat.getIntegerInstance()
            count.text = when {
                total == 0 && local && query.isBlank() -> if (ReplicaSync.busy) ReplicaSync.progress
                    else library?.options?.getString("status", "Connect to the room and sync in Settings.")
                total == 0 -> if (query.isBlank()) "No songs in this selection." else "No songs match “$query”."
                total == 1 -> "1 song"
                else -> "${number.format(total)} songs"
            }
            if (total > 20 || offset > 0) {
                val pages = ui.row()
                pages.addView(ui.iconButton("Previous page", Mark.BACK) {
                    offset = (offset - 20).coerceAtLeast(0)
                    show()
                }.apply { isEnabled = offset > 0 }, LinearLayout.LayoutParams(ui.dp(48), ui.dp(48)))
                pages.part(ui.label("${number.format(offset + 1)}–${number.format(offset + rows.size)}", 14f).apply {
                    gravity = android.view.Gravity.CENTER
                })
                pages.addView(ui.iconButton("Next page", Mark.FORWARD) {
                    offset += 20
                    show()
                }.apply { isEnabled = offset + 20 < total }, LinearLayout.LayoutParams(ui.dp(48), ui.dp(48)))
                content.addView(pages)
            }
        }
    }

    private fun pickPlaylist(target: Room, draft: SongDraft) {
        load({ target.get("/api/overview").getJSONArray("playlists") }) { rows ->
            val choices = (0 until rows.length()).map { RoomPlaylist(rows.getJSONObject(it)) }.filter { it.canAdd }
            if (choices.isEmpty()) {
                message("No editable playlists. The room owner can allow playlist additions for this device in Settings → Devices.")
            } else ui.choices(this, "Add to playlist", choices.map {
                Entry(it.name, it.syncLabel, mark = if (it.linked) Mark.MAGNET else null) { addSong(target, it, draft) }
            })
        }
    }

    private fun addSong(target: Room, selected: RoomPlaylist, draft: SongDraft) {
        if (selected.grouped) {
            val choices = selected.writableTargets()
            if (choices.isEmpty()) message("This playlist has no writable Spotify link.")
            else ui.choices(this, "Choose a Spotify playlist", choices.map { choice ->
                val name = choice.targetName.ifBlank { choice.name }
                val repeated = choices.count { it.targetName.ifBlank { it.name } == name } > 1
                val label = if (repeated) "$name · ${choice.providerPlaylistId}" else name
                Entry(label, choice.syncLabel, mark = Mark.MAGNET) { addSong(target, choice, draft) }
            })
            return
        }
        ui.addSongForm(this, selected, draft) { body, done ->
            load({ target.addPlaylistSong(body) }, failed = { done(it.message ?: "Could not add this song.") },
                reportErrors = false, executor = catalogIo) {
                done(null)
                if (screen == Screen.LIBRARY && playlist == selected.key) {
                    query = ""; offset = 0; show()
                }
                message(it.optString("message", "Song added to the playlist."))
            }
        }
    }

    private fun explore() {
        val target = room ?: return
        load({ target.requireCatalog(); target.get("/api/overview").getJSONArray("playlists") to
            target.get("/api/tracks?limit=1&facets=moods&facets=mood-availability") }) { (playlists, facets) ->
            val rows = mutableListOf<Entry>()
            fun choose(name: String, list: RoomPlaylist? = null, tag: String = "", filter: String = "all", group: String = "") {
                resetLibrary()
                playlistDetail = list
                selection = name; playlist = list?.key ?: "all"; genre = tag; filterName = filter; mood = group
                show()
            }
            rows.add(Entry("Deep Dive", "Recordings and artist connections") { screen = Screen.DISCOVER; show() })
            rows.add(Entry("Browse catalogs", "Public sources and private libraries") { screen = Screen.CATALOGS; show() })
            rows.add(Entry("Extensions", "Sources used by Find copy") { showRoomExtensions(target) })
            listOf("Not downloaded" to "missing", "Favorites" to "favorites", "Recently played" to "recent", "Most played" to "most").forEach { (name, key) ->
                rows.add(Entry(name) { choose(name, filter = key) })
            }
            val tags = facets.optJSONArray("genres") ?: JSONArray()
            val moods = facets.optJSONArray("moods") ?: JSONArray()
            val groups = (0 until moods.length()).mapNotNull { RoomMood.read(moods.getJSONObject(it)) }
            val supportsPlayback = facets.has("ready_genres")
            val map = groups.takeIf { it.isNotEmpty() }?.let {
                ui.column().apply {
                    var genre = ""
                    fun play(mix: MoodMix) {
                        if (genre !in mix.genres) genre = ""
                        if (supportsPlayback) {
                            message("Tuning in…")
                            send("play_mood", mix.fields(genre))
                        } else {
                            choose(genre.ifEmpty { mix.title }, tag = genre, group = mix.first.id)
                        }
                    }
                    val field = MoodMap(this@RoomActivity, ui, groups, ::play)
                    setPadding(0, ui.dp(5), 0, ui.dp(12))
                    addView(ui.row(6).apply {
                        part(ui.heading("Mood Map", 20f))
                        addView(ui.button("Activities") { screen = Screen.ACTIVITIES; show() })
                        addView(ui.iconButton("Map settings", Mark.SETTINGS) {
                            val mix = field.selection
                            val current = genre.takeIf { it in mix.genres }.orEmpty()
                            ui.choices(this@RoomActivity, "Map settings · Genre",
                                (listOf("") + mix.genres).map { tag ->
                                    Entry(tag.ifEmpty { "Whole mix" }, if (tag == current) "Selected" else "") {
                                        genre = tag
                                        play(mix)
                                    }
                                })
                        })
                    })
                    addView(ui.label("Tap to play. Drag between moods to blend them.", 14f, ui.secondary))
                    addView(field, LinearLayout.LayoutParams(-1, ui.dp(255)).apply { topMargin = ui.dp(6) })
                }
            }
            if (map == null) rows.add(0, Entry("Activities") { screen = Screen.ACTIVITIES; show() })
            if (playlists.length() > 0) rows.add(Entry("Playlists"))
            for (i in 0 until playlists.length()) {
                val item = RoomPlaylist(playlists.getJSONObject(i))
                rows.add(Entry(item.name, item.syncLabel, mark = if (item.linked) Mark.MAGNET else null) {
                    choose(item.name, list = item)
                })
            }
            if (tags.length() > 0) rows.add(Entry("Genres"))
            for (i in 0 until tags.length()) {
                val name = tags.getString(i)
                rows.add(Entry(name) { choose(name, tag = name) })
            }
            content.addView(ui.index(rows, map))
        }
    }

    private fun activityMixes(target: Room) {
        val panel = ui.column(6)
        val list = ui.column(6)
        panel.addView(list)
        val pending = ui.label("Loading activities…", 14f, ui.secondary)
        panel.addView(pending)
        content.addView(ScrollView(this).apply { addView(panel) })
        val rows = mutableMapOf<String, Pair<TextView, View>>()
        fun refresh() {
            load({ target.get("/api/activities") }, failed = {
                pending.text = "Couldn’t load activity mixes."
                pending.visibility = View.VISIBLE
            }) { index ->
                val indexed = index.optInt("indexed")
                val total = index.optInt("total")
                val canAnalyze = index.optBoolean("analysis_available")
                val preparing = index.optBoolean("preparing")
                val activities = index.getJSONArray("activities")
                for (i in 0 until activities.length()) {
                    val item = activities.getJSONObject(i)
                    val id = item.getString("id")
                    val name = item.getString("name")
                    val count = item.getInt("count")
                    val (detail, play) = rows.getOrPut(id) {
                        val label = ui.label("", 14f, ui.secondary)
                        val button = ui.iconButton("Play $name mix", Mark.PLAY) {
                            message("Starting mix…")
                            send("play_activity", JSONObject().put("activity", id))
                        }
                        list.addView(ui.panel().apply {
                            addView(ui.row(8).apply {
                                part(ui.column(3).apply {
                                    addView(ui.heading(name, 18f))
                                    addView(label)
                                })
                                addView(button)
                            })
                        })
                        label to button
                    }
                    val songs = if (count == 1) "song" else "songs"
                    val description = listOf(item.optString("energy_label"),
                        "${item.getInt("min_bpm")}–${item.getInt("max_bpm")} BPM", "$count $songs")
                        .filter(String::isNotBlank).joinToString(" · ")
                    if (detail.text != description) detail.text = description
                    play.isEnabled = count > 0
                }
                val progress = when {
                    preparing -> "Getting mixes ready…"
                    total == 0 -> "Add music to your library to start a mix."
                    !canAnalyze -> "Audio analysis is unavailable on this server."
                    indexed < total -> "Checking songs · $indexed of $total"
                    else -> ""
                }
                if (pending.text != progress) pending.text = progress
                pending.visibility = if (progress.isEmpty()) View.GONE else View.VISIBLE
                if (preparing || canAnalyze && indexed < total) panel.postDelayed({
                    if (active && panel.isAttachedToWindow && screen == Screen.ACTIVITIES) refresh()
                }, 8_000)
            }
        }
        refresh()
    }

    private data class RadioChoice(val name: String, val detail: String,
                                   val uri: Uri? = null, val source: RadioSource = RadioSource.LIBRARY) {
        val label get() = "$name · $detail"
    }

    private fun radio(target: Room) {
        content.addView(ui.button("SomaFM settings in browser") {
            try {
                startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(target.origin + "/?setup=radio")))
            } catch (_: android.content.ActivityNotFoundException) {
                message("Open the room’s Radio settings in a browser.")
            }
        }, LinearLayout.LayoutParams(-1, -2).apply { bottomMargin = ui.dp(8) })
        val search = ui.field("Search genres, mixes, or live stations").apply {
            imeOptions = EditorInfo.IME_ACTION_SEARCH
            isEnabled = false
        }
        content.addView(search, LinearLayout.LayoutParams(-1, -2).apply { bottomMargin = ui.dp(12) })
        val results = ui.column()
        content.addView(results, LinearLayout.LayoutParams(-1, 0, 1f))
        val controls = ui.column()
        content.addView(controls)
        val loading = ui.label("Loading stations…", 14f, ui.secondary)
        results.addView(loading)
        load({ target.get("/api/stations") }, failed = {
            authorizedExternalRadio = null
            loading.text = "Stations are unavailable."
        }) { data ->
            setRadioAccess(target, data)
            val genres = data.optJSONObject("ready_genres") ?: JSONObject()
            val streams = data.optJSONObject("live") ?: JSONObject()
            val library = genres.keys().asSequence().map { name ->
                val count = genres.getInt(name)
                RadioChoice(name, "Your library · $count " + if (count == 1) "song" else "songs")
            }.toList()
            val live = streams.keys().asSequence().mapNotNull { name ->
                RadioHistory.liveStation(data, name)?.let { station ->
                    RadioChoice(name, "SomaFM · ${station.title}", source = RadioSource.LIVE)
                }
            }.toList()
            val saved = data.optJSONArray("stations")?.takeIf { data.optBoolean("youtube_enabled", true) } ?: JSONArray()
            var videoCount = 0
            val mixes = (0 until saved.length()).mapNotNull { index ->
                val station = saved.getJSONObject(index)
                val ids = RadioHistory.videoIds(station.optJSONArray("seeds"))
                val stream = RadioHistory.mixUri(ids) ?: return@mapNotNull null
                videoCount += ids.size
                val count = "${ids.size} " + if (ids.size == 1) "video" else "videos"
                RadioChoice(station.getString("name"), "YouTube · $count",
                    uri = stream,
                    source = RadioSource.YOUTUBE)
            }
            fun updateControls() {
                controls.removeAllViews()
                val history = RadioHistory.read(this, target.id)
                if (RoomPlayback.hasRadio || radioSnapshot(target.id) != null) {
                    val clearLabel = if (history?.source == RadioSource.YOUTUBE) "Forget station" else "Stop radio"
                    controls.addView(ui.button(clearLabel) {
                        send("radio_stop")
                        controls.removeAllViews()
                    })
                }
            }
            updateControls()
            fun tune(choice: RadioChoice) {
                getSystemService(InputMethodManager::class.java).hideSoftInputFromWindow(search.windowToken, 0)
                when (choice.source) {
                    RadioSource.LIBRARY -> send("radio_start", JSONObject().put("genre", choice.name))
                    RadioSource.LIVE -> send("radio_live", JSONObject().put("name", choice.name))
                    RadioSource.YOUTUBE -> {
                        openRadioVideo(target, RadioHistory(RadioSource.YOUTUBE, choice.name,
                            choice.uri.toString(), choice.name, "YouTube"), onOpen = ::updateControls)
                        return
                    }
                }
                screen = Screen.PLAYER
                show()
            }
            var firstMatch: RadioChoice? = null
            val libraryPreview = library.sortedWith(compareByDescending<RadioChoice> { genres.optInt(it.name) }
                .thenBy { it.name.lowercase() }).take(8)
            fun filter(text: String) {
                val query = text.trim()
                val relevance = compareBy<RadioChoice> {
                    when {
                        it.name.equals(query, ignoreCase = true) -> 0
                        it.name.startsWith(query, ignoreCase = true) -> 1
                        else -> 2
                    }
                }.thenBy { it.name.lowercase() }
                fun matching(choices: List<RadioChoice>) = choices
                    .filter { it.label.contains(query, ignoreCase = true) }.sortedWith(relevance)
                val youtube = if (query.isEmpty()) mixes else matching(mixes)
                val roomAndLive = if (query.isEmpty()) libraryPreview + live else matching(library + live)
                firstMatch = youtube.firstOrNull() ?: roomAndLive.firstOrNull()
                val videos = if (videoCount == 1) "video" else "videos"
                val topics = if (mixes.size == 1) "topic" else "topics"
                val groups = listOf(
                    "SomaFM live" to roomAndLive.filter { it.source == RadioSource.LIVE },
                    "YouTube mixes · ${mixes.size} $topics · $videoCount $videos" to youtube,
                    "Library Radio" to roomAndLive.filter { it.source == RadioSource.LIBRARY },
                )
                val orderedGroups = if (query.isEmpty()) groups else groups.sortedByDescending { (_, choices) ->
                    choices.firstOrNull() == firstMatch
                }
                val entries = orderedGroups.flatMap { (label, choices) ->
                    if (choices.isEmpty()) emptyList() else listOf(Entry(label)) + choices.map { choice ->
                        val action = if (choice.source == RadioSource.YOUTUBE) "Open" else "Play"
                        Entry(choice.name, choice.detail, action) { tune(choice) }
                    }
                }
                results.removeAllViews()
                if (entries.isEmpty()) results.addView(ui.label("No matching stations or playable genres.", 14f, ui.secondary))
                else results.addView(ui.index(entries))
            }
            filter("")
            search.isEnabled = true
            search.setOnEditorActionListener { _, action, event ->
                val enter = event?.keyCode == KeyEvent.KEYCODE_ENTER
                if (action == EditorInfo.IME_ACTION_SEARCH || (enter && event?.action == KeyEvent.ACTION_DOWN)) {
                    if (search.text.isNotBlank()) firstMatch?.let(::tune)
                    true
                } else enter
            }
            search.addTextChangedListener(object : TextWatcher {
                override fun beforeTextChanged(text: CharSequence?, start: Int, count: Int, after: Int) = Unit
                override fun onTextChanged(text: CharSequence?, start: Int, before: Int, count: Int) {
                    filter(text?.toString().orEmpty())
                }
                override fun afterTextChanged(text: Editable?) = Unit
            })
        }
    }

    private fun deepDive(target: Room) {
        val edition = ui.label("", 14f, ui.secondary).apply { visibility = View.GONE }
        val banner = ui.row(8).apply {
            background = ui.surface(ui.fog)
            setPadding(ui.dp(10), ui.dp(8), ui.dp(8), ui.dp(8))
            addView(ImageView(context).apply {
                setImageDrawable(ui.mark(Mark.ALBUM, 38)); importantForAccessibility = View.IMPORTANT_FOR_ACCESSIBILITY_NO
            }, LinearLayout.LayoutParams(ui.dp(38), ui.dp(38)))
            part(ui.column().apply {
                addView(ui.heading("Weekly Discovery", 18f))
                addView(edition)
            })
            addView(ui.iconButton("Open Weekly Discovery", Mark.FORWARD) { screen = Screen.WEEKLY; show() })
        }
        content.addView(banner)
        message("Loading recordings…")
        load({ target.requireCatalog(); target.get("/api/discover") }) { shelf ->
            val monday = runCatching { java.time.LocalDate.parse(shelf.optString("weekly_edition")) }.getOrNull()
            if (monday != null) {
                val weeks = monday.toEpochDay() / 7
                banner.background = android.graphics.drawable.GradientDrawable(
                    android.graphics.drawable.GradientDrawable.Orientation.values()[Math.floorMod(weeks, 3L).toInt()],
                    intArrayOf(ui.fog, ui.card),
                ).apply { cornerRadius = ui.dp(10).toFloat(); setStroke(ui.dp(1), ui.rule) }
                edition.text = "Week of ${monday.format(java.time.format.DateTimeFormatter.ofPattern("MMM d"))}"
                edition.visibility = View.VISIBLE
            }
            when (shelf.optString("status")) {
                "building" -> {
                    message("Finding recordings…")
                    if (active) handler.postDelayed(refreshDiscover, 5000)
                }
                "error" -> {
                    message("Recordings couldn’t load.")
                    retry.visibility = View.VISIBLE
                }
                else -> {
                    val tracks = shelf.getJSONArray("tracks")
                    message(if (tracks.length() == 0) "Favorite a song to get recording suggestions." else "")
                    if (tracks.length() == 0) return@load
                    val layout = ui.column(8)
                    layout.addView(ui.heading("Recordings to hear", 18f))
                    val columnCount = if (resources.configuration.screenWidthDp >= 320) 2 else 1
                    val columns = List(columnCount) { ui.column(8) }
                    layout.addView(ui.row(8).apply {
                        gravity = android.view.Gravity.TOP
                        columns.forEach { part(it) }
                    })
                    for (i in 0 until tracks.length()) {
                        val item = tracks.getJSONObject(i)
                        val artist = item.getString("artist")
                        val title = item.getString("title")
                        val note = item.optJSONObject("request")?.copyStatusLabel()?.takeIf(String::isNotBlank)
                            ?: item.optString("version").ifBlank { item.optString("album") }
                        columns[i % columnCount].addView(ui.button(listOf(title, artist, note).filter(String::isNotBlank).joinToString("\n")) {
                            load({ target.pickRequest(item) }) { row -> openPick(target, item, row) }
                        }.apply { gravity = android.view.Gravity.START or android.view.Gravity.CENTER_VERTICAL })
                    }
                    content.addView(ScrollView(this).apply { addView(layout) }, LinearLayout.LayoutParams(-1, 0, 1f))
                }
            }
        }
    }

    private fun weeklyDiscovery(target: Room) {
        val scrollY = weeklyScroll?.scrollY ?: 0
        val actions = ui.row(6).apply {
            gravity = android.view.Gravity.END or android.view.Gravity.CENTER_VERTICAL
            addView(ui.iconButton("Refresh playlist", Mark.REFRESH) { show() })
        }
        content.addView(actions, LinearLayout.LayoutParams(-1, -2))
        message("Loading this week’s playlist…")
        load({ target.get("/api/discover/weekly") }) { playlist ->
            when (playlist.optString("state")) {
                "building" -> {
                    message("Building this week’s playlist…")
                    if (active) handler.postDelayed(refreshDiscover, 5000)
                }
                "waiting" -> message("Listen to a few songs to get your first playlist.")
                "unavailable" -> {
                    message("Music discovery is temporarily unavailable.")
                    retry.visibility = View.VISIBLE
                }
                "ready" -> {
                    val rows = playlist.getJSONArray("tracks")
                    val tracks = (0 until rows.length()).map(rows::getJSONObject)
                    val canRequest = playlist.optBoolean("can_request")
                    var editingPlaylist = false
                    val pending = tracks.filter { track ->
                        val request = track.optJSONObject("request")
                        val status = request?.optString("status")
                        !track.optBoolean("in_library") && request?.needsCopyReview() != true &&
                            (status == null || status in setOf("not_found", "error", "rejected"))
                    }
                    fun readyId(track: JSONObject): Int? {
                        val request = track.optJSONObject("request") ?: return null
                        return request.optInt("id").takeIf {
                            it > 0 && request.optString("status") == "downloaded" && request.optString("audio").isNotBlank()
                        }
                    }
                    fun play(op: String, fields: JSONObject) {
                        send(op, fields)
                        screen = Screen.PLAYER
                        show()
                    }
                    fun review(track: JSONObject) {
                        val request = track.getJSONObject("request")
                        val row = JSONObject(request.toString()).put("title", request.optString("title", track.getString("title")))
                            .put("artist", request.optString("artist", track.getString("artist")))
                        chooseDiscoveryFormat(row, target) { show() }
                    }
                    val readyIds = tracks.mapNotNull(::readyId)
                    message(when {
                        tracks.isEmpty() -> "This week’s playlist is empty."
                        !canRequest && pending.isNotEmpty() -> "Downloads are unavailable in this room."
                        else -> ""
                    })
                    if (readyIds.isNotEmpty()) {
                        actions.addView(ui.iconButton("Play downloaded songs", Mark.PLAY) {
                            if (editingPlaylist) return@iconButton
                            play("play_requests", JSONObject().put("request_ids", JSONArray(readyIds)))
                        }, 0)
                    }
                    if (canRequest && pending.isNotEmpty()) {
                        actions.addView(ui.iconButton("Download playlist", Mark.DOWNLOAD, solid = true) {
                            if (editingPlaylist) return@iconButton
                            editingPlaylist = true
                            message("Queuing songs for download…")
                            load({ target.requestWeeklySongs(pending.map { it.getString("id") }) },
                                 finished = { editingPlaylist = false }) { result ->
                                show()
                                val count = result.optInt("queued")
                                message(if (count == 0) "These songs already have a room request or copy."
                                        else "$count ${if (count == 1) "song" else "songs"} queued for download.")
                            }
                        }, actions.childCount - 1)
                    }
                    tracks.firstOrNull {
                        !it.optBoolean("in_library") && it.optJSONObject("request")?.needsCopyReview() == true
                    }?.let { track ->
                        actions.addView(ui.iconButton("Review copies", Mark.BROWSE) { review(track) }, actions.childCount - 1)
                    }
                    playlist.optJSONObject("download_progress")?.let { progress ->
                        val total = progress.optInt("total")
                        val downloaded = progress.optInt("downloaded")
                        val percent = progress.optInt("percent")
                        val attention = progress.optInt("needs_attention")
                        if (total > 0) {
                            val note = if (attention == 0) "" else " · $attention ${if (attention == 1) "needs" else "need"} attention"
                            content.addView(ui.label("$downloaded of $total downloaded · $percent%$note", 14f, ui.secondary))
                            content.addView(ProgressBar(this, null, android.R.attr.progressBarStyleHorizontal).apply {
                                max = 100
                                this.progress = percent
                                contentDescription = "Playlist downloads"
                            }, LinearLayout.LayoutParams(-1, ui.dp(12)))
                        }
                        if (active && progress.optInt("pending") > 0) handler.postDelayed(refreshDiscover, 8_000L)
                    }
                    val songs = ui.column()
                    for (track in tracks) {
                        val request = track.optJSONObject("request")
                        val title = request?.optString("title")?.takeIf(String::isNotBlank) ?: track.getString("title")
                        val artist = request?.optString("artist")?.takeIf(String::isNotBlank) ?: track.getString("artist")
                        val via = track.optJSONArray("trail")?.let { names ->
                            (0 until (names.length() - 1)).joinToString(" → ") { names.getString(it) }
                        }.orEmpty()
                        val requestStatus = request?.optString("status").orEmpty()
                        val inLibrary = track.optBoolean("in_library")
                        val detail = if (inLibrary && requestStatus != "missing") "In library" else request?.copyStatusLabel().orEmpty()
                        songs.addView(ui.line())
                        songs.addView(ui.row(8).apply {
                            part(ui.column().apply {
                                addView(ui.heading(title, 17f))
                                addView(ui.label(artist, 14f, ui.secondary))
                                if (via.isNotBlank()) addView(ui.label("Via $via", 13f, ui.secondary))
                                if (detail.isNotBlank()) addView(ui.row().apply { addView(ui.statusChip(detail)) })
                            })
                            readyId(track)?.let { id ->
                                addView(ui.iconButton("Play $title", Mark.PLAY) {
                                    play("play_request", JSONObject().put("request_id", id))
                                })
                            }
                            if (!inLibrary && requestStatus in CHANGEABLE_FORMAT &&
                                request != null && request.has("format_revision")) {
                                val copies = request.needsCopyReview()
                                val label = if (copies) "Review copies for $title" else "Choose format for $title"
                                addView(ui.iconButton(label, if (copies) Mark.BROWSE else Mark.SETTINGS) { review(track) })
                            }
                            if (!inLibrary && request != null && requestStatus in RETRYABLE_COPY && !request.needsCopyReview()) {
                                addView(ui.iconButton("Retry now: $title", Mark.REFRESH) {
                                    load({ target.retryCopy(request) }) { show() }
                                })
                            }
                            if (!inLibrary && (request == null || (!request.needsCopyReview() && request.canReplaceRecording()))) {
                                addView(ui.iconButton("Other recordings: $title", Mark.ALBUM) {
                                    chooseRequestVersion(target, track, request)
                                })
                            }
                            val removeKey = track.optString("remove_key")
                            if (removeKey.isNotBlank()) {
                                addView(ui.iconButton("Remove $title from Weekly Discovery", Mark.CLOSE) {
                                    if (editingPlaylist) return@iconButton
                                    editingPlaylist = true
                                    message("Removing song…")
                                    load({ target.removeWeeklySong(track.getString("id"), playlist.getString("week"), removeKey) },
                                         finished = { editingPlaylist = false }) { show() }
                                })
                            }
                        })
                    }
                    val scroll = ScrollView(this).apply { addView(songs) }
                    weeklyScroll = scroll
                    content.addView(scroll, LinearLayout.LayoutParams(-1, 0, 1f))
                    if (scrollY > 0) scroll.post { scroll.scrollTo(0, scrollY) }
                }
            }
        }
    }

    private fun chooseRequestVersion(target: Room, item: JSONObject, row: JSONObject?) {
        chooseRecordingVersion(target, row?.takeIf { it.optString("musicbrainz_id").isNotBlank() } ?: item) { version ->
            if (row == null || row.optString("status") == "downloaded") {
                openPick(target, version, null)
            } else {
                load({ target.requestCopy(version, row) }) { updated ->
                    show()
                    if (updated.optString("status") == "waiting_format") openPick(target, version, updated)
                    else message(updated.copyStatusLabel())
                }
            }
        }
    }

    private fun chooseRecordingVersion(target: Room, item: JSONObject, choose: (JSONObject) -> Unit) {
        val content = ui.column(8)
        val status = ui.label("Looking for versions…")
        content.addView(status)
        val style = SongDetailsStyle(ui)
        val dialog = style.dialog(this, "Other recordings", ScrollView(this).apply { addView(content) },
            artist = item.optString("title"))
        handler.removeCallbacks(refreshDiscover)
        dialog.setOnDismissListener {
            if (active && screen == Screen.WEEKLY) handler.postDelayed(refreshDiscover, 8_000L)
        }
        dialog.show()
        load({ target.recordingVersions(item) }, reportErrors = false,
            failed = { status.text = "Couldn’t load versions. Try again." }) { versions ->
            if (!dialog.isShowing) return@load
            content.removeView(status)
            if (versions.isEmpty()) content.addView(ui.label("No other recordings found."))
            for (version in versions) {
                content.addView(ui.recordingChoice(version) {
                    dialog.dismiss()
                    choose(version)
                })
            }
        }
    }

    private fun openPick(target: Room, item: JSONObject, row: JSONObject?,
                         creditCache: MutableMap<CreditTarget, Credits> = mutableMapOf()) {
        fun choose(rowToConfirm: JSONObject) {
            chooseDiscoveryFormat(rowToConfirm, target) { show() }
        }
        fun openView(view: SongDetailsView, current: JSONObject?) {
            if (view == SongDetailsView.DETAILS) openPick(target, item, current, creditCache)
            else ui.showArtistConnections(this, target, item, album = view == SongDetailsView.ALBUM,
                openView = { openView(it, current) }, cache = creditCache)
        }
        showDiscoveryPick(target, item, row, onPlay = { id ->
            send("play_request", JSONObject().put("request_id", id))
            screen = Screen.PLAYER
            show()
        }, onRequest = { current ->
            if (current != null && current.optString("status") in CHANGEABLE_FORMAT) choose(current)
            else load({ target.requestCopy(item) }) { request ->
                show()
                if (request.optString("status") in CHANGEABLE_FORMAT) choose(request)
                else openPick(target, item, request)
            }
        }, onTryMp3 = { requestMp3(target, it) },
            openView = item.creditRecordingId()?.let { ::openView },
            onRetry = { current ->
                load({ target.retryCopy(current) }) { result ->
                    show()
                    message(result.optJSONObject("request")?.copyStatusLabel() ?: result.optString("status"))
                }
            },
            onVersions = { current -> chooseRequestVersion(target, item, current) },
            onAddToPlaylist = { pickPlaylist(target, SongDraft.from(item)) },
            watch = { dialog, update ->
                val requestId = row!!.getLong("id")
                val pollRequest = object : Runnable {
                    override fun run() {
                        if (!dialog.isShowing || isDestroyed) return
                        if (!active) {
                            handler.postDelayed(this, 8_000L)
                            return
                        }
                        io.execute {
                            val result = runCatching { target.requestById(requestId) }
                            runOnUiThread {
                                if (!dialog.isShowing || isDestroyed || room?.id != target.id) return@runOnUiThread
                                val latest = result.getOrNull()
                                if (latest != null) {
                                    update(latest)
                                    if (latest.copyPending()) handler.postDelayed(this, 4_000L)
                                } else if (result.isFailure) {
                                    handler.postDelayed(this, 8_000L)
                                }
                            }
                        }
                    }
                }
                dialog.setOnDismissListener { handler.removeCallbacks(pollRequest) }
                handler.postDelayed(pollRequest, 4_000L)
            })
    }

    private fun schedulePoll() {
        handler.removeCallbacks(poll)
        if (active) handler.postDelayed(poll, 2500L)
    }

    private fun setRadioAccess(target: Room, stations: JSONObject) {
        val saved = RadioHistory.read(this, target.id)
        val available = when (saved?.source) {
            RadioSource.LIVE -> RadioHistory.liveStation(stations, saved.name)?.also {
                if (it != saved) it.save(this, target.id)
            }
            RadioSource.YOUTUBE -> saved.takeIf { it.available(stations) }
            else -> null
        }
        authorizedExternalRadio = available?.let { target.id to it }
    }

    private fun verifySavedRadio(target: Room) {
        val saved = RadioHistory.read(this, target.id) ?: return
        if (saved.source == RadioSource.LIBRARY) return
        load({ target.get("/api/stations") }, reportErrors = false, failed = {
            authorizedExternalRadio = null
        }) { stations -> if (active) setRadioAccess(target, stations) }
    }

    private fun radioSnapshot(device: String): JSONObject? = RoomPlayback.radioSnapshot(device)
        ?: RadioHistory.read(this, device)?.takeIf {
            it.source == RadioSource.LIBRARY || (phone()?.local != true && authorizedExternalRadio == (device to it))
        }?.snapshot()

    private fun refreshInsights(target: Room, radioActive: Boolean) {
        val panel = insightsPanel ?: return
        panel.renderBuffer(RoomPlayback.bufferedTracks(target.id))
        if (phone()?.local == true) {
            panel.trafficUnavailable("Offline playback uses files on this device.")
            panel.renderQueue(null)
            return
        }
        if (insightsRequestGeneration == generation) return
        insightsRequestGeneration = generation
        val unreadable = { panel.trafficUnavailable("Couldn’t read playback traffic from the room.") }
        load({
            val traffic = runCatching(target::insights)
            val roomQueue = if (radioActive) runCatching(target::session) else null
            if (traffic.exceptionOrNull() is PairingRequired || roomQueue?.exceptionOrNull() is PairingRequired) {
                throw PairingRequired()
            }
            traffic to roomQueue
        }, finished = { insightsRequestGeneration = null }, reportErrors = false, failed = { unreadable() }) { (traffic, roomQueue) ->
            traffic.onSuccess(panel::renderTraffic).onFailure { unreadable() }
            roomQueue?.onSuccess(panel::renderQueue)?.onFailure { panel.renderQueue(null) }
        }
    }

    private fun refreshPlayer() {
        val target = room ?: return
        val radio = radioSnapshot(target.id)
        refreshInsights(target, radio != null)
        radio?.let {
            renderPlayer(it)
            schedulePoll()
            return
        }
        if (phone()?.local == true) {
            renderPlayer(RoomPlayback.phoneSnapshot(target.id))
            schedulePoll()
            return
        }
        val buffered = RoomPlayback.bufferedSnapshot(target.id)
        if (buffered != null) {
            renderPlayer(buffered)
            schedulePoll()
            return
        }
        // The service already checks the room while this phone is following.
        RoomPlayback.recentRoomSnapshot(target)?.let {
            showRoomSnapshot(target, it)
            return
        }
        load({ target.session() }, reportErrors = screen == Screen.PLAYER,
            failed = { error -> if (error !is PairingRequired) {
                RoomPlayback.bufferedSnapshot(target.id)?.let(::renderPlayer)
                schedulePoll()
            } }) { snap ->
            radioSnapshot(target.id)?.let {
                renderPlayer(it)
                schedulePoll()
                return@load
            }
            showRoomSnapshot(target, snap)
        }
    }

    private fun showRoomSnapshot(target: Room, snapshot: JSONObject) {
        if (screen == Screen.PLAYER) {
            retry.visibility = View.GONE
            presenceError = false
            message()
        }
        if (active && !RoomPlayback.yieldedToOtherPlayer && snapshot.optBoolean("playing") &&
            snapshot.optBoolean("output_selected")) {
            val attempt = target.id to snapshot.optLong("occurrence")
            if (attempt != autoJoinAttempt && !RoomPlayback.following) send("snapshot")
            autoJoinAttempt = attempt
        } else {
            autoJoinAttempt = null
        }
        renderPlayer(snapshot)
        schedulePoll()
    }

    private fun renderPlayer(snapshot: JSONObject) {
        playerPanel?.render(snapshot)
        insightsPanel?.let { panel ->
            panel.renderSource(snapshot)
            if (snapshot.has("queue_length") && !snapshot.optBoolean("radio")) panel.renderQueue(snapshot)
        }
        val title = snapshot.optJSONObject("current")?.optString("title").orEmpty()
        playerShortcut?.apply {
            visibility = if (title.isEmpty()) View.GONE else View.VISIBLE
            if (text.toString() != title) {
                text = title
                contentDescription = "Open now playing: $title"
            }
        }
    }

    private fun openRadioVideo(target: Room, history: RadioHistory, onOpen: () -> Unit = {}) {
        val url = RadioHistory.youtubeUri(history.ref) ?: return
        if (phone()?.local == true) return
        load({ target.get("/api/stations") }, failed = { authorizedExternalRadio = null }) { stations ->
            if (!active) return@load
            if (!history.available(stations)) {
                authorizedExternalRadio = null
                if (screen == Screen.RADIO) show()
                message("This YouTube mix is unavailable. Check the room’s Radio extensions.")
                return@load
            }
            try {
                startActivity(Intent(Intent.ACTION_VIEW, url))
                RoomPlayback.yieldToOtherPlayer(this)
                history.save(this, target.id)
                authorizedExternalRadio = target.id to history
                onOpen()
            } catch (_: android.content.ActivityNotFoundException) {
                message("Install a browser or media player to play this station.")
            }
        }
    }

    private fun send(op: String, fields: JSONObject = JSONObject()) {
        val target = room
        if (op == "radio_resume" && target != null) {
            val history = RadioHistory.read(this, target.id)
            if (history?.source == RadioSource.YOUTUBE) {
                openRadioVideo(target, history)
                return
            }
        }
        startService(Intent(this, RoomPlayback::class.java).setAction(RoomPlayback.COMMAND)
            .putExtra("op", op).putExtra("fields", fields.toString()).putExtra("home_wifi", homeWifi))
    }
}
