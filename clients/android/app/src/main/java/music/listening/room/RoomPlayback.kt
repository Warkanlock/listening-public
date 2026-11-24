package music.listening.room

import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Handler
import android.os.Looper
import android.os.SystemClock
import androidx.media3.common.AudioAttributes
import androidx.media3.common.C
import androidx.media3.common.ForwardingPlayer
import androidx.media3.common.MediaItem
import androidx.media3.common.MediaMetadata
import androidx.media3.common.PlaybackException
import androidx.media3.common.Player
import androidx.media3.datasource.DataSource
import androidx.media3.datasource.DefaultDataSource
import androidx.media3.datasource.okhttp.OkHttpDataSource
import androidx.media3.exoplayer.ExoPlayer
import androidx.media3.exoplayer.source.DefaultMediaSourceFactory
import androidx.media3.session.MediaSession
import androidx.media3.session.MediaSessionService
import okhttp3.OkHttpClient
import org.json.JSONArray
import org.json.JSONObject
import java.util.concurrent.Executors
import kotlin.math.abs

class RoomPlayback : MediaSessionService() {
    companion object {
        const val COMMAND = "music.listening.room.COMMAND"
        const val UPDATE = "music.listening.room.UPDATE"
        private var embeddedDevice: String? = null
        private var embeddedRef: String? = null
        private var embeddedBytes: ByteArray? = null
        fun artwork(device: String, ref: String): ByteArray? =
            embeddedBytes.takeIf { device == embeddedDevice && ref == embeddedRef }

        private var instance: RoomPlayback? = null
        fun phoneSnapshot(device: String): JSONObject = instance?.takeIf { it.room?.id == device }?.let {
            JSONObject().put("current", it.phoneTrack).put("current_ref", it.phoneTrack?.optString("key"))
                .put("playing", it.player.playWhenReady && it.player.playbackState != Player.STATE_ENDED)
                .put("position", it.player.currentPosition / 1000.0)
        } ?: JSONObject()
        fun bufferedSnapshot(device: String): JSONObject? = instance?.takeIf {
            it.room?.id == device && it.offline != null
        }?.let { playback ->
            playback.snapshot?.let { JSONObject(it.toString()).put("position", playback.player.currentPosition / 1000.0) }
        }

        fun bufferedTracks(device: String): Int =
            instance?.takeIf { it.room?.id == device }?.buffer?.readyCount() ?: 0

        internal var pauseCardPlayback: (() -> Unit)? = null
        val hasRadio: Boolean get() = instance?.radioDevice != null

        fun radioSnapshot(device: String): JSONObject? =
            instance?.takeIf { it.radioDevice == device }?.radioSnapshot()

        var yieldedToOtherPlayer = false
            private set

        fun yieldToOtherPlayer(context: Context) {
            val device = instance?.radioDevice ?: Room.load(context)?.id
            device?.let { RadioHistory.clear(context, it) }
            yieldedToOtherPlayer = true
            following = false
            instance?.let { local ->
                local.intentSequence++
                local.main.removeCallbacks(local.poll)
                local.pendingControls.clear()
                local.leaveRadio()
                local.player.pause()
                local.notifyActivity()
            }
        }

        var following = false
            private set
    }

    private val main = Handler(Looper.getMainLooper())
    private val network = Executors.newSingleThreadExecutor()
    private lateinit var player: ExoPlayer
    private var session: MediaSession? = null
    private var room: Room? = null
    private var phoneTrack: JSONObject? = null
    private var phone: PhoneLibrary? = null
    private var activeRef: Any? = null
    private var version = -1L
    private var joined: Boolean
        get() = following
        set(value) { following = value }
    private var closed = false
    private var lastHeartbeat = 0L
    private var occurrence = -1L
    private var endReported = false
    private lateinit var buffer: QueueAudioBuffer
    private var snapshot: JSONObject? = null
    private var offline: BufferedSession? = null
    private var activeUri: Uri? = null
    private var rejoining = false
    private var playbackEpoch = 0L
    private var intentSequence = 0L
    private var radioDevice: String? = null
    private var radioName = ""
    private var radioSource = RadioSource.LIBRARY
    private var radioErrors = 0
    private var radioRoom: Room? = null
    private val pendingControls = ArrayDeque<Pair<String, JSONObject>>()

