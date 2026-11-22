package music.listening.room

import android.net.Uri
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.Request
import okhttp3.RequestBody
import okio.BufferedSink
import okio.source
import org.json.JSONObject
import java.io.IOException
import java.util.UUID
import java.util.concurrent.Executors

/** Copy metadata first. Each original is independently committed and safe to retry. */
private class SongRejected(message: String) : IOException(message)

internal object ReplicaSync {
    private val worker = Executors.newSingleThreadExecutor()
    @Volatile var busy = false
        private set
    @Volatile var importing = false
        private set
    private val intake = Executors.newSingleThreadExecutor()
    @Volatile var progress = ""
        private set

    @Synchronized fun importSong(room: Room, library: PhoneLibrary, uri: Uri, done: (String) -> Unit) {
        if (!library.enabled || importing) { done("Finish the current import first."); return }
        importing = true
        intake.execute {
            val result = runCatching { library.importAudio(uri) }
            importing = false
            result.onSuccess { start(room, library) }
            done(result.exceptionOrNull()?.message ?: "Song saved on this phone.")
        }
    }

    @Synchronized fun start(room: Room, library: PhoneLibrary, done: () -> Unit = {}): Boolean {
        if (busy || !library.enabled) return false
        busy = true
        progress = "Checking the room…"
        worker.execute {
            try {
                sync(room, library)
            } catch (error: Exception) {
                progress = if (error is PairingRequired) "Room access expired. Your phone copy is available."
                    else error.message ?: "Room unavailable. Sync again when connected."
            } finally {
                library.options.edit().putString("status", progress).apply()
                busy = false
                done()
            }
        }
        return true
    }

    private fun sync(room: Room, library: PhoneLibrary) {
        val round = UUID.randomUUID().toString()
        var cursor = "tracks-0"
        var count = 0
        do {
            if (!library.enabled) return
            val page = room.get("/api/replica?cursor=${Room.encode(cursor)}")
            library.bind(page.getString("collection"))
            val tracks = page.getJSONArray("tracks")
            library.catalogPage(tracks, round)
            count += tracks.length()
            progress = "Copied $count catalog entries…"
            cursor = page.getString("next")
        } while (cursor.isNotEmpty())

        var failed = 0
        var reason = ""
        fun attempt(work: () -> Unit) {
            try { work() } catch (error: SongRejected) {
                failed++
                reason = error.message.orEmpty()
            }
        }
        for (original in library.missingImports(round)) {
            if (!library.enabled) return
            progress = "Sending ${original.track.optString("title")}…"
            attempt { upload(room, library, original) }
        }
        var row = 0L
        var copied = 0
        while (library.enabled) {
            val batch = library.downloads(round, row)
            if (batch.isEmpty()) break
            for ((id, track) in batch) {
                if (!library.enabled) return
                row = id
                progress = "Saving ${track.getString("title")}…"
                attempt {
                    val audio = track.getJSONObject("original")
                    val hash = audio.getString("hash")
                    if (library.has(hash)) library.catalogPage(org.json.JSONArray().put(track), round)
                    else {
                        if (library.app.filesDir.usableSpace < audio.getLong("size") + (16L shl 20))
                            throw IllegalStateException("Phone storage is full. Free space, then sync again.")
                        val path = track.getString("audio")
                        room.audioUrl(path) // Validate the room's streaming route before selecting its replica equivalent.
                        val request = Request.Builder().url(room.origin + "/api/replica" + path.removePrefix("/api"))
                            .header("X-Listening-Collection", library.options.getString("collection", "")!!)
                            .header("Cookie", room.cookieHeader).build()
                        room.network.newCall(request).execute().use { response ->
                            if (response.code in setOf(401, 403, 409))
                                throw IllegalStateException("Room access or collection changed. Check offline sync in Devices.")
                            if (!response.isSuccessful) throw SongRejected("Room returned HTTP ${response.code} for this song.")
                            val stream = response.body?.byteStream() ?: throw IOException("Empty audio response.")
                            library.store(stream, hash, audio.getLong("size"), audio.getString("suffix"), track, round)
                        }
                        copied++
                    }
                }
            }
        }
        progress = if (failed > 0) "$failed transfer(s) still need sync. $reason"
            else "Sync complete · $count catalog entries · $copied new copies."
    }

    private fun upload(room: Room, library: PhoneLibrary, original: Original) {
        val body = object : RequestBody() {
            override fun contentType() = "application/octet-stream".toMediaType()
            override fun contentLength() = original.size
            override fun writeTo(sink: BufferedSink) {
                library.stream(original.hash).source().use { sink.writeAll(it) }
            }
        }
        val collection = library.options.getString("collection", "")!!
        val request = Request.Builder().url(room.origin + "/api/replica/original")
            .header("Cookie", room.cookieHeader).header("Origin", room.origin)
            .header("X-Audio-Sha256", original.hash).header("X-Audio-Suffix", original.suffix)
            .header("X-Audio-Title", Uri.encode(original.track.getString("title")))
            .header("X-Audio-Duration", original.track.optDouble("duration", 0.0).toString())
            .header("X-Listening-Collection", collection).post(body).build()
        room.network.newCall(request).execute().use { response ->
            val result = response.body?.string()?.let { runCatching { JSONObject(it) }.getOrNull() }
            if (response.code == 401 || response.code == 403)
                throw IllegalStateException(result?.optString("error") ?: "Room access expired.")
            if (!response.isSuccessful || result?.optString("hash") != original.hash || result.optString("collection") != collection) {
                throw SongRejected(result?.optString("error") ?: "Upload was not confirmed by the room.")
            }
        }
    }
}
