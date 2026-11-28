package music.listening.room

import android.app.Activity
import android.content.ComponentName
import android.content.Intent
import android.net.Uri
import android.os.Bundle
import android.os.CancellationSignal
import android.provider.DocumentsContract
import android.provider.DocumentsContract.Document
import android.text.TextUtils
import android.view.Gravity
import android.view.View
import android.view.ViewConfiguration
import android.view.ViewGroup
import android.widget.BaseAdapter
import android.widget.Button
import android.widget.ImageButton
import android.widget.LinearLayout
import android.widget.ListView
import android.widget.ScrollView
import android.widget.TextView
import androidx.media3.common.MediaItem
import androidx.media3.common.PlaybackException
import androidx.media3.common.Player
import androidx.media3.session.MediaController
import androidx.media3.session.SessionToken
import com.google.common.util.concurrent.ListenableFuture
import org.json.JSONObject
import java.util.Locale
import java.util.concurrent.Executor
import java.util.concurrent.Executors

private data class CardEntry(val id: String, val name: String, val folder: Boolean) {
    val playlist get() = !folder && name.endsWith(".m3u8")
    val title get() = if (folder) name else name.substringBeforeLast('.', name)
        .replace(Regex(""" \[([a-f0-9]{20}|[a-f0-9]{64})\]( \(\d+\))?$"""), "")
}

/** A recycled local list and an independent transport that can resume before a folder scan finishes. */
class CardActivity : Activity() {
    private val ui by lazy { RoomUi(this) }
    private val prefs by lazy { getSharedPreferences("card", MODE_PRIVATE) }
    private val io = Executors.newSingleThreadExecutor()
    private val folders = mutableListOf<CardEntry>()
    private var tree: Uri? = null
    private var controller: MediaController? = null
    private var future: ListenableFuture<MediaController>? = null
    private var query: CancellationSignal? = null
    private var pendingPlay: ((MediaController) -> Unit)? = null
    private var bookmark: CardBookmark? = null
    private var resumeQueueId: String? = null
    private var listedFolder: String? = null
    private var songs = emptyList<MediaItem>()
    private var songIndices = emptyMap<String, Int>()
    private var loadingPlaylist = false
    private lateinit var heading: TextView
    private lateinit var status: TextView
    private lateinit var list: ListView
    private lateinit var playingBar: NowPlayingBar
    private lateinit var start: Button
    private lateinit var up: ImageButton
    private lateinit var roomShortcut: ImageButton
    private lateinit var folder: Button

    private val adapter = object : BaseAdapter() {
        var entries = emptyList<CardEntry>()
        override fun getCount() = entries.size
        override fun getItem(position: Int) = entries[position]
        override fun getItemId(position: Int) = position.toLong()

        override fun getView(position: Int, reused: View?, parent: ViewGroup): View {
            val row = (reused as? TextView) ?: ui.label("", 17f).apply {
                minHeight = ui.dp(if (ui.eInk) 48 else 54)
                gravity = Gravity.CENTER_VERTICAL
                val vertical = ui.dp(if (ui.eInk) 4 else 6)
                setPadding(ui.dp(8), vertical, ui.dp(8), vertical)
                maxLines = 2
                ellipsize = TextUtils.TruncateAt.END
            }
            val entry = entries[position]
            val mark = when {
                entry.folder -> ui.controlMark(Mark.FOLDER, 20)
                entry.playlist -> ui.controlMark(Mark.ALBUM, 20)
                else -> null
            }
            row.setCompoundDrawablesRelative(mark, null, if (entry.folder) ui.controlMark(Mark.BROWSE, 16) else null, null)
            row.compoundDrawablePadding = ui.dp(10)
            row.text = entry.title
            row.contentDescription = when {
                entry.folder -> "${entry.title}, folder"
                entry.playlist -> "${entry.title}, playlist"
                else -> null
            }
            return row
        }
    }
    private val listener = object : Player.Listener {
        override fun onEvents(player: Player, events: Player.Events) = controls()
        override fun onPlayerError(error: PlaybackException) {
            showStatus("Could not play this file. Check the card or choose another song.")
        }
    }