    private data class BufferedSession(
        val base: JSONObject,
        val advances: List<JSONObject> = emptyList(),
        val finished: JSONObject? = null
    )

    override fun onCreate() {
        super.onCreate()
        room = Room.load(this)
        buffer = QueueAudioBuffer(this)
        phone = room?.let { PhoneLibrary.get(this, it.replicaId) }
        instance = this
        val sources = OkHttpDataSource.Factory(room?.network ?: OkHttpClient())
        room?.let { sources.setDefaultRequestProperties(mapOf("Cookie" to it.cookieHeader)) }
        player = ExoPlayer.Builder(this).setMediaSourceFactory(DefaultMediaSourceFactory(this)
            .setDataSourceFactory(DefaultDataSource.Factory(this, sources))).build().apply {
            setAudioAttributes(AudioAttributes.Builder().setUsage(C.USAGE_MEDIA)
                .setContentType(C.AUDIO_CONTENT_TYPE_MUSIC).build(), true)
            setHandleAudioBecomingNoisy(true)
            setWakeMode(C.WAKE_MODE_LOCAL)
        }
        player.addListener(object : Player.Listener {
            override fun onMediaMetadataChanged(mediaMetadata: MediaMetadata) {
                embeddedDevice = room?.id
                embeddedRef = player.currentMediaItem?.mediaId
                embeddedBytes = mediaMetadata.artworkData?.takeIf { it.size <= SleeveArtwork.MAX_BYTES }
            }

            override fun onMediaItemTransition(item: MediaItem?, reason: Int) {
                val device = radioDevice ?: return
                item?.let {
                    RadioHistory(radioSource, radioName, it.mediaId, it.mediaMetadata.title?.toString().orEmpty(),
                        it.mediaMetadata.artist?.toString().orEmpty()).save(this@RoomPlayback, device)
                }
                notifyActivity()
            }

            override fun onPlaybackStateChanged(state: Int) {
                if (radioDevice != null) {
                    if (state == Player.STATE_READY) radioErrors = 0
                    if (state == Player.STATE_ENDED) reportRadioPresence(false)
                    return
                }
                if (state == Player.STATE_ENDED && phoneTrack != null) {
                    command("next")
                    return
                }
                if (state == Player.STATE_ENDED && activeRef != null && !endReported) {
                    endReported = true
                    val end = JSONObject().put("expected_ref", activeRef)
                    if (occurrence >= 0) end.put("expected_occurrence", occurrence)
                    command("next", end)
                }
            }
            override fun onPlayWhenReadyChanged(ready: Boolean, reason: Int) {
                if (ready) pauseCardPlayback?.invoke()
                if (radioRoom != null) reportRadioPresence(ready)
                if (!ready && (reason == Player.PLAY_WHEN_READY_CHANGE_REASON_AUDIO_BECOMING_NOISY ||
                            reason == Player.PLAY_WHEN_READY_CHANGE_REASON_AUDIO_FOCUS_LOSS)) {
                    command("pause")
                }
            }
            override fun onPlayerError(error: PlaybackException) {
                if (radioDevice != null) {
                    if (++radioErrors < player.mediaItemCount) {
                        val next = (player.currentMediaItemIndex + 1) % player.mediaItemCount
                        player.seekTo(next, 0)
                        player.prepare()
                    } else {
                        player.pause()
                        notifyActivity("Could not play this station.")
                    }
                    return
                }
                val target = room
                val track = snapshot?.optJSONObject("current")
                if (target != null && phoneTrack == null && track != null && activeUri?.scheme != "file") {
                    val cached = cachedAudio(target, track, activeProfile)
                    if (cached != null) {
                        val position = player.currentPosition
                        val playing = player.playWhenReady
                        loadTrack(track, cached, position)
                        player.playWhenReady = playing
                        return
                    }
                }
                joined = false
                notifyActivity(if (phoneTrack != null) "This saved song could not be played." else
                    "Audio stopped. Check the room connection, then tap Play.")
            }
        })
        val controls = object : ForwardingPlayer(player) {
            override fun play() = resumeFromMediaSession()
            override fun pause() { intentSequence++; command("pause") }
            override fun setPlayWhenReady(play: Boolean) {
                if (play) resumeFromMediaSession() else { intentSequence++; command("pause") }
            }
            override fun seekTo(positionMs: Long) = seekRoom(positionMs)
            override fun seekTo(mediaItemIndex: Int, positionMs: Long) = seekRoom(positionMs)
            override fun seekToNext() = command("next")
            override fun seekToPrevious() = command("previous")
        }
        val open = PendingIntent.getActivity(this, 0, Intent(this, RoomActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
        val playbackSession = MediaSession.Builder(this, controls).setId("room-player")
            .setSessionActivity(open).build()
        session = playbackSession
        addSession(playbackSession)
    }

    override fun onGetSession(controllerInfo: MediaSession.ControllerInfo): MediaSession? = session

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent?.action == COMMAND) {
            val op = intent.getStringExtra("op") ?: return START_NOT_STICKY
            val body = JSONObject(intent.getStringExtra("fields") ?: "{}")
            val homeWifi = intent.getBooleanExtra("home_wifi", false)
            if (op != "snapshot") intentSequence++
            when (op) {
                "play_mood", "play_activity" -> tuneMix(op, body)
                "radio_start" -> Room.load(this, homeWifi = homeWifi)?.let {
                    startLibraryRadio(it, body.getString("genre"))
                }
                "radio_live" -> Room.load(this, homeWifi = homeWifi)?.let { target ->
                    startLiveRadio(target, body.getString("name"))
                }
                "radio_resume" -> Room.load(this, homeWifi = homeWifi)?.let { target ->
                    RadioHistory.read(this, target.id)?.let { history ->
                        when (history.source) {
                            RadioSource.LIBRARY -> startLibraryRadio(target, history.name, history.ref)
                            RadioSource.LIVE -> startLiveRadio(target, history.name)
                            RadioSource.YOUTUBE -> Unit
                        }
                    }
                }
                "radio_stop" -> {
                    val device = radioDevice ?: Room.load(this, homeWifi = homeWifi)?.id
                    device?.let { RadioHistory.clear(this, it) }
                    leaveRadio()
                    notifyActivity()
                }
                else -> {
                    if (op == "seek" && body.has("lyric_radio") &&
                        (body.optBoolean("lyric_radio") != (radioDevice != null) ||
                            (radioDevice != null && body.optString("expected_ref") != player.currentMediaItem?.mediaId))) return START_NOT_STICKY
                    if (op == "jump" && body.has("radio_ref") && radioDevice == null) return START_NOT_STICKY
                    if (radioDevice == null || op !in listOf("resume", "pause", "next", "previous", "seek", "jump")) {
                        (radioDevice ?: room?.id)?.let { RadioHistory.clear(this, it) }
                        leaveRadio()
                        yieldedToOtherPlayer = false
                        joined = true
                    }
                    command(op, body)
                }
            }
            return START_NOT_STICKY
        }
        return super.onStartCommand(intent, flags, startId)
    }

