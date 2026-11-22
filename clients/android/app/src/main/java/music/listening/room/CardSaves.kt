package music.listening.room

import android.content.ContentResolver
import android.net.Uri
import android.provider.DocumentsContract
import android.provider.DocumentsContract.Document
import okhttp3.Call
import okhttp3.Request
import org.json.JSONObject
import java.io.File
import java.io.IOException
import java.io.InputStream
import java.security.MessageDigest

internal data class SavedPlaylist(val key: String, val name: String, val available: Int)
internal data class SaveTrack(val url: String, val label: String, val seconds: Double)

internal class CardSaveOperation {
    @Volatile var cancelled = false
        private set
    private var activeCall: Call? = null

    fun check() {
        if (cancelled) throw IOException("Save paused. Tap Retry to continue.")
    }

    @Synchronized
    fun register(call: Call) {
        check()
        activeCall = call
    }

    @Synchronized
    fun cancel() {
        cancelled = true
        activeCall?.cancel()
    }
}

internal class CardSaves(private val resolver: ContentResolver, private val tree: Uri, private val scratch: File,
                         private val operation: CardSaveOperation) {
    private data class DocumentFile(val uri: Uri, val name: String)
    private val root = DocumentsContract.buildDocumentUriUsingTree(tree, DocumentsContract.getTreeDocumentId(tree))
    private val folder = children(root).firstOrNull { it.name == "Saved playlists" }?.uri
        ?: DocumentsContract.createDocument(resolver, root, Document.MIME_TYPE_DIR, "Saved playlists")
        ?: throw IOException("Could not create the Saved playlists folder on this card.")
    private val files = children(folder).associateBy { it.name }.toMutableMap()

    fun save(room: Room, playlist: SavedPlaylist, tracks: List<SaveTrack>, profile: MobileAudio,
             progress: (Int) -> Unit) {
        val saved = mutableMapOf<String, String>()
        val names = tracks.mapIndexed { index, track ->
            val filename = saved.getOrPut(track.url) { saveTrack(room, track, profile) }
            progress(index + 1)
            filename
        }
        // A generation is published before replacing the previous manifest.
        // If transfer stops, the previous playlist still works offline.
        val marker = key(room.cardOrigin + "/" + playlist.key).take(20)
        val name = "${safe(playlist.name)} [$marker].m3u8"
        val previous = files.values.filter {
            it.name.contains("[$marker]") && it.name.endsWith(".m3u8") && isOwnedManifest(it.uri)
        }
        publish(name) { output ->
            output.write(("#EXTM3U\n#LISTENING-PLAYLIST\n" + names.joinToString("\n", postfix = "\n")).toByteArray())
        }
        previous.forEach { DocumentsContract.deleteDocument(resolver, it.uri) }
    }

    private data class AudioIdentity(val sha: String, val extension: String, val bytes: Long)

    private fun identity(response: okhttp3.Response, bytes: Long): AudioIdentity {
        if (response.code == 401) throw PairingRequired()
        if (response.code !in setOf(200, 206)) {
            val reason = runCatching { JSONObject(response.body?.string().orEmpty()).optString("error") }.getOrNull()
            throw IOException(reason?.takeIf { it.isNotEmpty() } ?: "The room could not send this track.")
        }
        val sha = response.header("ETag")?.removeSurrounding("\"")?.takeIf { it.matches(hashPattern) }
            ?: throw IOException("Update the room to enable verified card downloads.")
        val extension = response.header("X-Audio-Extension")?.takeIf { it in formats }
            ?: throw IOException("This track’s format is unavailable on the card player.")
        if (bytes <= 0) throw IOException("The room returned an invalid audio size.")
        return AudioIdentity(sha, extension, bytes)
    }

    private fun saveTrack(room: Room, track: SaveTrack, profile: MobileAudio): String {
        operation.check()
        scratch.mkdirs()
        val stamp = key(room.cardOrigin + room.id + track.url + profile.wire)
        val part = File(scratch, "$stamp.part")
        val tag = File(scratch, "$stamp.etag")
        val old = if (part.length() > 0 && tag.isFile) tag.readText() else ""
        val audioUrl = room.audioUrl(track.url, profile)
        val url = audioUrl + (if ('?' in audioUrl) "&" else "?") + "save=1"
        val request = Request.Builder().url(url).header("Cookie", room.cookieHeader)
            .header("Accept-Encoding", "identity").build()

        val expected = room.network.newCall(request.newBuilder().header("Range", "bytes=0-0").build())
            .execute().use { response ->
                val size = response.header("Content-Range")?.let { probeRange.matchEntire(it) }
                    ?.groupValues?.get(1)?.toLongOrNull() ?: 0L
                identity(response, size)
            }

        fun reused(audio: AudioIdentity): String? {
            val file = files.values.firstOrNull { it.name.endsWith("[${audio.sha}]${audio.extension}") } ?: return null
            if (resolver.openInputStream(file.uri)?.use(::hash) != audio.sha)
                throw IOException("A saved track changed. Move it out of Saved playlists and tap Retry.")
            part.delete()
            tag.delete()
            return file.name
        }

        reused(expected)?.let { return it }
        val audio = if (old == expected.sha && part.length() == expected.bytes && part.inputStream().use(::hash) == old) {
            expected
        } else {
            val offset = part.length().takeIf { old == expected.sha && it < expected.bytes } ?: 0L
            val download = request.newBuilder()
            if (offset > 0) download.header("Range", "bytes=$offset-").header("If-Range", "\"$old\"")
            room.network.newCall(download.build()).execute().use { response ->
                val body = response.body ?: throw IOException("The room returned no audio.")
                val remaining = body.contentLength()
                val start = if (response.code == 206) offset else 0L
                val received = identity(response, start + remaining)
                if (remaining <= 0 || (response.code == 206 && (start <= 0 || old != received.sha ||
                        response.header("Content-Range") != "bytes $start-${received.bytes - 1}/${received.bytes}"))) {
                    throw IOException("The room returned an invalid audio range.")
                }
                if (start == 0L) part.delete()
                tag.writeText(received.sha)
                java.io.FileOutputStream(part, start > 0).use { output -> body.byteStream().copyTo(output) }
                if (part.length() != received.bytes || part.inputStream().use(::hash) != received.sha) {
                    part.delete()
                    tag.delete()
                    throw IOException("The copy failed its checksum. Tap Retry to download it again.")
                }
                received
            }
        }
        if (audio != expected) reused(audio)?.let { return it }
        val name = "${safe(track.label)} [${audio.sha}]${audio.extension}"
        val published = publish(name, audio.sha) { output ->
            part.inputStream().use { input ->
                val chunk = ByteArray(128 * 1024)
                while (true) {
                    operation.check()
                    val count = input.read(chunk)
                    if (count < 0) break
                    output.write(chunk, 0, count)
                }
            }
        }
        part.delete()
        tag.delete()
        return published.name
    }

    private fun publish(name: String, checksum: String? = null, write: (java.io.OutputStream) -> Unit): DocumentFile {
        operation.check()
        val stage = DocumentsContract.createDocument(resolver, folder, "application/octet-stream", ".listening-part")
            ?: throw IOException("Could not write to the card. Check its free space and permissions.")
        var finished = false
        try {
            (resolver.openOutputStream(stage, "wt") ?: throw IOException("Could not open the card file.")).use(write)
            if (checksum != null && resolver.openInputStream(stage)?.use(::hash) != checksum)
                throw IOException("The card could not verify the saved copy.")
            operation.check()
            val final = DocumentsContract.renameDocument(resolver, stage, name)
                ?: throw IOException("Could not publish the file on this card.")
            finished = true
            val actual = resolver.query(final, arrayOf(Document.COLUMN_DISPLAY_NAME), null, null, null)?.use {
                if (it.moveToFirst()) it.getString(0) else null
            } ?: throw IOException("Could not read the saved file.")
            return DocumentFile(final, actual).also { files[actual] = it }
        } finally {
            if (!finished) runCatching { DocumentsContract.deleteDocument(resolver, stage) }
        }
    }

    private fun isOwnedManifest(uri: Uri): Boolean =
        resolver.openInputStream(uri)?.bufferedReader()?.use {
            it.readLine() == "#EXTM3U" && it.readLine() == "#LISTENING-PLAYLIST"
        } == true

    private fun children(parent: Uri): List<DocumentFile> {
        val path = DocumentsContract.buildChildDocumentsUriUsingTree(tree, DocumentsContract.getDocumentId(parent))
        return resolver.query(path, arrayOf(Document.COLUMN_DOCUMENT_ID, Document.COLUMN_DISPLAY_NAME), null, null, null)
            ?.use { cursor ->
                buildList {
                    while (cursor.moveToNext()) add(DocumentFile(
                        DocumentsContract.buildDocumentUriUsingTree(tree, cursor.getString(0)), cursor.getString(1)))
                }
            } ?: throw IOException("The card folder is unavailable.")
    }

    companion object {
        private val probeRange = Regex("bytes 0-0/([0-9]+)")
        private val hashPattern = Regex("[a-f0-9]{64}")
        private val formats = setOf(".flac", ".m4a", ".aac", ".opus", ".ogg", ".mp3", ".wav")

        private fun safe(name: String): String =
            name.replace(Regex("""[\p{Cntrl}\\/:*?"<>|\[\]]"""), " ").trim().take(36).trim().ifBlank { "Music" }

        private fun hash(input: InputStream): String {
            val digest = MessageDigest.getInstance("SHA-256")
            val chunk = ByteArray(128 * 1024)
            while (true) {
                val size = input.read(chunk)
                if (size < 0) break
                digest.update(chunk, 0, size)
            }
            return digest.digest().joinToString("") { "%02x".format(it) }
        }

        private fun key(text: String) = text.byteInputStream().use(::hash)

        fun tracks(room: Room, playlist: SavedPlaylist): List<SaveTrack> {
            val tracks = mutableListOf<SaveTrack>()
            do {
                val path = "/api/tracks?playlist=${Room.encode(playlist.key)}&filter=ready&limit=100&offset=${tracks.size}"
                val page = room.get(path)
                val rows = page.getJSONArray("tracks")
                for (index in 0 until rows.length()) {
                    val row = rows.getJSONObject(index)
                    val label = listOf(row.optString("artist"), row.getString("title")).filter(String::isNotBlank).joinToString(" - ")
                    tracks += SaveTrack(row.getString("audio"), label, row.optDouble("duration", 0.0))
                }
            } while (rows.length() > 0 && tracks.size < page.getInt("total"))
            return tracks
        }
    }
}