    override fun onCreate(saved: Bundle?) {
        super.onCreate(saved)
        ui.theme.applyWindow(this, ui.paper)
        tree = prefs.getString("tree", null)?.let(Uri::parse)
        val body = ui.screen()
        heading = ui.heading("Device files", 23f).apply {
            maxLines = 2
            ellipsize = TextUtils.TruncateAt.END
        }
        up = ui.iconButton("Back to room", Mark.BACK, outlined = false) {
            if (folders.isEmpty()) returnToRoom() else onBackPressed()
        }
        roomShortcut = ui.iconButton("Back to room", Mark.ROOM, outlined = false, action = ::returnToRoom)
        body.addView(ui.header(heading, up, roomShortcut))
        folder = ui.button("Change folder") { chooseFolder() }
        body.addView(ui.row(8).apply {
            addView(folder, LinearLayout.LayoutParams(0, -2, 1f))
            addView(ui.button("Save for offline") {
                startActivity(Intent(this@CardActivity, CardSaveActivity::class.java))
            }, LinearLayout.LayoutParams(0, -2, 1f))
        })
        status = ui.label("", 14f, ui.secondary).apply {
            visibility = View.GONE
            accessibilityLiveRegion = View.ACCESSIBILITY_LIVE_REGION_POLITE
        }
        body.addView(status)
        list = ListView(this).apply {
            if (ui.eInk) {
                overScrollMode = View.OVER_SCROLL_NEVER
                setFriction(ViewConfiguration.getScrollFriction() * 3)
            }
            divider = android.graphics.drawable.ColorDrawable(ui.rule)
            dividerHeight = ui.dp(1)
            adapter = this@CardActivity.adapter
            setOnItemClickListener { _, _, index, _ ->
                if (loadingPlaylist) return@setOnItemClickListener
                val entry = this@CardActivity.adapter.getItem(index)
                if (entry.folder) {
                    folders.add(entry)
                    showFolder()
                } else if (entry.playlist) {
                    openPlaylist(entry)
                } else {
                    val queue = songs
                    val position = songIndices[entry.id] ?: return@setOnItemClickListener
                    resumeQueueId = null
                    startPlayback { it.setMediaItems(queue, position, 0) }
                }
            }
        }
        body.addView(list, LinearLayout.LayoutParams(-1, 0, 1f))
        playingBar = NowPlayingBar(this, ui, ::showNowPlaying) {
            if (!loadingPlaylist && pendingPlay == null) playOrResume()
        }
        body.addView(playingBar)
        start = ui.button("Play folder", true, action = ::playOrResume).apply {
            maxLines = 2
            ellipsize = TextUtils.TruncateAt.END
            visibility = View.GONE
        }
        body.addView(start)
        setContentView(body)
    }

    override fun onStart() {
        super.onStart()
        val selected = prefs.getString("tree", null)?.let(Uri::parse)
        if (selected != tree) {
            tree = selected
            folders.clear()
            listedFolder = null
        }
        bookmark = tree?.let { CardBookmark.load(this, it) }
        if (listedFolder == null && folders.isEmpty()) restoreFolder()
        controls()
        connectController()
        showFolder()
    }

    private fun showStatus(message: String) {
        status.text = message
        status.visibility = if (message.isEmpty()) View.GONE else View.VISIBLE
    }

    private fun connectController() {
        future?.let(MediaController::releaseFuture)
        val pending = MediaController.Builder(this, SessionToken(this, ComponentName(this, CardPlayback::class.java))).buildAsync()
        future = pending
        pending.addListener({
            if (future === pending && !pending.isCancelled) {
                val ready = runCatching { pending.get() }.getOrNull()
                controller = ready
                if (ready != null) {
                    ready.addListener(listener)
                    pendingPlay?.invoke(ready)
                } else if (pendingPlay != null) {
                    showStatus("The card player could not start. Reopen the app to try again.")
                }
                pendingPlay = null
                controls()
            }
        }, Executor { runOnUiThread(it) })
    }

    override fun onStop() {
        query?.cancel()
        query = null
        pendingPlay = null
        controller?.removeListener(listener)
        controller = null
        future?.let(MediaController::releaseFuture)
        future = null
        super.onStop()
    }

    override fun onDestroy() {
        io.shutdownNow()
        super.onDestroy()
    }

    override fun onBackPressed() {
        if (folders.isEmpty()) super.onBackPressed()
        else {
            folders.removeAt(folders.lastIndex)
            showFolder()
        }
    }

    private fun returnToRoom() {
        startActivity(Intent(this, RoomActivity::class.java)
            .addFlags(Intent.FLAG_ACTIVITY_CLEAR_TOP or Intent.FLAG_ACTIVITY_SINGLE_TOP))
        finish()
    }