    private fun tuneMix(op: String, fields: JSONObject) {
        val target = room ?: return
        val sequence = intentSequence
        network.execute {
            val result = runCatching { target.action(op, fields) }
            main.post {
                if (closed || sequence != intentSequence) return@post
                result.onSuccess { value ->
                    RadioHistory.clear(this, target.id)
                    leaveRadio()
                    yieldedToOtherPlayer = false
                    joined = true
                    receiveSnapshot(target, value)
                    val track = value.optJSONObject("current")?.optString("title").orEmpty()
                    notifyActivity(notice = if (track.isBlank()) "Mix started." else "Mix started · $track")
                    schedulePoll()
                }.onFailure { notifyActivity(it.message ?: "Couldn’t start this mix. Try again.") }
            }
        }
    }

    private fun beginRadio(target: Room, source: RadioSource, name: String): Long {
        leaveRadio()
        val epoch = ++playbackEpoch
        radioDevice = target.id
        radioRoom = target
        radioSource = source
        radioName = name
        radioErrors = 0
        yieldedToOtherPlayer = false
        joined = false
        offline = null
        rejoining = false
        pendingControls.clear()
        main.removeCallbacks(poll)
        player.pause()
        player.clearMediaItems()
        activeRef = null
        activeUri = null
        phoneTrack = null
        return epoch
    }

