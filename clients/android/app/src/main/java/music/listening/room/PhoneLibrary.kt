package music.listening.room

import android.content.ContentValues
import android.content.Context
import android.database.sqlite.SQLiteDatabase
import android.database.sqlite.SQLiteOpenHelper
import android.media.MediaMetadataRetriever
import android.net.Uri
import android.provider.OpenableColumns
import org.json.JSONObject
import java.io.File
import java.io.IOException
import java.io.InputStream
import java.security.MessageDigest
import java.util.UUID

private const val CHUNK = 128 * 1024
internal const val MAX_IMPORT = 512L * 1024 * 1024

/** One app-private SQLite replica per pairing. A committed original always has all its chunks. */
internal class PhoneLibrary private constructor(val app: Context, val device: String) :
    SQLiteOpenHelper(app, "phone-${UUID.fromString(device)}.sqlite3", null, 1) {
    val options = app.getSharedPreferences("offline-$device", Context.MODE_PRIVATE)
    var enabled: Boolean
        get() = options.getBoolean("enabled", false)
        set(value) { options.edit().putBoolean("enabled", value).apply() }
    var local: Boolean
        get() = enabled && options.getBoolean("local", true)
        set(value) { options.edit().putBoolean("local", value).apply() }

    init { setWriteAheadLoggingEnabled(true) }

    override fun onCreate(db: SQLiteDatabase) {
        db.execSQL("CREATE TABLE originals(hash TEXT PRIMARY KEY, size INTEGER NOT NULL, suffix TEXT NOT NULL, imported INTEGER DEFAULT 0, seen TEXT)")
        db.execSQL("CREATE TABLE chunks(hash TEXT, part INTEGER, bytes BLOB NOT NULL, PRIMARY KEY(hash,part)) WITHOUT ROWID")
        db.execSQL("CREATE TABLE songs(key TEXT PRIMARY KEY, title TEXT, artist TEXT, album TEXT, details TEXT NOT NULL, hash TEXT, wanted TEXT, round TEXT)")
        db.execSQL("CREATE INDEX song_audio ON songs(hash)")
    }
    override fun onUpgrade(db: SQLiteDatabase, old: Int, new: Int) = Unit

    private fun <T> write(block: (SQLiteDatabase) -> T): T {
        val db = writableDatabase
        db.beginTransactionNonExclusive()
        try {
            val result = block(db)
            db.setTransactionSuccessful()
            return result
        } finally { db.endTransaction() }
    }

    fun bind(collection: String) {
        require(collection.matches(Regex("[a-f0-9]{32}"))) { "Invalid collection identity." }
        val previous = options.getString("collection", null)
        require(previous == null || previous == collection) { "This server now serves another collection. Your phone copy is safe." }
        if (previous == null) check(options.edit().putString("collection", collection).commit())
    }

    fun has(hash: String) = readableDatabase.rawQuery("SELECT 1 FROM originals WHERE hash=?", arrayOf(hash)).use { it.moveToFirst() }

    private fun saveSong(db: SQLiteDatabase, track: JSONObject, hash: String?) {
        val values = ContentValues().apply {
            put("key", track.getString("key")); put("title", track.getString("title"))
            put("artist", track.optString("artist")); put("album", track.optString("album"))
            put("details", track.toString())
        }
        db.insertWithOnConflict("songs", null, values, SQLiteDatabase.CONFLICT_IGNORE)
        if (hash != null) values.put("hash", hash)
        db.update("songs", values, "key=?", arrayOf(track.getString("key")))
    }

    fun catalogPage(tracks: org.json.JSONArray, round: String) = write { db ->
        for (i in 0 until tracks.length()) {
            val track = tracks.getJSONObject(i)
            val hash = track.optJSONObject("original")?.getString("hash")
            if (hash != null) db.execSQL("UPDATE originals SET seen=? WHERE hash=?", arrayOf(round, hash))
            saveSong(db, track, hash?.takeIf(::has))
            db.execSQL("UPDATE songs SET round=?,wanted=? WHERE key=?", arrayOf(round, hash, track.getString("key")))
        }
    }

    fun downloads(round: String, after: Long) = readableDatabase.rawQuery(
        "SELECT rowid,details FROM songs WHERE round=? AND wanted IS NOT NULL AND wanted IS NOT hash AND rowid>? ORDER BY rowid LIMIT 40",
        arrayOf(round, after.toString())).use { c ->
        buildList { while (c.moveToNext()) add(c.getLong(0) to JSONObject(c.getString(1))) }
    }

    fun store(input: InputStream, hash: String, size: Long, suffix: String, track: JSONObject, round: String?) = write { db ->
        require(hash.matches(Regex("[a-f0-9]{64}")) && size > 0)
        if (!has(hash)) {
            if (app.filesDir.usableSpace < size + (16L shl 20)) throw IOException("Phone storage is full. Saved songs are still playable.")
            val buffer = ByteArray(CHUNK)
            val digest = MessageDigest.getInstance("SHA-256")
            var total = 0L
            var part = 0
            while (total < size) {
                val needed = minOf(CHUNK.toLong(), size - total).toInt()
                var n = 0
                while (n < needed) {
                    val read = input.read(buffer, n, needed - n)
                    if (read < 1) throw IOException("Audio transfer ended early.")
                    n += read
                }
                digest.update(buffer, 0, n)
                db.execSQL("INSERT INTO chunks VALUES(?,?,?)", arrayOf(hash, part++, if (n == CHUNK) buffer else buffer.copyOf(n)))
                total += n
            }
            if (input.read() != -1 || digest.digest().hex() != hash) throw IOException("Audio checksum mismatch. Sync again.")
            db.execSQL("INSERT INTO originals(hash,size,suffix) VALUES(?,?,?)", arrayOf<Any>(hash, size, suffix))
        }
        if (round == null) db.execSQL("UPDATE originals SET imported=1 WHERE hash=?", arrayOf(hash))
        else db.execSQL("UPDATE originals SET seen=? WHERE hash=?", arrayOf(round, hash))
        saveSong(db, track, hash)
    }

    fun importAudio(uri: Uri) {
        require(options.contains("collection")) { "Connect once and sync the catalog before adding phone songs." }
        val resolver = app.contentResolver
        val name = resolver.query(uri, arrayOf(OpenableColumns.DISPLAY_NAME), null, null, null)?.use {
            if (it.moveToFirst()) it.getString(0) else null
        } ?: throw IOException("Could not read the song filename.")
        val suffix = "." + name.substringAfterLast('.').lowercase()
        require(suffix in setOf(".mp3", ".flac", ".m4a", ".aac")) { "Choose an MP3, FLAC, M4A or AAC file." }
        val temp = File.createTempFile("song-", suffix, app.cacheDir)
        try {
            val digest = MessageDigest.getInstance("SHA-256")
            val input = resolver.openInputStream(uri) ?: throw IOException("Could not open that song.")
            input.use { stream -> temp.outputStream().use { out ->
                val block = ByteArray(CHUNK)
                var size = 0L
                while (true) {
                    val n = stream.read(block)
                    if (n < 0) break
                    size += n
                    require(size <= MAX_IMPORT) { "Choose a song smaller than 512 MiB." }
                    out.write(block, 0, n); digest.update(block, 0, n)
                }
            } }
            val hash = digest.digest().hex()
            val meta = MediaMetadataRetriever()
            val track = try {
                meta.setDataSource(temp.path)
                require(meta.extractMetadata(MediaMetadataRetriever.METADATA_KEY_HAS_AUDIO) == "yes") { "No playable audio in that file." }
                JSONObject().put("key", "phone:$hash")
                    .put("title", meta.extractMetadata(MediaMetadataRetriever.METADATA_KEY_TITLE) ?: name.substringBeforeLast('.'))
                    .put("artist", meta.extractMetadata(MediaMetadataRetriever.METADATA_KEY_ARTIST) ?: "Unknown artist")
                    .put("album", meta.extractMetadata(MediaMetadataRetriever.METADATA_KEY_ALBUM) ?: "")
                    .put("duration", (meta.extractMetadata(MediaMetadataRetriever.METADATA_KEY_DURATION)?.toLongOrNull() ?: 0) / 1000.0)
                    .put("status", "imported")
            } finally { meta.release() }
            temp.inputStream().use { store(it, hash, temp.length(), suffix, track, null) }
        } finally { temp.delete() }
    }

    fun stats() = readableDatabase.rawQuery("SELECT count(*),coalesce(sum(size),0) FROM originals", null).use {
        it.moveToFirst(); it.getInt(0) to it.getLong(1)
    }

    private val visible = """NOT (s.key='phone:'||s.hash AND EXISTS
        (SELECT 1 FROM songs t WHERE t.hash=s.hash AND t.key!=s.key))"""

    fun songs(query: String, offset: Int): Pair<Int, List<JSONObject>> {
        val filter = "$visible AND instr(lower(s.title||' '||s.artist||' '||s.album), lower(?))>0"
        val db = readableDatabase
        val count = db.rawQuery("SELECT count(*) FROM songs s WHERE $filter", arrayOf(query)).use { it.moveToFirst(); it.getInt(0) }
        return count to db.rawQuery("SELECT details,hash FROM songs s WHERE $filter ORDER BY s.title COLLATE NOCASE,s.key LIMIT 20 OFFSET ?", arrayOf(query, offset.toString())).use {
            buildList { while (it.moveToNext()) add(JSONObject(it.getString(0)).put("local_hash", it.getString(1))) }
        }
    }

    fun playing(key: String, direction: Int = 0): JSONObject? {
        // Rowid provides a stable traversal as catalog pages are appended.
        val pick = when {
            direction > 0 -> "s.rowid>(SELECT rowid FROM songs WHERE key=?) ORDER BY s.rowid"
            direction < 0 -> "s.rowid<(SELECT rowid FROM songs WHERE key=?) ORDER BY s.rowid DESC"
            else -> "s.key=?"
        }
        return readableDatabase.rawQuery("SELECT details,hash FROM songs s WHERE s.hash IS NOT NULL AND $visible AND $pick LIMIT 1", arrayOf(key)).use {
            if (it.moveToFirst()) JSONObject(it.getString(0)).put("local_hash", it.getString(1)) else null
        }
    }

    fun missingImports(round: String) = readableDatabase.rawQuery(
        """SELECT o.hash,o.size,o.suffix,s.details FROM originals o JOIN songs s ON s.key='phone:'||o.hash
           WHERE o.imported=1 AND coalesce(o.seen,'')!=?""", arrayOf(round)
    ).use { c -> buildList { while (c.moveToNext()) add(Original(c.getString(0), c.getLong(1), c.getString(2), JSONObject(c.getString(3)))) } }

    fun length(hash: String) = readableDatabase.rawQuery("SELECT size FROM originals WHERE hash=?", arrayOf(hash)).use {
        if (!it.moveToFirst()) throw IOException("Song has not been copied yet.")
        it.getLong(0)
    }

    fun stream(hash: String, start: Long = 0): InputStream = object : InputStream() {
        private val length = length(hash)
        private var at = start
        private var part = -1L
        private var data = ByteArray(0)
        override fun read(): Int {
            val one = ByteArray(1)
            return if (read(one, 0, 1) == -1) -1 else one[0].toInt() and 255
        }
        override fun read(b: ByteArray, off: Int, len: Int): Int {
            if (len == 0) return 0
            if (at >= length) return -1
            if (part != at / CHUNK) {
                part = at / CHUNK
                data = readableDatabase.rawQuery("SELECT bytes FROM chunks WHERE hash=? AND part=?", arrayOf(hash, part.toString())).use {
                    if (!it.moveToFirst()) throw IOException("Missing audio chunk.")
                    it.getBlob(0)
                }
            }
            val skip = (at % CHUNK).toInt()
            val n = minOf(data.size - skip, len)
            data.copyInto(b, off, skip, skip + n)
            at += n
            return n
        }
    }

    companion object {
        private val libraries = mutableMapOf<String, PhoneLibrary>()
        @Synchronized fun get(context: Context, device: String) =
            libraries.getOrPut(device) { PhoneLibrary(context.applicationContext, device) }
        @Synchronized fun remove(context: Context, device: String) {
            libraries.remove(device)?.close()
            context.deleteDatabase("phone-${UUID.fromString(device)}.sqlite3")
            context.getSharedPreferences("offline-$device", Context.MODE_PRIVATE).edit().clear().commit()
        }
    }
}

internal data class Original(val hash: String, val size: Long, val suffix: String, val track: JSONObject)
private fun ByteArray.hex() = joinToString("") { "%02x".format(it) }
