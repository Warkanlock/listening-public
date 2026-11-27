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
import android.text.InputType
import android.view.inputmethod.InputMethodManager
import android.graphics.Typeface
import android.view.View
import android.widget.*
import java.text.NumberFormat
import org.json.JSONArray
import org.json.JSONObject
import java.util.concurrent.Executors

class RoomActivity : Activity() {
    private enum class Screen(val title: String, val icon: Mark) {
        LISTEN("Listen", Mark.ROOM), SEARCH("Search", Mark.SEARCH), LIBRARY("Library", Mark.LIBRARY),
        SONGS("Songs", Mark.LIBRARY), PLAYLISTS("Playlists", Mark.ALBUM), GENRES("Genres", Mark.BROWSE),
        DISCOVER("Deep Dive", Mark.EXPLORE), WEEKLY("Weekly Discovery", Mark.ALBUM), MOODS("Moods", Mark.EXPLORE),
        ACTIVITIES("Activities", Mark.PLAYING), CATALOGS("Browse catalogs", Mark.BROWSE),
        DOWNLOADS("Downloads", Mark.DOWNLOAD), RADIO("Radio", Mark.RADIO), PLAYER("Now playing", Mark.PLAYING),
        SETTINGS("Settings", Mark.SETTINGS), SETTING("Settings", Mark.SETTINGS), INSIGHTS("Playback insights", Mark.INSIGHTS)
    }

    private data class Location(val screen: Screen, val listPosition: Pair<Int, Int>?, val scrollOffset: Int?)
    private data class CatalogPage(
        val title: String = "Songs",
        val playlist: RoomPlaylist? = null,
        val genre: String = "",
        val mood: String = "",
        val filter: String = "all",
        val query: String = "",
        val offset: Int = 0,
    )