    private fun startLiveRadio(target: Room, name: String) {
        val sequence = intentSequence
        fetchLiveStation(target, name) { station ->
            if (intentSequence != sequence) return@fetchLiveStation
            if (station == null) {
                notifyActivity("This live station is unavailable. Check the room’s Radio extensions.")
            } else {
                playLiveStation(target, station)
            }
        }
    }

    private fun playLiveStation(target: Room, station: RadioHistory) {
        val stream = RadioHistory.liveUri(station.ref) ?: return
        beginRadio(target, RadioSource.LIVE, station.name)
        val item = MediaItem.Builder().setMediaId(station.ref).setUri(stream)
            .setMediaMetadata(MediaMetadata.Builder().setTitle(station.title).setArtist(station.artist).build()).build()
        // A public radio stream never receives the room's cookie or private routing.
        val sources = DefaultMediaSourceFactory(this).setDataSourceFactory(DefaultDataSource.Factory(this))
        player.setMediaSource(sources.createMediaSource(item))
        player.prepare()
        player.play()
        scheduleLiveCheck()
        notifyActivity()
    }

    private fun fetchLiveStation(target: Room, name: String, result: (RadioHistory?) -> Unit) {
        network.execute {
            val station = runCatching { RadioHistory.liveStation(target.get("/api/stations"), name) }.getOrNull()
            main.post { if (!closed) result(station) }
        }
    }

    private fun verifyLiveSession(onAllowed: (RadioHistory) -> Unit) {
        val target = radioRoom ?: return
        val epoch = playbackEpoch
        fetchLiveStation(target, radioName) { station ->
            if (playbackEpoch != epoch) return@fetchLiveStation
            if (station == null) {
                leaveRadio()
                notifyActivity("This live station is unavailable. Check the room’s Radio extensions.")
            } else onAllowed(station)
        }
    }

    private val liveCheck = Runnable { verifyLiveSession { scheduleLiveCheck() } }

    private fun scheduleLiveCheck() {
        main.removeCallbacks(liveCheck)
        main.postDelayed(liveCheck, 20_000)
    }

    private fun startLibraryRadio(target: Room, genre: String, first: String? = null) {
        val epoch = beginRadio(target, RadioSource.LIBRARY, genre)
        val profile = MobileAudio.load(this)
        network.execute {
            val tracks = runCatching {
                val rows = target.get("/api/radio/library?genre=" + Uri.encode(genre)).getJSONArray("tracks")
                (0 until rows.length()).map { index ->
                    val track = rows.getJSONObject(index)
                    MediaItem.Builder().setMediaId(track.get("id").toString())
                        .setUri(target.playbackAudioUrl(track.getString("audio"), profile))
                        .setMediaMetadata(MediaMetadata.Builder().setTitle(track.getString("title"))
                            .setArtist(track.optString("artist"))
                            .setAlbumTitle(track.optString("album")).build()).build()
                }.shuffled()
            }
            main.post {
                if (closed || playbackEpoch != epoch) return@post
                val items = tracks.getOrNull()
                if (items.isNullOrEmpty()) {
                    leaveRadio()
                    notifyActivity(tracks.exceptionOrNull()?.message ?: "This genre has no playable songs.")
                    return@post
                }
                val sources = OkHttpDataSource.Factory(target.network)
                    .setDefaultRequestProperties(mapOf("Cookie" to target.cookieHeader))
                val factory = DefaultMediaSourceFactory(this)
                    .setDataSourceFactory(DefaultDataSource.Factory(this, sources))
                pauseCardPlayback?.invoke()
                player.repeatMode = Player.REPEAT_MODE_ALL
                val start = items.indexOfFirst { it.mediaId == first }.coerceAtLeast(0)
                player.setMediaSources(items.map(factory::createMediaSource), start, 0)
                player.prepare()
                player.play()
                notifyActivity()
            }
        }
    }