    private fun chooseFolder() = startActivityForResult(Intent(Intent.ACTION_OPEN_DOCUMENT_TREE)
        .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_GRANT_PERSISTABLE_URI_PERMISSION), 1)

    @Deprecated("Uses the platform folder picker")
    override fun onActivityResult(request: Int, result: Int, data: Intent?) {
        super.onActivityResult(request, result, data)
        if (request != 1 || result != RESULT_OK) return
        val uri = data?.data ?: return
        if (uri.authority != "com.android.externalstorage.documents") {
            showStatus("Choose a Music folder in local storage or on the SD card.")
            return
        }
        try {
            contentResolver.takePersistableUriPermission(uri, Intent.FLAG_GRANT_READ_URI_PERMISSION)
            val old = tree
            tree = uri
            folders.clear()
            if (old != uri) {
                listedFolder = null
                bookmark = null
                pendingPlay = null
                val player = controller
                if (player != null) player.clearMediaItems()
                else pendingPlay = { it.clearMediaItems() }
                prefs.edit().putString("tree", uri.toString()).remove("resume_uri")
                    .remove("resume_title").remove("resume_position").apply()
                if (old != null) runCatching {
                    contentResolver.releasePersistableUriPermission(old, Intent.FLAG_GRANT_READ_URI_PERMISSION)
                }
            }
            controls()
            showFolder()
        } catch (_: SecurityException) {
            showStatus("Select the folder again to allow read access.")
        }
    }

    private fun restoreFolder() {
        val root = tree ?: return
        val saved = bookmark ?: return
        val rootId = DocumentsContract.getTreeDocumentId(root)
        val prefix = if (rootId.endsWith(':')) rootId else "$rootId/"
        val relative = DocumentsContract.getDocumentId(saved.uri).removePrefix(prefix)
        val ancestors = relative.split('/').dropLast(1)
        var path = prefix.trimEnd('/')
        for (name in ancestors) {
            path += if (path.endsWith(':')) name else "/$name"
            folders.add(CardEntry(path, name, true))
        }
    }

    private fun showFolder() {
        query?.cancel()
        query = null
        val root = tree
        val parent = folders.lastOrNull()
        heading.text = parent?.name ?: "Device files"
        val back = if (parent == null) "Back to room" else "Back to parent folder"
        up.contentDescription = back
        up.tooltipText = back
        roomShortcut.visibility = if (parent == null) View.GONE else View.VISIBLE
        folder.text = if (root == null) "Choose Music folder" else "Change folder"
        folder.isSelected = root == null
        if (root == null) {
            showStatus("Select a Music folder on your phone or microSD card.")
            return
        }
        val parentId = parent?.id ?: DocumentsContract.getTreeDocumentId(root)
        if (listedFolder != parentId) {
            updateList(emptyList())
            songs = emptyList()
            songIndices = emptyMap()
            controls()
            showStatus("Reading folder…")
        }
        val signal = CancellationSignal()
        query = signal
        io.execute {
            val result = runCatching {
                val entries = readFolder(root, parentId, signal)
                val audio = entries.filterNot { it.folder || it.playlist }
                val queue = audio.map {
                    val uri = DocumentsContract.buildDocumentUriUsingTree(root, it.id)
                    MediaItem.Builder().setMediaId(uri.toString()).setUri(uri).build()
                }
                Triple(entries, queue, audio.withIndex().associate { it.value.id to it.index })
            }
            runOnUiThread {
                if (query !== signal || isDestroyed) return@runOnUiThread
                result.onSuccess { (entries, queue, indices) ->
                    listedFolder = parentId
                    songs = queue
                    songIndices = indices
                    updateList(entries)
                    completeResumeQueue()
                    controls()
                    showStatus(if (entries.isEmpty()) "No music here yet. Add audio files or choose another folder." else "")
                }.onFailure {
                    listedFolder = null
                    songs = emptyList()
                    songIndices = emptyMap()
                    updateList(emptyList())
                    controls()
                    showStatus("Folder unavailable. Insert the card or select the Music folder again.")
                }
            }
        }
    }

    private fun readFolder(root: Uri, parent: String, signal: CancellationSignal): List<CardEntry> {
        val children = DocumentsContract.buildChildDocumentsUriUsingTree(root, parent)
        val fields = arrayOf(Document.COLUMN_DOCUMENT_ID, Document.COLUMN_DISPLAY_NAME, Document.COLUMN_MIME_TYPE)
        return requireNotNull(contentResolver.query(children, fields, null, null, null, signal)).use { cursor ->
            buildList {
                while (cursor.moveToNext()) {
                    signal.throwIfCanceled()
                    val name = cursor.getString(1) ?: continue
                    if (name.startsWith(".")) continue
                    val type = cursor.getString(2).orEmpty()
                    val folder = type == Document.MIME_TYPE_DIR
                    if (folder || name.matches(ownedPlaylist) || type.startsWith("audio/") ||
                        name.substringAfterLast('.').lowercase(Locale.ROOT) in audioTypes) {
                        add(CardEntry(cursor.getString(0), name, folder))
                    }
                }
            }.sortedWith(compareBy<CardEntry> { !it.folder }.thenBy { !it.playlist }.thenBy(String.CASE_INSENSITIVE_ORDER) { it.name })
        }
    }

    private fun playlistItems(root: Uri, entry: CardEntry, entries: List<CardEntry>): List<MediaItem> {
        val marker = ownedPlaylist.matchEntire(entry.name)?.groupValues?.get(1) ?: return emptyList()
        val byName = entries.filter { !it.folder && !it.playlist }.associateBy { it.name }
        val uri = DocumentsContract.buildDocumentUriUsingTree(root, entry.id)
        return contentResolver.openInputStream(uri)?.bufferedReader()?.use { input ->
            if (input.readLine() != "#EXTM3U" || input.readLine() != "#LISTENING-PLAYLIST") return@use emptyList()
            input.lineSequence().mapNotNull { filename ->
                byName[filename]?.let { track ->
                    val audio = DocumentsContract.buildDocumentUriUsingTree(root, track.id)
                    MediaItem.Builder().setMediaId(audio.toString()).setUri(audio)
                        .setRequestMetadata(MediaItem.RequestMetadata.Builder()
                            .setExtras(Bundle().apply { putString(CardBookmark.PLAYLIST, marker) }).build()).build()
                }
            }.toList()
        }.orEmpty()
    }

    private fun openPlaylist(entry: CardEntry) {
        val root = tree ?: return
        val folderId = listedFolder
        val entries = adapter.entries
        loadingPlaylist = true
        controls()
        io.execute {
            val queue = runCatching { playlistItems(root, entry, entries) }.getOrDefault(emptyList())
            runOnUiThread {
                loadingPlaylist = false
                if (isDestroyed || isFinishing) return@runOnUiThread
                if (folderId == listedFolder && root == tree) {
                    if (queue.isEmpty()) showStatus("No saved songs from this playlist are on the card.")
                    else {
                        resumeQueueId = null
                        startPlayback { it.setMediaItems(queue, 0, 0) }
                    }
                }
                controls()
            }
        }
    }

    private fun resumePlaylist(saved: CardBookmark, marker: String) {
        val root = tree ?: return
        loadingPlaylist = true
        controls()
        io.execute {
            val queue = runCatching {
                val parent = DocumentsContract.getDocumentId(saved.uri).substringBeforeLast('/')
                val entries = readFolder(root, parent, CancellationSignal())
                val manifest = entries.firstOrNull {
                    ownedPlaylist.matchEntire(it.name)?.groupValues?.get(1) == marker
                }
                manifest?.let { playlistItems(root, it, entries) }.orEmpty()
            }.getOrDefault(emptyList())
            runOnUiThread {
                loadingPlaylist = false
                if (isDestroyed || isFinishing || root != tree) return@runOnUiThread
                val id = saved.uri.toString()
                val index = saved.occurrence.takeIf { queue.getOrNull(it)?.mediaId == id }
                    ?: queue.indexOfFirst { it.mediaId == id }
                startPlayback {
                    if (index >= 0) it.setMediaItems(queue, index, saved.position)
                    else it.setMediaItem(saved.item(), saved.position)
                }
            }
        }
    }

    private fun updateList(entries: List<CardEntry>) {
        if (adapter.entries == entries) return
        adapter.entries = entries
        adapter.notifyDataSetChanged()
    }

    private fun startPlayback(select: (MediaController) -> Unit) {
        val start: (MediaController) -> Unit = {
            select(it)
            it.prepare()
            it.play()
        }
        controller?.let(start) ?: run {
            pendingPlay = start
            if (future?.isDone != false) connectController()
        }
        controls()
    }

    private fun completeResumeQueue() {
        val id = resumeQueueId ?: return
        val player = controller ?: return
        val index = songs.indexOfFirst { it.mediaId == id }
        if (index < 0 || player.mediaItemCount != 1 || player.currentMediaItem?.mediaId != id) return
        resumeQueueId = null
        player.addMediaItems(0, songs.take(index))
        player.addMediaItems(songs.drop(index + 1))
    }

    private fun playOrResume() {
        val player = controller
        if (player?.currentMediaItem != null) {
            if (player.playWhenReady) player.pause()
            else {
                if (player.playbackState == Player.STATE_ENDED) player.seekTo(0)
                if (player.playbackState == Player.STATE_IDLE) player.prepare()
                player.play()
            }
            return
        }
        bookmark?.let { saved ->
            saved.playlist?.let { marker ->
                resumePlaylist(saved, marker)
                return
            }
            startPlayback { ready ->
                val index = songs.indexOfFirst { it.mediaId == saved.uri.toString() }
                if (index >= 0) ready.setMediaItems(songs, index, saved.position)
                else {
                    resumeQueueId = saved.uri.toString()
                    ready.setMediaItem(saved.item(), saved.position)
                }
            }
        } ?: run {
            if (songs.isNotEmpty()) startPlayback { it.setMediaItems(songs) }
        }
    }

    private fun controls() {
        val player = controller
        val item = player?.currentMediaItem
        val metadata = player?.mediaMetadata
        val title = metadata?.title?.toString()
            ?: item?.mediaId?.let(Uri::parse)?.takeIf { it.scheme == "content" }?.let(CardBookmark::documentTitle)
            ?: bookmark?.title.orEmpty()
        val transport = if (player?.playWhenReady == true) PlaybackTransport.Pause else PlaybackTransport.Play
        playingBar.render(if (item == null) "" else title, metadata?.artist?.toString().orEmpty(), transport)
        val starting = loadingPlaylist || pendingPlay != null
        val label = when {
            starting -> "Starting…"
            bookmark != null -> "Resume · ${bookmark?.title.orEmpty()}"
            else -> "Play folder"
        }
        if (!TextUtils.equals(start.text, label)) start.text = label
        start.visibility = if (item == null && (starting || bookmark != null || songs.isNotEmpty())) View.VISIBLE else View.GONE
        start.isEnabled = !starting
    }

    private fun showNowPlaying() {
        val player = controller ?: return
        if (player.currentMediaItem == null) return
        val sleeve = RecordSleeve(this, ui)
        val transport = ui.iconButton("Play", Mark.PLAY, true, action = ::playOrResume)
        val back = ui.iconButton("Previous track", Mark.PREVIOUS) { player.seekToPrevious() }
        val forward = ui.iconButton("Next track", Mark.NEXT) { player.seekToNext() }
        val queue = ui.queueButton {
            val first = player.currentMediaItemIndex + 1
            val tracks = (first until player.mediaItemCount).map { index ->
                val item = player.getMediaItemAt(index)
                val title = item.mediaMetadata.title ?: CardBookmark.documentTitle(Uri.parse(item.mediaId))
                Entry(title.toString(), item.mediaMetadata.artist?.toString().orEmpty()) {
                    player.seekToDefaultPosition(index)
                }
            }
            ui.showQueue(this, tracks)
        }
        fun render() {
            val item = player.currentMediaItem ?: return
            val metadata = player.mediaMetadata
            val title = metadata.title ?: CardBookmark.documentTitle(Uri.parse(item.mediaId))
            val track = JSONObject().put("title", title.toString())
                .put("artist", metadata.artist?.toString().orEmpty())
            sleeve.bind(track, item.mediaId, metadata.artworkData)
            ui.playbackIcon(transport, if (player.playWhenReady) "Pause" else "Play", showPause = player.playWhenReady)
            back.isEnabled = player.isCurrentMediaItemSeekable
            forward.isEnabled = player.hasNextMediaItem()
        }
        val controls = ui.column(8).apply {
            addView(ui.transport(back, transport, forward), LinearLayout.LayoutParams(-1, -2))
            addView(queue, LinearLayout.LayoutParams(ui.dp(48), ui.dp(48)).apply {
                gravity = Gravity.CENTER_HORIZONTAL
            })
        }
        val layout = ui.playerContent(sleeve, controls).apply {
            setPadding(ui.dp(16), ui.dp(12), ui.dp(16), ui.dp(8))
        }
        val updates = object : Player.Listener {
            override fun onEvents(player: Player, events: Player.Events) = render()
        }
        render()
        player.addListener(updates)
        val body = ScrollView(this).apply {
            if (ui.eInk) overScrollMode = View.OVER_SCROLL_NEVER
            addView(layout)
        }
        SongDetailsStyle(ui).dialog(this, "Now playing", body,
            closeLabel = "Close player").apply {
            setOnDismissListener { player.removeListener(updates) }
            show()
            if (ui.compactPlayerLandscape) {
                window?.setLayout(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT)
            }
        }
    }

    private companion object {
        val ownedPlaylist = Regex(""".+ \[([a-f0-9]{20})\]( \(\d+\))?\.m3u8""")
        val audioTypes = setOf("flac", "mp3", "m4a", "aac", "ogg", "opus", "wav")
    }
}