    private lateinit var ui: RoomUi
    private val io = Executors.newSingleThreadExecutor()
    private val catalogIo = Executors.newSingleThreadExecutor()
    private val handler = Handler(Looper.getMainLooper())
    private var active = false
    private var generation = 0
    private var room: Room? = null
    private var screen = Screen.LISTEN
    private val sectionTrail = ArrayDeque<Location>()
    private var collectionPage = CatalogPage()
    private var searchPage = CatalogPage()
    private var setting = RoomSetting.APPEARANCE
    private var playlistSearch: EditText? = null
    private var catalogBrowser: CatalogBrowser? = null
    private var catalogBrowserState: CatalogBrowserState? = null
    private var radioQuery = ""
    private var refreshRadioControls: (() -> Unit)? = null
    private var playlistRefreshGeneration: Int? = null
    private var catalogPage: CatalogPage
        get() = if (screen == Screen.SEARCH) searchPage else collectionPage
        set(value) {
            if (screen == Screen.SEARCH) searchPage = value else collectionPage = value
        }
    private lateinit var content: LinearLayout
    private lateinit var retry: Button
    private lateinit var status: TextView
    private var presenceError = false
    private var playerPanel: PlayerPanel? = null
    private var insightsPanel: PlaybackInsights? = null
    private var insightsRequestGeneration: Int? = null
    private var playingBar: NowPlayingBar? = null
    private var renderedRadio = false
    private data class DownloadNotice(val roomId: String, val summary: String, val songs: List<JSONObject>,
                                      val feedKeys: List<String>)
    private enum class DownloadGroup(val label: String) {
        REVIEW("Needs review"), READY("Ready to play"), MISSING("No copy found")
    }
    private enum class DownloadSource(val title: String) {
        UPDATES("Download updates"), REQUESTS("Requests needing attention"), WEEKLY("Weekly Discovery needs attention")
    }
    private enum class DownloadAction(val label: String, val icon: Mark) {
        PLAY("Play", Mark.PLAY), REVIEW("Review copies", Mark.BROWSE), CHOOSE("Choose format", Mark.SETTINGS),
        FORMAT("Change format", Mark.SETTINGS), RETRY("Retry", Mark.REFRESH), RESTORE("Download again", Mark.DOWNLOAD)
    }
    private var downloadNotice: DownloadNotice? = null
    private var downloadNoticeSlot: LinearLayout? = null
    private var downloadActivityLoading = false
    private var refreshDownloads: (() -> Unit)? = null
    private var apkInstallResult: ((Boolean) -> Unit)? = null
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
                if (active && !isDestroyed && room?.id == target.id && screen in setOf(Screen.SONGS, Screen.SEARCH) && library.local) show()
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
                if (hasWindowFocus()) refreshDownloads?.invoke()
            }
            handler.postDelayed(this, 20000)
        }
    }
    private val poll = Runnable { if (active && room != null) refreshPlayer() }
    private var pageScroll: ScrollView? = null
    private val refreshDiscover = object : Runnable {
        override fun run() {
            if (!active || screen !in setOf(Screen.DISCOVER, Screen.WEEKLY)) return
            if ((ui.eInk || screen == Screen.WEEKLY) && !hasWindowFocus()) scheduleDiscoveryRefresh(8_000)
            else show(listPosition(), pageScroll?.scrollY)
        }
    }
    private val playbackUpdates = object : BroadcastReceiver() {
        override fun onReceive(context: Context, intent: Intent) {
            if (Room.load(this@RoomActivity) == null) {
                room = null
                show()
            } else room?.let { target ->
                val snapshot = radioSnapshot(target.id) ?: phonePlaybackSnapshot(target) ?: RoomPlayback.recentRoomSnapshot(target)
                if (snapshot != null) renderPlayer(snapshot)
                else if (renderedRadio) {
                    renderPlayer(JSONObject())
                    refreshPlayer()
                }
            }
            refreshRadioControls?.invoke()
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
            registerReceiver(playbackUpdates, IntentFilter(RoomPlayback.UPDATE), RECEIVER_NOT_EXPORTED)
        } else {
            @Suppress("DEPRECATION")
            registerReceiver(playbackUpdates, IntentFilter(RoomPlayback.UPDATE))
        }
        if (pairOnly) return
        handler.post(presence)
        if (room != null && retry.visibility == View.VISIBLE) show()
        else if (room != null && screen in setOf(Screen.DISCOVER, Screen.WEEKLY, Screen.ACTIVITIES, Screen.DOWNLOADS, Screen.RADIO)) {
            show(listPosition(), pageScroll?.scrollY)
        }
        else if (room != null) refreshPlayer()
    }

    override fun onStop() {
        active = false
        authorizedExternalRadio = null
        handler.removeCallbacks(presence)
        handler.removeCallbacks(poll)
        handler.removeCallbacks(refreshDiscover)
        unregisterReceiver(playbackUpdates)
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
        val previous = sectionTrail.removeLastOrNull()
        if (previous != null) {
            screen = previous.screen
            show(previous.listPosition, previous.scrollOffset)
        } else if (screen != Screen.LISTEN && room != null) {
            selectTab(Screen.LISTEN)
        } else super.onBackPressed()
    }

    private fun listPosition(): Pair<Int, Int>? {
        fun find(view: View): ListView? = when (view) {
            is ListView -> view
            is LinearLayout -> (0 until view.childCount).firstNotNullOfOrNull { find(view.getChildAt(it)) }
            else -> null
        }
        val list = find(content) ?: return null
        return list.firstVisiblePosition to (list.getChildAt(0)?.top ?: 0)
    }

    private fun openSection(destination: Screen) {
        sectionTrail.addLast(Location(screen, listPosition(), pageScroll?.scrollY))
        screen = destination
        show()
    }

    private fun selectTab(destination: Screen) {
        if (screen == destination && sectionTrail.isEmpty()) return
        sectionTrail.clear()
        screen = destination
        show()
    }

    private fun shortcut(destination: Screen, subtitle: String = "") =
        Entry(destination.title, subtitle, mark = destination.icon) { openSection(destination) }

    private fun deviceFiles() = startActivity(Intent(this, CardActivity::class.java))

    private fun campingMode() = startActivity(Intent(this, CardSaveActivity::class.java).putExtra(OPEN_CAMPING, true))

    private fun openCollection(page: CatalogPage) {
        collectionPage = page
        openSection(Screen.SONGS)
    }

    private fun placeIndex(entries: List<Entry>, listPosition: Pair<Int, Int>? = null) {
        val list = ui.index(entries)
        content.addView(list, LinearLayout.LayoutParams(-1, 0, 1f))
        listPosition?.let { (first, top) -> list.setSelectionFromTop(first, top) }
    }

    private fun showScrollable(body: View) = ScrollView(this).apply {
        if (ui.eInk) overScrollMode = View.OVER_SCROLL_NEVER
        addView(body)
        pageScroll = this
        content.addView(this, LinearLayout.LayoutParams(-1, 0, 1f))
    }

    private fun LinearLayout.part(view: View) =
        addView(view, LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f))

    private fun message(text: String = "") {
        if (status.text.toString() != text) status.text = text
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

    private fun settingsPage(target: Room) {
        if (setting == RoomSetting.EXTENSIONS) {
            content.addView(roomExtensionsPanel(target, ui), LinearLayout.LayoutParams(-1, 0, 1f))
            return
        }
        val body = ui.column(8)
        content.addView(ScrollView(this).apply {
            if (ui.eInk) overScrollMode = View.OVER_SCROLL_NEVER
            addView(body)
        }, LinearLayout.LayoutParams(-1, 0, 1f))
        when (setting) {
            RoomSetting.APPEARANCE -> {
                if (RoomTheme.isKompakt) body.addView(ui.label(RoomTheme.E_INK.label))
                else body.addView(ui.button("Theme · ${ui.theme.label}") { chooseAppearance() })
            }
            RoomSetting.PLAYBACK -> playbackSettings(body, target)
            RoomSetting.OFFLINE -> offlineSettings(body, target)
            RoomSetting.CONNECTION -> connectionSettings(body, target)
            RoomSetting.EXTENSIONS -> Unit
            RoomSetting.UPDATES -> body.addView(ui.button("Check for APK updates") { showApkUpdate() })
            RoomSetting.INSIGHTS -> body.addView(ui.button("Show playback insights") { openSection(Screen.INSIGHTS) })
        }
    }

    private fun playbackSettings(body: LinearLayout, target: Room) {
        lateinit var audio: Button
        audio = ui.button("Mobile audio · ${MobileAudio.load(this).label}") {
            MobileAudio.choose(this) { audio.text = "Mobile audio · ${it.label}" }
        }
        body.addView(audio)
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
        body.addView(buffer)
        if (phone()?.local != true) body.addView(ui.button("Playback outputs") {
            load(target::session) { snapshot ->
                val outputs = snapshot.optJSONArray("outputs")
                if (outputs == null || outputs.length() == 0) message("No playback outputs are available.")
                else ui.showPlaybackDevices(this, target.id, outputs) { ids ->
                    send("outputs", JSONObject().put("devices", JSONArray(ids)))
                }
            }
        })
    }

    private fun offlineSettings(body: LinearLayout, target: Room) {
        val library = PhoneLibrary.get(this, target.replicaId)
        fun mode(local: Boolean) {
            stopService(Intent(this, RoomPlayback::class.java))
            library.local = local
            resetLibrary()
            show()
        }
        if (!library.enabled) {
            body.addView(ui.button("Keep an offline library", true) {
                AlertDialog.Builder(this).setTitle("Copy this room to your phone?")
                    .setMessage("Copies the catalog and original audio into phone storage. Songs you add here sync back to the room. Allow offline sync in the server’s Devices settings first.")
                    .setNegativeButton("Cancel", null).setPositiveButton("Enable and sync") { _, _ ->
                        library.enabled = true
                        mode(true)
                        sync(target, library)
                    }.show()
            })
            return
        }
        val summary = ui.label("", 15f, ui.secondary)
        val syncButton = ui.button("Sync now") { sync(target, library) }
        val importButton = ui.button("Add song from phone") {
            startActivityForResult(Intent(Intent.ACTION_OPEN_DOCUMENT).setType("audio/*")
                .addCategory(Intent.CATEGORY_OPENABLE), 81)
        }
        body.addView(summary)
        body.addView(syncButton)
        body.addView(importButton)
        body.addView(ui.line())
        body.addView(ui.button(if (library.local) "Listen with the room" else "Listen on this phone") { mode(!library.local) })
        body.addView(ui.button("Turn off offline sync") { library.enabled = false; mode(false) })

        val update = object : Runnable {
            override fun run() {
                if (!body.isAttachedToWindow) return
                val (count, bytes) = library.stats()
                val size = android.text.format.Formatter.formatShortFileSize(this@RoomActivity, bytes)
                val progress = if (ReplicaSync.busy) ReplicaSync.progress else library.options.getString("status", "").orEmpty()
                val canImport = library.options.contains("collection")
                val text = listOf(
                    "$count saved · $size",
                    progress,
                    if (canImport) "" else "Connect once to enable phone imports.",
                ).filter(String::isNotEmpty).joinToString("\n")
                if (summary.text.toString() != text) summary.text = text
                syncButton.isEnabled = !ReplicaSync.busy
                importButton.isEnabled = !ReplicaSync.importing && canImport
                if (body.hasWindowFocus()) handler.postDelayed(this, if (ui.eInk) 4_000L else 1_000L)
            }
        }
        val focusChanged = android.view.ViewTreeObserver.OnWindowFocusChangeListener { focused ->
            handler.removeCallbacks(update)
            if (focused) update.run()
        }
        fun watch() {
            body.viewTreeObserver.addOnWindowFocusChangeListener(focusChanged)
            update.run()
        }
        body.addOnAttachStateChangeListener(object : View.OnAttachStateChangeListener {
            override fun onViewAttachedToWindow(view: View) = watch()
            override fun onViewDetachedFromWindow(view: View) {
                body.viewTreeObserver.removeOnWindowFocusChangeListener(focusChanged)
                handler.removeCallbacks(update)
            }
        })
        if (body.isAttachedToWindow) watch()
    }

    private fun connectionSettings(body: LinearLayout, target: Room) {
        val library = PhoneLibrary.get(this, target.replicaId)
        body.addView(ui.label(target.origin).apply { setTextIsSelectable(true) })
        body.addView(ui.label(target.network.label, 15f, ui.secondary))
        if (library.options.contains("enabled")) body.addView(ui.button("Restore room access") {
            if (ReplicaSync.busy || ReplicaSync.importing) {
                Toast.makeText(this, "Wait for the current transfer to finish.", Toast.LENGTH_SHORT).show()
            } else restoreAccess(library)
        })
        body.addView(ui.button("Disconnect") {
            if (ReplicaSync.busy || ReplicaSync.importing) {
                Toast.makeText(this, "Turn off sync and wait for the current transfer to finish.", Toast.LENGTH_LONG).show()
                return@button
            }
            fun disconnect() {
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
        })
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

    @Suppress("DEPRECATION")
    internal fun startApkInstall(intent: Intent, onResult: (Boolean) -> Unit) {
        startActivityForResult(intent, APK_INSTALL_REQUEST)
        apkInstallResult = onResult
    }

    @Deprecated("Uses the platform document picker and installer")
    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        super.onActivityResult(requestCode, resultCode, data)
        if (requestCode == APK_INSTALL_REQUEST) {
            val onResult = apkInstallResult
            apkInstallResult = null
            onResult?.invoke(resultCode == RESULT_OK)
            return
        }
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

    private fun show(listPosition: Pair<Int, Int>? = null, scrollOffset: Int? = null) {
        catalogBrowser?.let { catalogBrowserState = it.snapshot() }
        catalogBrowser = null
        generation++
        pageScroll = null
        playlistSearch = null
        handler.removeCallbacks(poll)
        handler.removeCallbacks(refreshDiscover)
        playerPanel = null
        insightsPanel = null
        playingBar = null
        renderedRadio = false
        downloadNoticeSlot = null
        refreshDownloads = null
        refreshRadioControls = null
        val outer = ui.screen().apply { isFocusableInTouchMode = true }
        setContentView(outer)
        val target = room
        val local = phone()?.local == true
        val parent = sectionTrail.lastOrNull()?.screen?.takeIf { target != null }
        val heading = when {
            target == null -> "Connect to a room"
            screen == Screen.SONGS -> collectionPage.title
            screen == Screen.SETTING -> setting.title
            else -> screen.title
        }
        outer.addView(ui.row(8).apply {
            if (parent != null) addView(ui.iconButton(
                if (screen == Screen.PLAYER) "Close now playing" else "Back to ${parent.title}",
                if (screen == Screen.PLAYER) Mark.CLOSE else Mark.BACK, outlined = false
            ) { onBackPressed() })
            part(ui.heading(heading).apply { maxLines = 2 })
            if (target != null && screen in listOf(Screen.LISTEN, Screen.SEARCH, Screen.LIBRARY)) {
                addView(ui.iconButton("Settings", Mark.SETTINGS, outlined = false) { openSection(Screen.SETTINGS) })
            } else if (target == null && !RoomTheme.isKompakt) {
                addView(ui.iconButton("Appearance", Mark.SETTINGS, outlined = false) { chooseAppearance() })
            }
        })
        status = ui.label("", 15f, ui.secondary).apply {
            accessibilityLiveRegion = View.ACCESSIBILITY_LIVE_REGION_POLITE
        }
        outer.addView(status)
        if (target != null && !local && screen == Screen.DOWNLOADS) {
            downloadNoticeSlot = ui.column().also(outer::addView)
            renderDownloadNotice(target)
        }
        presenceError = false
        retry = ui.button("Retry") { show() }.apply { visibility = View.GONE }
        if (parent != null) outer.addView(retry, LinearLayout.LayoutParams(-2, -2))
        else outer.addView(retry)
        message()
        content = ui.column()
        outer.addView(content, LinearLayout.LayoutParams(-1, 0, 1f))
        if (target == null) {
            connect()
            return
        }
        if (screen != Screen.PLAYER) {
            val bar = NowPlayingBar(this, ui, { openSection(Screen.PLAYER) }) { sendTransport(it) }
            playingBar = bar
            outer.addView(bar, LinearLayout.LayoutParams(-1, -2))
            val selected = sectionTrail.firstOrNull()?.screen ?: screen
            outer.addView(ui.navigation(listOf(Screen.LISTEN, Screen.SEARCH, Screen.LIBRARY).map { tab ->
                RoomUi.NavigationTab(tab.title, tab == selected) { selectTab(tab) }
            }))
        }
        when (screen) {
            Screen.LISTEN -> listenHome(local, listPosition)
            Screen.LIBRARY -> libraryHome(local, listPosition)
            Screen.SEARCH, Screen.SONGS -> library(listPosition)
            Screen.PLAYLISTS -> playlists(target, listPosition)
            Screen.GENRES -> genres(target, listPosition)
            Screen.MOODS -> moods(target, listPosition)
            Screen.SETTINGS -> content.addView(ui.settingsIndex { page ->
                setting = page
                if (page == RoomSetting.INSIGHTS) openSection(Screen.INSIGHTS)
                else openSection(Screen.SETTING)
            }, LinearLayout.LayoutParams(-1, 0, 1f))
            Screen.SETTING -> settingsPage(target)
            Screen.ACTIVITIES -> activityMixes(target, scrollOffset)
            Screen.CATALOGS -> load({ target.get("/api/discovery/sources") }) { sources ->
                val browser = CatalogBrowser(this, ui, sources, { query, onCall, done ->
                    load({ target.searchCatalog(query, onCall) }, failed = { done(Result.failure(it)) }, executor = catalogIo, reportErrors = false) {
                        done(Result.success(it))
                    }
                }, { pickPlaylist(target, it) }, { item, done ->
                    load({ target.importCatalogSong(item) }, failed = { done(Result.failure(it)) }, executor = catalogIo, reportErrors = false) {
                        done(Result.success(it))
                    }
                }, { id -> requestCopy(target, id) }, restored = catalogBrowserState)
                catalogBrowser = browser
                content.addView(browser, LinearLayout.LayoutParams(-1, 0, 1f))
            }
            Screen.DISCOVER -> deepDive(target, listPosition)
            Screen.WEEKLY -> weeklyDiscovery(target, scrollOffset)
            Screen.DOWNLOADS -> downloads(target, scrollOffset)
            Screen.RADIO -> radio(target, listPosition)
            Screen.INSIGHTS -> {
                val panel = PlaybackInsights(this, ui)
                insightsPanel = panel
                content.addView(panel)
            }
            Screen.PLAYER -> {
                val favorite = if (phone()?.local == true) null
                    else { song: FavoriteSong, liked: Boolean, done: (Boolean?) -> Unit ->
                        saveFavorite(target, song, liked, done)
                    }
                playerPanel = PlayerPanel(this, ui, target, ::sendTransport,
                    addToPlaylist = if (phone()?.local == true) null else { item -> pickPlaylist(target, SongDraft.from(item, catalogTrack = true)) },
                    saveFavorite = favorite, positionOpened = ::refreshPlayer) {
                    selectTab(Screen.LIBRARY)
                }
                content.addView(playerPanel, LinearLayout.LayoutParams(-1, -1))
            }
        }
        if (active) refreshPlayer()
    }

    private fun saveFavorite(target: Room, song: FavoriteSong, liked: Boolean, done: (Boolean?) -> Unit) {
        val requestingScreen = generation
        val completed: (Result<Boolean>) -> Unit = { result ->
            if (!isDestroyed && room?.id == target.id && room?.origin == target.origin) {
                if (generation == requestingScreen) {
                    done(result.getOrNull())
                    result.exceptionOrNull()?.let { error ->
                        if (error is PairingRequired) pairAgain()
                        else message(error.message ?: "Couldn’t save the favorite. Try again.")
                    }
                } else if (result.isSuccess && screen == Screen.SONGS && collectionPage.filter == "favorites") show()
            }
        }
        if (!RoomPlayback.saveRoomFavorite(target, song, liked, completed)) {
            io.execute {
                val result = runCatching { target.setFavorite(song, liked) }
                runOnUiThread { completed(result) }
            }
        }
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
        screen = Screen.LISTEN
        sectionTrail.clear()
        collectionPage = CatalogPage()
        searchPage = CatalogPage()
        catalogBrowser = null
        catalogBrowserState = null
        downloadNotice = null
        radioQuery = ""
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
                if (previous == null) {
                    preferences.edit().putStringSet(target.id, rows.map { it.getString("key") }.toSet()).apply()
                    return@runOnUiThread
                }
                val pending = rows.filter { it.getString("key") !in previous }
                val feedKeys = rows.map { it.getString("key") }
                val old = downloadNotice?.takeIf { it.roomId == target.id }
                if (pending.map { it.getString("key") } == old?.songs.orEmpty().map { it.getString("key") }) {
                    downloadNotice = old?.copy(songs = pending, feedKeys = feedKeys)
                    return@runOnUiThread
                }
                val summary = when (pending.size) {
                    0 -> ""
                    1 -> "${pending.first().optString("title")} · ${downloadGroup(pending.first()).label}"
                    else -> {
                        val ready = pending.count { downloadGroup(it) == DownloadGroup.READY }
                        val missing = pending.count { downloadGroup(it) == DownloadGroup.MISSING }
                        val review = pending.size - ready - missing
                        listOfNotNull("$ready ready to play".takeIf { ready > 0 },
                            "$missing no copy found".takeIf { missing > 0 },
                            "$review ${if (review == 1) "needs" else "need"} review".takeIf { review > 0 }).joinToString(" · ")
                    }
                }
                downloadNotice = if (pending.isEmpty()) null else DownloadNotice(target.id, summary, pending, feedKeys)
                renderDownloadNotice(target)
            }
        }
    }

    private fun downloadGroup(row: JSONObject) = when (row.optString("request_state")) {
        "ready" -> DownloadGroup.READY
        "no_matches" -> DownloadGroup.MISSING
        "" -> when (row.optString("status")) {
            "downloaded" -> DownloadGroup.READY
            "not_found" -> DownloadGroup.MISSING
            else -> DownloadGroup.REVIEW
        }
        else -> DownloadGroup.REVIEW
    }

    private fun requestNeedsAttention(row: JSONObject) = when (row.optString("request_state")) {
        "choose_format", "choose_copy", "copy_unavailable", "no_matches", "search_failed",
        "verification_failed", "copy_missing" -> true
        "" -> row.optString("status") in CHANGEABLE_FORMAT || row.optString("status") == "missing"
        else -> false
    }

    private fun downloadActions(row: JSONObject, source: DownloadSource): List<DownloadAction> = when {
        downloadGroup(row) == DownloadGroup.READY -> listOf(DownloadAction.PLAY)
        !requestNeedsAttention(row) -> emptyList()
        row.needsCopyReview() -> listOf(DownloadAction.REVIEW)
        row.optString("request_state") == "copy_missing" || row.optString("status") == "missing" -> listOf(DownloadAction.RESTORE)
        row.optString("status") == "waiting_format" -> listOf(DownloadAction.CHOOSE)
        source != DownloadSource.UPDATES && row.optString("status") in setOf("error", "rejected") ->
            listOf(DownloadAction.RETRY, DownloadAction.FORMAT)
        else -> listOf(DownloadAction.FORMAT)
    }

    private fun renderDownloadNotice(target: Room) {
        val slot = downloadNoticeSlot ?: return
        slot.removeAllViews()
        val notice = downloadNotice?.takeIf { it.roomId == target.id } ?: return
        slot.addView(ui.button(notice.summary) {
            val unread = downloadNotice?.takeIf { it.roomId == target.id } ?: return@button
            val preferences = getSharedPreferences("download_activity", MODE_PRIVATE)
            val seen = preferences.getStringSet(target.id, null).orEmpty()
            val recent = unread.songs.map { it.getString("key") } + unread.feedKeys.filter(seen::contains)
            val remembered = (recent + seen).distinct().take(96).toSet()
            preferences.edit().putStringSet(target.id, remembered).apply()
            downloadNotice = null
            renderDownloadNotice(target)
            showDownloadUpdates(target, unread.songs)
        }.apply {
            contentDescription = "${notice.summary} · Open download updates"
            setCompoundDrawablesRelative(null, null, ui.mark(Mark.FORWARD, 17, ui.accentInk), null)
        }, LinearLayout.LayoutParams(-1, -2).apply { bottomMargin = ui.dp(8) })
        slot.announceForAccessibility(notice.summary)
    }

    private fun showDownloadUpdates(target: Room, songs: List<JSONObject>, source: DownloadSource = DownloadSource.UPDATES,
                                    onChanged: () -> Unit = {}) {
        val style = SongDetailsStyle(ui)
        val list = style.column()
        val dialog = style.dialog(this, source.title, ScrollView(this).apply { addView(list) },
            closeLabel = if (source == DownloadSource.UPDATES) "Close download updates" else "Close attention list")
        val feedback = ui.label("", 14f, ui.secondary).apply {
            accessibilityLiveRegion = View.ACCESSIBILITY_LIVE_REGION_POLITE
            visibility = View.GONE
        }
        val sections = ui.column()
        list.addView(feedback)
        list.addView(sections)
        val requests = songs.mapNotNull { row ->
            val id = row.optLong("id").takeIf { it > 0 } ?: row.optString("key").substringBefore('/').toLongOrNull()
            id?.let { it to row }
        }.toMap(LinkedHashMap())
        fun updateFeedback(text: String) {
            feedback.text = text
            feedback.visibility = if (text.isEmpty()) View.GONE else View.VISIBLE
        }
        var loading = false
        var changed = false
        dialog.setOnDismissListener { if (changed && !isDestroyed && room?.id == target.id) onChanged() }
        if (requests.isEmpty()) updateFeedback("No requests need attention.")
        fun render() {
            sections.removeAllViews()
            val grouped = requests.entries.groupBy { downloadGroup(it.value) }
            for (group in DownloadGroup.entries) {
                val items = grouped[group] ?: continue
                sections.addView(ui.heading(group.label, 17f).apply { setPadding(0, ui.dp(8), 0, ui.dp(4)) })
                for ((id, notice) in items) {
                    val title = notice.getString("title")
                    val actions = downloadActions(notice, source)
                    val info = ui.column().apply {
                        addView(ui.heading(title, 15f))
                        notice.optString("artist").takeIf(String::isNotBlank)?.let { addView(ui.label(it, 13f, ui.secondary)) }
                        addView(ui.row().apply { addView(ui.statusChip(notice.copyStatusLabel())) })
                    }
                    fun reconcile(row: JSONObject?, status: String) {
                        changed = true
                        if (row != null && downloadActions(row, source).isNotEmpty() &&
                            (source == DownloadSource.UPDATES || requestNeedsAttention(row))) requests[id] = row
                        else requests.remove(id)
                        render()
                        updateFeedback("$title · $status")
                    }
                    fun activate(action: DownloadAction) {
                        if (loading) return
                        loading = true
                        updateFeedback("Loading $title…")
                        io.execute {
                            val result = runCatching {
                                val row = target.requestById(id)
                                val retry = action == DownloadAction.RETRY || action == DownloadAction.RESTORE
                                val updated = if (retry && row != null && action in downloadActions(row, source) &&
                                    notice.has("format_revision") && notice.optLong("format_revision") == row.optLong("format_revision"))
                                    target.retryCopy(row, restoreMissing = action == DownloadAction.RESTORE) else null
                                row to updated
                            }
                            runOnUiThread {
                                loading = false
                                if (!dialog.isShowing || isDestroyed || room?.id != target.id) return@runOnUiThread
                                result.onFailure { updateFeedback(it.message ?: "Couldn’t load the request. Try again.") }
                                result.onSuccess { (row, updated) ->
                                    updateFeedback("")
                                    when {
                                        updated != null -> reconcile(updated.optJSONObject("request"),
                                            updated.optString("status", "Queued for retry"))
                                        row == null -> reconcile(null, "Request unavailable")
                                        action !in downloadActions(row, source) || action == DownloadAction.RETRY ||
                                            action == DownloadAction.RESTORE -> reconcile(row, row.copyStatusLabel())
                                        action == DownloadAction.PLAY && row.optString("audio").isNotBlank() -> {
                                            val trackId = row.optInt("track_index")
                                            if (trackId > 0) send("play", JSONObject().put("track_id", trackId).put("playlist", "all"))
                                            else send("play_request", JSONObject().put("request_id", id))
                                            dialog.dismiss()
                                        }
                                        action == DownloadAction.PLAY -> reconcile(null, "Copy unavailable")
                                        else -> chooseDiscoveryFormat(row, target) { next -> reconcile(next, next.copyStatusLabel()) }
                                    }
                                }
                            }
                        }
                    }
                    val controls = ui.row(6).apply {
                        for (action in actions) {
                            val label = when (action) {
                                DownloadAction.PLAY, DownloadAction.RETRY -> "${action.label} $title"
                                else -> "${action.label} for $title"
                            }
                            addView(if (source == DownloadSource.UPDATES) ui.iconButton(label, action.icon) { activate(action) }
                                else ui.button(action.label) { activate(action) }.apply { contentDescription = label })
                        }
                    }
                    sections.addView(ui.line())
                    sections.addView(if (actions.size > 1) ui.column(4).apply { addView(info); addView(controls) }
                        else ui.row(6).apply { part(info); addView(controls) })
                }
            }
        }
        render()
        dialog.show()
    }

    private fun downloads(target: Room, scrollOffset: Int?) {
        val page = ui.column(8)
        val scroll = showScrollable(page)
        var displayed: String? = null
        var loading = false
        fun render(result: JSONObject, refresh: () -> Unit) {
            val rows = result.getJSONArray("requests")
            val requests = (0 until rows.length()).map(rows::getJSONObject)
            val pending = requests.filter { it.copyPending() }
            val attention = requests.filter(::requestNeedsAttention)
            val recent = result.optJSONArray("downloads") ?: JSONArray()

            page.removeAllViews()
            page.addView(ui.button("Refresh downloads", action = refresh))
            page.addView(ui.heading("Needs attention", 18f))
            if (attention.isEmpty()) page.addView(ui.label("No requests need attention.", 14f, ui.secondary))
            else page.addView(ui.button("Review requests (${attention.size})", true) {
                showDownloadUpdates(target, attention, DownloadSource.REQUESTS, onChanged = refresh)
            })
            page.addView(ui.heading("In progress", 18f))
            if (pending.isEmpty()) page.addView(ui.label("No active downloads in recent requests.", 14f, ui.secondary))
            for (request in pending) {
                page.addView(ui.line())
                page.addView(ui.column(2).apply {
                    addView(ui.label(request.getString("title"), 17f).apply { typeface = ui.headline })
                    addView(ui.label(listOf(request.optString("artist"), request.copyStatusLabel())
                        .filter(String::isNotBlank).joinToString(" · "), 14f, ui.secondary))
                })
            }
            page.addView(ui.heading("Recent downloads", 18f))
            if (recent.length() == 0) page.addView(ui.label("No recent downloads.", 14f, ui.secondary))
            else page.addView(recentDownloads(recent))
        }
        fun refresh(interactive: Boolean) {
            if (loading || screen != Screen.DOWNLOADS) return
            loading = true
            if (interactive) message(if (displayed == null) "Loading downloads…" else "Refreshing downloads…")
            load({ target.get("/api/requests") }, finished = { loading = false }, reportErrors = false,
                failed = { error ->
                    if (interactive && error !is PairingRequired) {
                        message(error.message ?: "Couldn’t load downloads. Try again.")
                        if (displayed == null) retry.apply {
                            text = "Retry"
                            setOnClickListener { refresh(true) }
                            visibility = View.VISIBLE
                        }
                    }
                }) { result ->
                val snapshot = result.toString()
                if (interactive || displayed == null) {
                    message()
                    retry.visibility = View.GONE
                }
                if (snapshot != displayed) {
                    val previousOffset = if (displayed == null) scrollOffset ?: 0 else scroll.scrollY
                    render(result) { refresh(true) }
                    if (previousOffset > 0) scroll.post { scroll.scrollTo(0, previousOffset) }
                    displayed = snapshot
                }
            }
        }
        refreshDownloads = { refresh(false) }
        refresh(true)
    }

    private fun recentDownloads(tracks: JSONArray) = ui.column(6).apply {
        for (index in 0 until tracks.length()) {
            val track = tracks.getJSONObject(index)
            val title = track.getString("title")
            addView(ui.line())
            addView(ui.row(6).apply {
                part(ui.column(2).apply {
                    addView(ui.label(title, 17f).apply { typeface = ui.headline; maxLines = 2 })
                    addView(ui.label(track.optString("artist"), 13f, ui.secondary).apply { maxLines = 1 })
                })
                addView(ui.iconButton("Play $title", Mark.PLAY) {
                    val catalogId = track.optInt("track_index")
                    if (catalogId > 0) send("play", JSONObject().put("track_id", catalogId).put("playlist", "all"))
                    else send("play_request", JSONObject().put("request_id", track.getInt("id")))
                })
            })
        }
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
                            setResult(RESULT_OK)
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
        if (!pairOnly) {
            form.addView(ui.button("Device files", action = ::deviceFiles))
            form.addView(ui.button("Check for APK updates") { showApkUpdate() })
        }
        link.imeOptions = android.view.inputmethod.EditorInfo.IME_ACTION_GO
        link.setOnEditorActionListener { _, action, _ ->
            if (action != android.view.inputmethod.EditorInfo.IME_ACTION_GO) return@setOnEditorActionListener false
            connectButton.performClick()
            true
        }
    }

    private fun listenHome(local: Boolean, position: Pair<Int, Int>?) {
        val rows = if (local) listOf(
            Entry("Songs on this phone") { openCollection(CatalogPage()) },
            Entry("Device files", mark = Mark.BROWSE, open = ::deviceFiles)
        ) else listOf(
            shortcut(Screen.WEEKLY, "Your playlist for this week"),
            shortcut(Screen.DISCOVER, "Find related recordings"),
            shortcut(Screen.RADIO, "Stations and continuous mixes"),
            shortcut(Screen.ACTIVITIES, "Music for what you’re doing"),
            shortcut(Screen.MOODS, "Start a mix by mood")
        )
        placeIndex(rows, position)
    }

    private fun libraryHome(local: Boolean, position: Pair<Int, Int>?) {
        val rows = buildList {
            add(Entry("Songs") { openCollection(CatalogPage()) })
            if (!local) {
                add(shortcut(Screen.PLAYLISTS))
                add(Entry("Favorites") { openCollection(CatalogPage("Favorites", filter = "favorites")) })
                add(shortcut(Screen.DOWNLOADS, "Requests and recent copies"))
            }
            add(Entry("Camping mode", "Save music for a trip", open = ::campingMode))
            add(Entry("Device files", open = ::deviceFiles))
            if (!local) {
                add(Entry("Browse more"))
                add(shortcut(Screen.GENRES))
                add(Entry("Recently played") { openCollection(CatalogPage("Recently played", filter = "recent")) })
                add(Entry("Most played") { openCollection(CatalogPage("Most played", filter = "most")) })
                add(Entry("Not downloaded") { openCollection(CatalogPage("Not downloaded", filter = "missing")) })
            }
        }
        placeIndex(rows, position)
    }

    private fun playlists(target: Room, position: Pair<Int, Int>?) {
        load({ target.requireCatalog(); target.get("/api/overview").getJSONArray("playlists") }) { rows ->
            if (rows.length() == 0) message("There are no playlists in this room.")
            placeIndex((0 until rows.length()).map { i ->
                val playlist = RoomPlaylist(rows.getJSONObject(i))
                Entry(playlist.name, if (playlist.linked) playlist.syncLabel else "",
                    mark = if (playlist.linked) Mark.MAGNET else null) {
                    openCollection(CatalogPage(playlist.name, playlist = playlist))
                }
            }, position)
        }
    }

    private fun genres(target: Room, position: Pair<Int, Int>?) {
        load({ target.requireCatalog(); target.get("/api/tracks?limit=1").optJSONArray("genres") ?: JSONArray() }) { tags ->
            if (tags.length() == 0) message("There are no genres in this room.")
            placeIndex((0 until tags.length()).map { i ->
                val name = tags.getString(i)
                Entry(name) { openCollection(CatalogPage(name, genre = name)) }
            }, position)
        }
    }

    private fun refreshPlaylist(target: Room) {
        val selected = collectionPage.playlist ?: return
        // Keep the destination stable while a dialog owns the window or a search is being edited.
        fun visible() = active && screen == Screen.SONGS && hasWindowFocus() &&
            playlistSearch?.text?.toString() == collectionPage.query
        if (!visible() || playlistRefreshGeneration == generation) return
        playlistRefreshGeneration = generation
        load({ target.get("/api/overview").getJSONArray("playlists") },
            finished = { playlistRefreshGeneration = null }, reportErrors = false) { rows ->
            if (!visible()) return@load
            val row = (0 until rows.length()).asSequence().map(rows::getJSONObject)
                .firstOrNull { it.optString("id") == selected.key }
            if (row == null) {
                collectionPage = CatalogPage()
                onBackPressed()
                message("This playlist is no longer available.")
                return@load
            }
            val latest = RoomPlaylist(row)
            if (selected.sameEdition(latest)) return@load
            val position = listPosition()
            val lastOffset = (row.optInt("entries") - 1).coerceAtLeast(0) / 20 * 20
            collectionPage = collectionPage.copy(title = latest.name, playlist = latest,
                offset = minOf(collectionPage.offset, lastOffset))
            show(position)
        }
    }

    private fun library(listPosition: Pair<Int, Int>?) {
        val target = room ?: return
        val page = catalogPage
        val playlist = page.playlist?.key ?: "all"
        val (query, offset) = page.query to page.offset
        val library = phone()
        val local = library?.local == true
        val searching = screen == Screen.SEARCH
        page.playlist?.let { selected ->
            content.addView(ui.playlistHeader(selected,
                play = { send("play_available", JSONObject().put("playlist", selected.key)) },
                add = { addSong(target, selected, SongDraft()) }))
        }
        val placeholder = when {
            searching -> "Song, artist or album"
            page.playlist != null -> "Find in playlist"
            else -> "Find in this list"
        }
        val form = ui.searchBar(this, query, placeholder) {
            catalogPage = page.copy(query = it, offset = 0)
            show()
        }
        content.addView(form.view)
        if (page.playlist != null) playlistSearch = form.field
        if (searching && query.isBlank()) {
            if (!local) placeIndex(listOf(shortcut(Screen.CATALOGS, "Search sources beyond this room")), listPosition)
            return
        }
        val filters = listOf("playlist" to playlist, "genre" to page.genre, "mood" to page.mood, "filter" to page.filter,
            "q" to query, "offset" to offset.toString(), "limit" to "20")
        val facets = if (page.filter == "favorites") listOf("facets" to "download-favorites") else emptyList()
        val params = (filters + facets).joinToString("&") { (k, v) -> "$k=${Room.encode(v)}" }
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
                    } else info()
                }
            } + favoriteDownloads(data)
            val total = data.getInt("total")
            val number = NumberFormat.getIntegerInstance()
            val count = when {
                total == 0 && local && query.isBlank() -> if (ReplicaSync.busy) ReplicaSync.progress
                    else library?.options?.getString("status", "Connect to the room and sync in Settings.")
                total == 0 -> if (query.isBlank()) "No songs in this selection." else "No songs match “$query”."
                total == 1 -> "1 song"
                else -> "${number.format(total)} songs"
            }
            count?.let {
                content.addView(ui.label(it, 14f, ui.secondary))
            }
            placeIndex(rows, listPosition)
            if (total > 20 || offset > 0) {
                val pages = ui.row()
                pages.addView(ui.iconButton("Previous page", Mark.BACK) {
                    catalogPage = page.copy(offset = (offset - 20).coerceAtLeast(0))
                    show()
                }.apply { isEnabled = offset > 0 }, LinearLayout.LayoutParams(ui.dp(48), ui.dp(48)))
                pages.part(ui.label("${number.format(offset + 1)}–${number.format(offset + tracks.length())}", 14f).apply {
                    gravity = android.view.Gravity.CENTER
                })
                pages.addView(ui.iconButton("Next page", Mark.FORWARD) {
                    catalogPage = page.copy(offset = offset + 20)
                    show()
                }.apply { isEnabled = offset + 20 < total }, LinearLayout.LayoutParams(ui.dp(48), ui.dp(48)))
                content.addView(pages)
            }
        }
    }

    private fun favoriteDownloads(page: JSONObject): List<Entry> {
        val downloads = page.optJSONArray("download_favorites") ?: return emptyList()
        return (0 until downloads.length()).mapNotNull { index ->
            val track = downloads.optJSONObject(index) ?: return@mapNotNull null
            val song = FavoriteSong.from(track) as? FavoriteSong.Download ?: return@mapNotNull null
            val duration = track.optInt("duration")
            Entry(track.optString("title"), track.optString("artist"), if (duration > 0) clockTime(duration) else "") {
                send("play_request", JSONObject().put("request_id", song.key))
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
                if (screen == Screen.SONGS && collectionPage.playlist?.key == selected.key) {
                    collectionPage = collectionPage.copy(query = "", offset = 0)
                    show()
                }
                message(it.optString("message", "Song added to the playlist."))
            }
        }
    }

    private fun moods(target: Room, position: Pair<Int, Int>?) {
        load({ target.requireCatalog(); target.get("/api/tracks?limit=1&facets=moods&facets=mood-availability") }) { facets ->
            val moods = facets.optJSONArray("moods") ?: JSONArray()
            val groups = (0 until moods.length()).mapNotNull { RoomMood.read(moods.getJSONObject(it)) }
            if (groups.isEmpty()) {
                message("There are no moods in this room.")
                return@load
            }
            fun playMood(mix: MoodMix, genre: String = "") {
                if (facets.has("ready_genres")) {
                    send("play_mood", mix.fields(genre))
                } else {
                    openCollection(CatalogPage(genre.ifEmpty { mix.title }, genre = genre, mood = mix.first.id))
                }
            }
            fun openMap() {
                var genre = ""
                val map = MoodMap(this, ui, groups) { mix ->
                    if (genre !in mix.genres) genre = ""
                    playMood(mix, genre)
                }
                val body = ui.column(8).apply {
                    addView(ui.button("Choose genre") {
                        val mix = map.selection
                        ui.choices(this@RoomActivity, "Genre", (listOf("") + mix.genres).map { tag ->
                            Entry(tag.ifEmpty { "Whole mix" }, if (tag == genre) "Selected" else "") {
                                genre = tag
                                playMood(mix, genre)
                            }
                        })
                    })
                    addView(ui.label("Tap to play. Drag between moods to blend them.", 14f, ui.secondary))
                    addView(map, LinearLayout.LayoutParams(-1, ui.dp(232)))
                }
                SongDetailsStyle(ui).dialog(this, "Mood Map", body).show()
            }
            placeIndex(groups.map { mood -> Entry(mood.name) { playMood(MoodMix(mood)) } } +
                Entry("Mood Map", "Blend moods", open = ::openMap), position)
        }
    }

    private fun activityMixes(target: Room, scrollOffset: Int?) {
        val panel = ui.column(6)
        val list = ui.column(6)
        panel.addView(list)
        val pending = ui.label("Loading activities…", 14f, ui.secondary)
        panel.addView(pending)
        val scroll = showScrollable(panel)
        var initialOffset = scrollOffset ?: 0
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
                        list.addView(ui.line())
                        list.addView(ui.row(8).apply {
                            part(ui.column(3).apply {
                                addView(ui.label(name, 17f).apply { typeface = ui.headline })
                                addView(label)
                            })
                            addView(button)
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
                if (initialOffset > 0 && rows.isNotEmpty()) {
                    val offset = initialOffset
                    scroll.post { scroll.scrollTo(0, offset) }
                    initialOffset = 0
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

    private fun radio(target: Room, listPosition: Pair<Int, Int>?) {
        content.addView(ui.button("SomaFM settings in browser") {
            try {
                startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(target.origin + "/?setup=radio")))
            } catch (_: android.content.ActivityNotFoundException) {
                message("Open the room’s Radio settings in a browser.")
            }
        }, LinearLayout.LayoutParams(-1, -2).apply { bottomMargin = ui.dp(8) })
        val searchSlot = ui.column()
        content.addView(searchSlot)
        val results = ui.column()
        content.addView(results, LinearLayout.LayoutParams(-1, 0, 1f))
        val stop = ui.button("Stop radio") { send("radio_stop") }.apply { visibility = View.GONE }
        content.addView(stop)
        fun updateControls() {
            val history = RadioHistory.read(this, target.id)
            val label = if (history?.source == RadioSource.YOUTUBE) "Forget station" else "Stop radio"
            if (stop.text != label) stop.text = label
            val visible = if (RoomPlayback.hasRadio || radioSnapshot(target.id) != null) View.VISIBLE else View.GONE
            if (stop.visibility != visible) stop.visibility = visible
        }
        refreshRadioControls = ::updateControls
        updateControls()
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
            updateControls()
            fun tune(choice: RadioChoice) {
                getSystemService(InputMethodManager::class.java).hideSoftInputFromWindow(searchSlot.windowToken, 0)
                when (choice.source) {
                    RadioSource.LIBRARY -> send("radio_start", JSONObject().put("genre", choice.name))
                    RadioSource.LIVE -> send("radio_live", JSONObject().put("name", choice.name))
                    RadioSource.YOUTUBE -> {
                        openRadioVideo(target, RadioHistory(RadioSource.YOUTUBE, choice.name,
                            choice.uri.toString(), choice.name, "YouTube"), onOpen = ::updateControls)
                        return
                    }
                }
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
            filter(radioQuery)
            val list = results.getChildAt(0) as? ListView
            listPosition?.let { (first, top) -> list?.setSelectionFromTop(first, top) }
            val search = ui.searchBar(this, radioQuery, "Genres, mixes or stations") {
                radioQuery = it
                filter(it)
            }
            searchSlot.addView(search.view)
        }
    }

    private fun deepDive(target: Room, position: Pair<Int, Int>?) {
        message("Loading recordings…")
        load({ target.requireCatalog(); target.get("/api/discover") }) { shelf ->
            when (shelf.optString("status")) {
                "building" -> {
                    message("Finding recordings…")
                    scheduleDiscoveryRefresh(5_000)
                }
                "error" -> {
                    message("Recordings couldn’t load.")
                    retry.visibility = View.VISIBLE
                }
                else -> {
                    val tracks = shelf.getJSONArray("tracks")
                    message(if (tracks.length() == 0) "Favorite a song to get recording suggestions." else "")
                    placeIndex((0 until tracks.length()).map { i ->
                        val item = tracks.getJSONObject(i)
                        val note = item.optJSONObject("request")?.copyStatusLabel()?.takeIf(String::isNotBlank)
                            ?: item.optString("version").ifBlank { item.optString("album") }
                        val detail = listOf(item.getString("artist"), note).filter(String::isNotBlank).joinToString(" · ")
                        Entry(item.getString("title"), detail) {
                            load({ target.pickRequest(item) }) { row -> openPick(target, item, row) }
                        }
                    }, position)
                }
            }
        }
    }

    private fun weeklyDiscovery(target: Room, scrollOffset: Int?) {
        fun reload() = show(scrollOffset = pageScroll?.scrollY)
        val actions = ui.row(6).apply {
            gravity = android.view.Gravity.END or android.view.Gravity.CENTER_VERTICAL
            addView(ui.iconButton("Refresh playlist", Mark.REFRESH) { reload() })
        }
        content.addView(actions, LinearLayout.LayoutParams(-1, -2))
        message("Loading this week’s playlist…")
        load({ target.get("/api/discover/weekly") }) { playlist ->
            when (playlist.optString("state")) {
                "building" -> {
                    message("Building this week’s playlist…")
                    scheduleDiscoveryRefresh(5_000)
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
                    fun weeklyRequest(track: JSONObject): JSONObject? {
                        val request = track.optJSONObject("request") ?: return null
                        return JSONObject(request.toString()).put("title", request.optString("title", track.getString("title")))
                            .put("artist", request.optString("artist", track.getString("artist")))
                            .put("musicbrainz_id", request.optString("musicbrainz_id").ifBlank { track.getString("id") })
                    }
                    fun review(track: JSONObject) {
                        weeklyRequest(track)?.let { chooseDiscoveryFormat(it, target) { reload() } }
                    }
                    val readyIds = tracks.mapNotNull(::readyId)
                    message(when {
                        tracks.isEmpty() -> "This week’s playlist is empty."
                        !canRequest && pending.isNotEmpty() -> "Downloads are unavailable in this room."
                        else -> ""
                    })
                    val edition = runCatching { java.time.LocalDate.parse(playlist.optString("week")) }.getOrNull()
                    if (edition != null) {
                        val date = edition.format(java.time.format.DateTimeFormatter.ofPattern("MMM d"))
                        actions.addView(ui.label("Week of $date", 14f, ui.secondary), 0, LinearLayout.LayoutParams(0, -2, 1f))
                    }
                    val batch = ui.row(6)
                    if (readyIds.isNotEmpty()) {
                        batch.part(ui.button("Play saved", solid = true) {
                            if (!editingPlaylist) send("play_requests", JSONObject().put("request_ids", JSONArray(readyIds)))
                        })
                    }
                    if (canRequest && pending.isNotEmpty()) {
                        batch.part(ui.button("Download playlist", solid = readyIds.isEmpty()) {
                            if (editingPlaylist) return@button
                            editingPlaylist = true
                            message("Queuing songs for download…")
                            load({ target.requestWeeklySongs(pending.map { it.getString("id") }) },
                                 finished = { editingPlaylist = false }) { result ->
                                reload()
                                val count = result.optInt("queued")
                                message(if (count == 0) "These songs already have a room request or copy."
                                        else "$count ${if (count == 1) "song" else "songs"} queued for download.")
                            }
                        })
                    }
                    if (batch.childCount > 0) content.addView(batch)
                    playlist.optJSONObject("download_progress")?.let { progress ->
                        val total = progress.optInt("total")
                        val downloaded = progress.optInt("downloaded")
                        val percent = progress.optInt("percent")
                        if (total > 0) {
                            val ids = progress.optJSONArray("attention_ids")?.let { values ->
                                (0 until values.length()).map(values::getLong).toSet()
                            }
                            val attention = tracks.mapNotNull { track ->
                                val row = weeklyRequest(track) ?: return@mapNotNull null
                                val include = ids?.contains(row.optLong("id")) ?: (
                                    (!track.optBoolean("in_library") || row.optString("status") == "missing") &&
                                        readyId(track) == null && !row.copyPending())
                                row.takeIf { include }
                            }
                            content.addView(ui.row(6).apply {
                                part(ui.label("$downloaded of $total downloaded · $percent%", 14f, ui.secondary))
                                if (attention.isNotEmpty()) {
                                    val count = attention.size
                                    val label = "$count ${if (count == 1) "needs" else "need"} attention"
                                    addView(ui.button(label) {
                                        showDownloadUpdates(target, attention, DownloadSource.WEEKLY) {
                                            if (screen == Screen.WEEKLY) reload()
                                        }
                                    })
                                }
                            }, LinearLayout.LayoutParams(-1, -2).apply { topMargin = ui.dp(16) })
                            if (!ui.eInk) content.addView(ProgressBar(this, null, android.R.attr.progressBarStyleHorizontal).apply {
                                max = 100
                                this.progress = percent
                                contentDescription = "Playlist downloads"
                            }, LinearLayout.LayoutParams(-1, ui.dp(12)))
                        }
                        if (progress.optInt("pending") > 0) scheduleDiscoveryRefresh(8_000)
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
                        val id = readyId(track)
                        val menu = buildList {
                            if (!inLibrary && request != null && requestStatus in CHANGEABLE_FORMAT && request.has("format_revision")) {
                                val label = if (request.needsCopyReview()) "Review copies" else "Choose format"
                                add(Entry(label) { review(track) })
                            }
                            if (!inLibrary && request != null && requestStatus in RETRYABLE_COPY && !request.needsCopyReview()) {
                                add(Entry("Retry download") { load({ target.retryCopy(request) }) { reload() } })
                            }
                            if (!inLibrary && (request == null || (!request.needsCopyReview() && request.canReplaceRecording()))) {
                                add(Entry("Other recordings") { chooseRequestVersion(target, track, request) })
                            }
                            val removeKey = track.optString("remove_key")
                            if (removeKey.isNotBlank()) {
                                add(Entry("Remove from Weekly Discovery") {
                                    if (!editingPlaylist) {
                                        editingPlaylist = true
                                        message("Removing song…")
                                        load({ target.removeWeeklySong(track.getString("id"), playlist.getString("week"), removeKey) },
                                             finished = { editingPlaylist = false }) { reload() }
                                    }
                                })
                            }
                        }
                        fun showOptions() = ui.choices(this, title, menu)
                        songs.addView(ui.line())
                        songs.addView(ui.row(8).apply {
                            gravity = android.view.Gravity.CENTER_VERTICAL
                            part(ui.column(2).apply {
                                setPadding(0, ui.dp(8), 0, ui.dp(8))
                                minimumHeight = ui.dp(48)
                                addView(ui.label(title, 17f).apply { typeface = ui.headline })
                                val subtitle = listOf(artist, detail, if (via.isBlank()) "" else "Via $via")
                                    .filter(String::isNotBlank).joinToString(" · ")
                                if (subtitle.isNotBlank()) addView(ui.label(subtitle, 14f, ui.secondary))
                                if (id != null || menu.isNotEmpty()) {
                                    isFocusable = true
                                    setOnClickListener {
                                        if (id != null) send("play_request", JSONObject().put("request_id", id))
                                        else showOptions()
                                    }
                                }
                            })
                            if (menu.isNotEmpty()) addView(ui.iconButton("Options for $title", Mark.MORE, outlined = false) {
                                showOptions()
                            })
                        })
                    }
                    val scroll = showScrollable(songs)
                    if (scrollOffset != null) scroll.post { scroll.scrollTo(0, scrollOffset) }
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
            if (screen == Screen.WEEKLY) scheduleDiscoveryRefresh(8_000)
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
        if (active) {
            val delay = when {
                !ui.eInk || playerPanel?.needsLivePosition == true -> 2_500L
                screen == Screen.PLAYER || screen == Screen.INSIGHTS -> 12_000L
                else -> 20_000L
            }
            handler.postDelayed(poll, delay)
        }
    }

    private fun scheduleDiscoveryRefresh(delay: Long) {
        if (active) handler.postDelayed(refreshDiscover, if (ui.eInk) maxOf(delay, 15_000L) else delay)
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

    private fun phonePlaybackSnapshot(target: Room): JSONObject? = if (phone()?.local == true)
        RoomPlayback.phoneSnapshot(target.id) else RoomPlayback.bufferedSnapshot(target.id)

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
        refreshRadioControls?.invoke()
        refreshInsights(target, radio != null)
        (radio ?: phonePlaybackSnapshot(target))?.let {
            renderPlayer(it)
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
        renderedRadio = snapshot.optBoolean("radio")
        playerPanel?.render(snapshot)
        insightsPanel?.let { panel ->
            panel.renderSource(snapshot)
            if (snapshot.has("queue_length") && !snapshot.optBoolean("radio")) panel.renderQueue(snapshot)
        }
        playingBar?.render(snapshot)
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

    private fun sendTransport(op: String, fields: JSONObject = JSONObject()) {
        if (renderedRadio && op in setOf("pause", "resume", "previous", "next", "seek")) {
            send(op, fields.put("requires_radio", true))
        } else send(op, fields)
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