    private val radioPresence = Runnable { reportRadioPresence(true) }

    private fun reportRadioPresence(playing: Boolean) {
        main.removeCallbacks(radioPresence)
        val target = radioRoom ?: return
        val epoch = playbackEpoch
        val source = if (!playing) null else if (radioSource == RadioSource.LIVE) "live" else "library"
        network.execute {
            val result = runCatching { target.radioPresence(source) }
            if (playing && result.getOrNull() == false) main.post {
                if (!closed && playbackEpoch == epoch && player.playWhenReady) {
                    leaveRadio()
                    notifyActivity("This station is unavailable. Check the room and try again.")
                }
            }
        }
        if (playing) main.postDelayed(radioPresence, 10_000)
    }

    private fun leaveRadio() {
        if (radioDevice == null) return
        reportRadioPresence(false)
        radioDevice = null
        radioRoom = null
        main.removeCallbacks(liveCheck)
        playbackEpoch++
        player.repeatMode = Player.REPEAT_MODE_OFF
        player.pause()
        player.clearMediaItems()
    }

    private fun radioSnapshot(): JSONObject {
        fun details(item: MediaItem) = JSONObject().put("id", item.mediaId).put("title", item.mediaMetadata.title)
            .put("artist", item.mediaMetadata.artist).put("album", item.mediaMetadata.albumTitle)
        val item = player.currentMediaItem ?: return JSONObject()
        val count = player.mediaItemCount
        val upcoming = (1..minOf(20, count - 1)).map {
            details(player.getMediaItemAt((player.currentMediaItemIndex + it) % count))
        }
        val duration = player.duration.coerceAtLeast(0) / 1000.0
        return JSONObject().put("radio", true).put("radio_live", radioSource == RadioSource.LIVE)
            .put("current", details(item).put("duration", duration))
            .put("current_ref", item.mediaId)
            .put("upcoming", JSONArray(upcoming))
            .put("playing", player.playWhenReady)
            .put("position", player.currentPosition / 1000.0)
    }

    private fun controlRadio(op: String, fields: JSONObject) {
        when (op) {
            "resume" -> {
                val sequence = intentSequence
                val play = {
                    if (intentSequence == sequence) {
                        if (player.playerError != null) {
                            radioErrors = 0
                            player.prepare()
                        }
                        player.play()
                        notifyActivity()
                    }
                }
                if (radioSource == RadioSource.LIVE) {
                    verifyLiveSession { station ->
                        if (intentSequence != sequence) return@verifyLiveSession
                        if (player.currentMediaItem?.mediaId == station.ref) play()
                        else radioRoom?.let { playLiveStation(it, station) }
                    }
                } else play()
            }
            "pause" -> player.pause()
            "next" -> if (radioSource == RadioSource.LIBRARY) player.seekToNextMediaItem()
            "previous" -> if (radioSource == RadioSource.LIBRARY) player.seekToPreviousMediaItem()
            "seek" -> if (radioSource == RadioSource.LIBRARY) player.seekTo((fields.getDouble("position") * 1000).toLong())
            "jump" -> {
                val ref = fields.optString("radio_ref")
                val index = (0 until player.mediaItemCount).firstOrNull { player.getMediaItemAt(it).mediaId == ref }
                if (index != null) player.seekTo(index, 0)
            }
        }
        notifyActivity()
    }

    private fun resumeFromMediaSession() {
        intentSequence++
        if (yieldedToOtherPlayer) {
            yieldedToOtherPlayer = false
            joined = true
        }
        command("resume")
    }

    private fun seekRoom(ms: Long) = command("seek", JSONObject().put("position", ms / 1000.0))

    private val poll = Runnable { if (joined) command("snapshot") }
    private fun schedulePoll() {
        main.removeCallbacks(poll)
        if (joined) main.postDelayed(poll, 2500L)
    }

