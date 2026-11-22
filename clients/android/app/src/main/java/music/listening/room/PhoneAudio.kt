package music.listening.room

import android.net.Uri
import androidx.media3.common.C
import androidx.media3.datasource.BaseDataSource
import androidx.media3.datasource.DataSpec
import java.io.IOException
import java.io.InputStream

/** Seek directly into the original's SQLite chunks without staging a second copy. */
internal class PhoneAudio(private val library: PhoneLibrary) : BaseDataSource(false) {
    private var input: InputStream? = null
    private var uri: Uri? = null
    private var remaining = 0L

    override fun open(dataSpec: DataSpec): Long {
        transferInitializing(dataSpec)
        val hash = dataSpec.uri.lastPathSegment ?: throw IOException("Missing song identity.")
        val size = library.length(hash)
        if (dataSpec.position > size) throw IOException("Seek outside this song.")
        remaining = size - dataSpec.position
        if (dataSpec.length != C.LENGTH_UNSET.toLong()) remaining = minOf(remaining, dataSpec.length)
        input = library.stream(hash, dataSpec.position)
        uri = dataSpec.uri
        transferStarted(dataSpec)
        return remaining
    }

    override fun read(buffer: ByteArray, offset: Int, length: Int): Int {
        if (length == 0) return 0
        if (remaining == 0L) return C.RESULT_END_OF_INPUT
        val n = input!!.read(buffer, offset, minOf(length.toLong(), remaining).toInt())
        if (n < 0) throw IOException("Incomplete saved song.")
        remaining -= n
        bytesTransferred(n)
        return n
    }
    override fun getUri() = uri
    override fun close() {
        if (input != null) { input!!.close(); input = null; transferEnded() }
        uri = null
    }
}
