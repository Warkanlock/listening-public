package music.listening.room

import android.content.Context
import okhttp3.Call
import okhttp3.Request
import java.io.File
import java.io.IOException
import java.util.concurrent.Executors

/** Complete, private copies of the tracks this playback service can reach offline. */
internal class QueueAudioBuffer(context: Context) {
    companion object {
        private const val LIMIT = 128L * 1024 * 1024
        private fun preferences(context: Context) =
            context.getSharedPreferences("playback", Context.MODE_PRIVATE)
        fun ahead(context: Context) = preferences(context).getInt("buffer_ahead", 1).coerceIn(1, 3)
        fun setAhead(context: Context, count: Int) =
            preferences(context).edit().putInt("buffer_ahead", count).apply()
    }

    private val directory = File(context.cacheDir, "room-playback").apply {
        mkdirs()
        listFiles()?.forEach { it.delete() }
    }
    private val worker = Executors.newSingleThreadExecutor()
    private val files = mutableMapOf<String, File>()
    private val unavailable = mutableSetOf<String>()
    private var wanted = emptyList<String>()
    private var transfer: Call? = null
    private var fetching: String? = null
    private var filling = false
    private var closed = false

    @Synchronized fun file(url: String): File? = files[url]?.takeIf(File::isFile)

    @Synchronized fun readyCount(): Int = files.values.count(File::isFile)

    @Synchronized fun prepare(room: Room, urls: List<String>) {
        if (closed) return
        val next = urls.distinct()
        if (next != wanted) unavailable.clear()
        wanted = next
        for (url in files.keys.toList()) {
            if (url !in wanted || files[url]?.isFile != true) files.remove(url)?.delete()
        }
        if (fetching !in wanted) transfer?.cancel()
        if (!filling && wanted.any { it !in files && it !in unavailable }) {
            filling = true
            worker.execute { fill(room) }
        }
    }

    private fun fill(room: Room) {
        val attempted = mutableSetOf<String>()
        try {
            while (true) {
                val (url, remaining) = synchronized(this) {
                    val next = wanted.firstOrNull { it !in files && it !in unavailable && it !in attempted } ?: return
                    next to LIMIT - files.values.sumOf(File::length)
                }
                attempted += url
                if (remaining <= 0) return
                val cached = try {
                    download(room, url, remaining)
                } catch (_: IOException) {
                    return
                }
                synchronized(this) {
                    transfer = null
                    fetching = null
                    if (cached != null) {
                        if (!closed && url in wanted) files[url] = cached else cached.delete()
                    } else if (!closed && url in wanted) unavailable += url
                }
            }
        } finally {
            synchronized(this) {
                transfer = null
                fetching = null
                filling = false
            }
        }
    }

    private fun download(room: Room, url: String, remaining: Long): File? {
        val call = room.network.newCall(Request.Builder().url(url).header("Cookie", room.cookieHeader).build())
        synchronized(this) {
            if (closed || url !in wanted) return null
            fetching = url
            transfer = call
        }
        call.execute().use { response ->
            if (response.code != 200) throw IOException("Audio could not be buffered.")
            val body = response.body ?: return null
            if (!body.contentType().toString().startsWith("audio/") || body.contentLength() > remaining) return null
            val scratch = File.createTempFile("audio-", ".part", directory)
            var complete = false
            try {
                scratch.outputStream().use { output ->
                    val input = body.byteStream()
                    val bytes = ByteArray(64 * 1024)
                    var size = 0L
                    while (true) {
                        val count = input.read(bytes)
                        if (count == -1) break
                        size += count
                        if (size > remaining) return null
                        output.write(bytes, 0, count)
                    }
                    complete = size > 0L
                }
                return scratch.takeIf { complete }
            } finally {
                if (!complete) scratch.delete()
            }
        }
    }

    @Synchronized fun close() {
        closed = true
        wanted = emptyList()
        transfer?.cancel()
        worker.shutdownNow()
        files.values.forEach { it.delete() }
        files.clear()
    }
}