    private fun command(op: String, fields: JSONObject = JSONObject()) {
        main.removeCallbacks(poll)
        if (closed || yieldedToOtherPlayer) return
        if (radioDevice != null) {
            controlRadio(op, fields)
            return
        }
        val target = room ?: return
        if (rejoining) {
            if (op != "snapshot") pendingControls.addLast(op to fields)
            return
        }
        val library = phone
        if (library != null && library.local) {
            playOnPhone(library, op, fields)
            return
        }
        val next = snapshot?.optJSONArray("upcoming")?.optJSONObject(0)
        val cachedBoundary = op == "next" && fields.has("expected_ref") && next != null &&
            snapshot?.optBoolean("buffer_reconciliation") == true &&
            cachedAudio(target, next, activeProfile) != null
        if (op != "snapshot" && (offline != null || cachedBoundary)) {
            if (!playBuffered(target, op, fields)) notifyActivity("Reconnect to change the room queue.")
            if (cachedBoundary) command("snapshot") else schedulePoll()
            return
        }
        val epoch = playbackEpoch
        network.execute {
            val result = runCatching {
                val time = SystemClock.elapsedRealtime()
                if (time - lastHeartbeat > 20000L) {
                    target.heartbeat()
                    lastHeartbeat = time
                }
                if (op == "snapshot") target.session() else target.action(op, fields)
            }
            main.post {
                if (closed || epoch != playbackEpoch || yieldedToOtherPlayer || phone?.local == true) return@post
                result.onSuccess { receiveSnapshot(target, it) }.onFailure {
                    val current = snapshot?.optJSONObject("current")
                    val cached = current?.let { track -> cachedAudio(target, track, activeProfile) }
                    if (it is RoomUnreachable && current != null && cached != null && activeRef != null) {
                        if (offline == null) offline = snapshot?.let { BufferedSession(it) }
                        if (activeUri != cached && player.playbackState != Player.STATE_ENDED) {
                            val wasPlaying = player.playWhenReady
                            loadTrack(current, cached, player.currentPosition)
                            player.playWhenReady = wasPlaying
                        }
                        if (op != "snapshot") playBuffered(target, op, fields)
                        else notifyActivity()
                        schedulePoll()
                        return@post
                    }
                    // A partial current stream can still play as far as its own media buffer.
                    if (it is RoomUnreachable && activeRef != null) {
                        if (op != "snapshot" && !playBuffered(target, op, fields))
                            notifyActivity(it.message)
                        schedulePoll()
                        return@post
                    }
                    player.pause()
                    endReported = false
                    if (it is PairingRequired) {
                        buffer.prepare(target, emptyList())
                        joined = false
                        stopSelf()
                    }
                    notifyActivity(it.message ?: "Room unavailable.")
                }
                schedulePoll()
            }
        }
    }

    private fun receiveSnapshot(target: Room, remote: JSONObject) {
        if (rejoining) return
        val local = offline
        if (local == null || !remote.optBoolean("buffer_reconciliation") ||
            remote.optLong("version") != local.base.optLong("version") ||
            remote.opt("current_ref") != local.base.opt("current_ref") ||
            remote.optLong("occurrence") != local.base.optLong("occurrence") ||
            !remote.optBoolean("output_selected", true)) {
            offline = null
            applySnapshot(target, remote)
            return
        }
        val fields = JSONObject().put("expected_version", local.base.getLong("version"))
            .put("expected_ref", local.base.get("current_ref"))
            .put("expected_occurrence", local.base.getLong("occurrence"))
            .put("advances", JSONArray(local.advances)).put("finished", local.finished)
            .put("position", player.currentPosition / 1000.0)
            .put("playing", snapshot?.optBoolean("playing") == true)
        rejoining = true
        val epoch = playbackEpoch
        network.execute {
            val result = runCatching { target.action("resume_buffer", fields) }
            main.post {
                if (closed || epoch != playbackEpoch) return@post
                rejoining = false
                if (yieldedToOtherPlayer) return@post
                result.onSuccess {
                    offline = null
                    applySnapshot(target, it)
                }.onFailure {
                    if (it is PairingRequired) {
                        offline = null
                        player.stop()
                        buffer.prepare(target, emptyList())
                        joined = false
                        notifyActivity(it.message)
                        stopSelf()
                        return@post
                    }
                }
                while (pendingControls.isNotEmpty()) {
                    val (op, action) = pendingControls.removeFirst()
                    command(op, action)
                }
                schedulePoll()
            }
        }
    }

    private fun playOnPhone(library: PhoneLibrary, op: String, fields: JSONObject) {
        val key = phoneTrack?.optString("key").orEmpty()
        when (op) {
            "play", "next", "previous" -> {
                val track = when (op) {
                    "play" -> library.playing(fields.optString("key"))
                    "next" -> library.playing(key, 1)
                    else -> library.playing(key, -1)
                }
                if (track == null) {
                    if (op == "previous") player.seekTo(0) else player.pause()
                    return
                }
                val item = MediaItem.Builder().setMediaId(track.getString("key"))
                    .setUri(Uri.parse("replica:///audio/" + track.getString("local_hash")))
                    .setMediaMetadata(MediaMetadata.Builder().setTitle(track.getString("title"))
                        .setArtist(track.optString("artist")).build()).build()
                val factory = DefaultMediaSourceFactory(this).setDataSourceFactory(DataSource.Factory { PhoneAudio(library) })
                track.remove("cover")
                phoneTrack = track
                activeRef = null
                player.setMediaSource(factory.createMediaSource(item))
                player.prepare()
                player.play()
            }
            "pause" -> player.pause()
            "resume" -> { if (player.playbackState == Player.STATE_ENDED) player.seekTo(0); player.play() }
            "seek" -> {
                if (fields.has("expected_ref") && fields.optString("expected_ref") != key) return
                player.seekTo((fields.getDouble("position") * 1000).toLong())
            }
        }
        notifyActivity()
    }

    private var activeProfile = MobileAudio.Original

    private fun cachedAudio(target: Room, track: JSONObject, profile: MobileAudio): Uri? =
        buffer.file(target.playbackAudioUrl(track.getString("audio"), profile))?.let(Uri::fromFile)

    private fun loadTrack(track: JSONObject, uri: Uri, position: Long) {
        val item = MediaItem.Builder().setMediaId(track.get("id").toString()).setUri(uri)
            .setMediaMetadata(MediaMetadata.Builder().setTitle(track.getString("title"))
                .setArtist(track.optString("artist")).build()).build()
        activeUri = uri
        player.setMediaItem(item, position)
        player.prepare()
    }

    private fun playBuffered(target: Room, op: String, fields: JSONObject): Boolean {
        val value = snapshot ?: return false
        val current = value.optJSONObject("current") ?: return false
        val updated = JSONObject(value.toString())
        val base = offline ?: BufferedSession(value)
        var updatedBuffer = base
        when (op) {
            "next" -> {
                if (fields.has("expected_ref") && (fields.get("expected_ref") != activeRef ||
                    fields.optLong("expected_occurrence", occurrence) != occurrence)) return true
                val queue = value.getJSONArray("upcoming")
                val next = queue.optJSONObject(0)
                val cached = next?.let { cachedAudio(target, it, activeProfile) }
                val boundary = JSONObject().put("position", player.currentPosition / 1000.0)
                    .put("natural", fields.has("expected_ref"))
                if (value.optInt("queue_length") == 0) {
                    player.pause()
                    player.seekTo(0)
                    updated.put("playing", false)
                    updatedBuffer = base.copy(finished = boundary)
                    notifyActivity("End of the queue.")
                } else if (next == null || cached == null || base.advances.size >= QueueAudioBuffer.ahead(this)) {
                    player.pause()
                    updated.put("playing", false)
                    if (boundary.getBoolean("natural")) updatedBuffer = base.copy(finished = boundary)
                    notifyActivity("Reconnect to play the next song.")
                } else {
                    activeRef = next.get("id")
                    occurrence++
                    endReported = false
                    updatedBuffer = base.copy(advances = base.advances + boundary)
                    val remaining = JSONArray()
                    for (index in 1 until queue.length()) remaining.put(queue.get(index))
                    updated.put("current", next).put("current_ref", activeRef).put("occurrence", occurrence)
                        .put("upcoming", remaining).put("queue_length", value.optInt("queue_length") - 1)
                        .put("playing", true)
                    loadTrack(next, cached, 0)
                    player.play()
                }
            }
            "pause" -> {
                player.pause()
                updated.put("playing", false)
                if (base.finished != null && value.optInt("queue_length") > 0) {
                    updatedBuffer = base.copy(finished = null)
                    player.seekTo(0)
                }
            }
            "resume", "seek" -> {
                if (op == "seek" && fields.has("expected_ref") && fields.get("expected_ref") != current.get("id")) return true
                if (op == "resume" && base.finished != null && value.optInt("queue_length") > 0) return false
                val cached = cachedAudio(target, current, activeProfile) ?: return false
                val position = if (op == "seek") (fields.getDouble("position") * 1000).toLong()
                    else if (player.playbackState == Player.STATE_ENDED) 0L else player.currentPosition
                if (activeUri != cached) loadTrack(current, cached, position) else player.seekTo(position)
                if (op == "resume") {
                    player.play()
                    updated.put("playing", true)
                } else if (value.optInt("queue_length") > 0) {
                    updatedBuffer = base.copy(finished = null)
                }
            }
            else -> return false
        }
        updated.put("position", player.currentPosition / 1000.0)
        snapshot = updated
        offline = updatedBuffer
        notifyActivity()
        return true
    }

    private val clearUnselectedBuffer = Runnable { room?.let { buffer.prepare(it, emptyList()) } }

    private fun applySnapshot(target: Room, value: JSONObject) {
        val wasSelected = snapshot?.optBoolean("output_selected", true) == true
        snapshot = value
        val nextVersion = value.getLong("version")
        if (nextVersion < version) {
            // A restarted server can begin another session version sequence.
            // The single network worker preserves request and response order.
            activeRef = null
        }
        version = nextVersion
        val track = value.optJSONObject("current")
        val selected = value.optBoolean("output_selected", true)
        if (track == null || !joined || !selected) {
            player.stop()
            player.clearMediaItems()
            activeRef = null
            activeUri = null
            if (track == null || !joined) {
                main.removeCallbacks(clearUnselectedBuffer)
                buffer.prepare(target, emptyList())
            } else if (wasSelected) {
                main.postDelayed(clearUnselectedBuffer, 5 * 60_000L)
            }
            return
        }
        main.removeCallbacks(clearUnselectedBuffer)
        val ref = value.get("current_ref")
        val position = (value.getDouble("position") * 1000).toLong().coerceAtLeast(0L)
        val nextOccurrence = value.optLong("occurrence", -1L)
        val profile = MobileAudio.load(this)
        if (ref != activeRef || nextOccurrence != occurrence || profile != activeProfile || player.playerError != null) {
            activeRef = ref
            activeProfile = profile
            occurrence = nextOccurrence
            endReported = false
            val uri = cachedAudio(target, track, profile) ?: Uri.parse(target.playbackAudioUrl(track.getString("audio"), profile))
            loadTrack(track, uri, position)
        } else if (abs(player.currentPosition - position) > 1400L) {
            player.seekTo(position)
        }
        player.playWhenReady = value.getBoolean("playing")
        val queue = value.getJSONArray("upcoming")
        val urls = listOf(track) + (0 until minOf(queue.length(), QueueAudioBuffer.ahead(this)))
            .mapNotNull(queue::optJSONObject)
        buffer.prepare(target, urls.map { target.playbackAudioUrl(it.getString("audio"), profile) })
        notifyActivity()
    }

    private fun notifyActivity(error: String? = null, notice: String? = null) {
        sendBroadcast(Intent(UPDATE).setPackage(packageName).putExtra("error", error).putExtra("notice", notice))
    }

    override fun onDestroy() {
        instance = null
        embeddedDevice = null
        embeddedRef = null
        embeddedBytes = null
        closed = true
        joined = false
        if (radioRoom != null) {
            reportRadioPresence(false)
            radioRoom = null
            radioDevice = null
            network.shutdown()
        } else {
            network.shutdownNow()
        }
        main.removeCallbacksAndMessages(null)
        session?.release()
        player.release()
        buffer.close()
        super.onDestroy()
    }
}
