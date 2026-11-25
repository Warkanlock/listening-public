package music.listening.room

import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.graphics.ImageDecoder
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.os.SystemClock
import okhttp3.Call
import okhttp3.HttpUrl.Companion.toHttpUrlOrNull
import okhttp3.OkHttpClient
import okhttp3.Request
import java.io.IOException
import java.nio.ByteBuffer
import java.util.concurrent.Executors
import java.util.concurrent.Future
import java.util.concurrent.TimeUnit

/** Decodes cover art off the UI and playback threads. Nothing is written to storage. */
internal class SleeveArtwork(screenSide: Int, private val display: (Bitmap?) -> Unit) {
    companion object {
        const val MAX_BYTES = 4 * 1024 * 1024
        private const val MAX_SIDE = 704
        private val work = Executors.newSingleThreadExecutor()
        private val main = Handler(Looper.getMainLooper())
        // Imported artwork is public HTTPS. This client has no room credentials.
        private val images = OkHttpClient.Builder().followSslRedirects(false)
            .callTimeout(10, TimeUnit.SECONDS).build()

        private fun sampleSize(side: Int, limit: Int): Int {
            var sample = 1
            while (side / sample > limit) sample *= 2
            return sample
        }
    }

    // The same bitmap also fills the full-screen cover viewer.
    private val maxSide = screenSide.coerceIn(1, MAX_SIDE)

    private fun prepare(bytes: ByteArray): Bitmap? {
        if (Build.VERSION.SDK_INT >= 28) {
            return try {
                ImageDecoder.decodeBitmap(ImageDecoder.createSource(ByteBuffer.wrap(bytes))) { decoder, info, _ ->
                    decoder.allocator = ImageDecoder.ALLOCATOR_SOFTWARE
                    val width = info.size.width
                    val height = info.size.height
                    val side = maxOf(width, height)
                    val target = minOf(maxSide, side / sampleSize(side, MAX_SIDE))
                    if (side > target) {
                        decoder.setTargetSize((width.toLong() * target / side).toInt().coerceAtLeast(1),
                            (height.toLong() * target / side).toInt().coerceAtLeast(1))
                    }
                }
            } catch (_: IOException) { null }
        }
        val options = BitmapFactory.Options().apply { inJustDecodeBounds = true }
        BitmapFactory.decodeByteArray(bytes, 0, bytes.size, options)
        if (options.outWidth <= 0 || options.outHeight <= 0) return null
        options.inJustDecodeBounds = false
        options.inSampleSize = sampleSize(maxOf(options.outWidth, options.outHeight), maxSide)
        return BitmapFactory.decodeByteArray(bytes, 0, bytes.size, options)
    }

    private var ref = ""
    private var cover = ""
    private var embedded: ByteArray? = null
    private var serial = 0
    private var retryAt = Long.MAX_VALUE
    private var transfer: Call? = null
    private var job: Future<*>? = null

    fun clear() {
        serial++
        transfer?.cancel()
        job?.cancel(true)
        transfer = null
        job = null
        ref = ""
        cover = ""
        embedded = null
        retryAt = Long.MAX_VALUE
        display(null)
    }

    fun bind(track: String, url: String, picture: ByteArray?) {
        val sameSource = ref == track && cover == url && embedded === picture
        if (sameSource && SystemClock.elapsedRealtime() < retryAt) return
        clear()
        ref = track
        cover = url
        embedded = picture
        val endpoint = url.toHttpUrlOrNull()?.takeIf { it.isHttps && it.username.isEmpty() && it.password.isEmpty() }
        val call = endpoint?.let { images.newCall(Request.Builder().url(it).build()) }
        transfer = call
        if (picture == null && call == null) return
        val current = serial
        job = work.submit {
            var transientFailure = false
            val image = try {
                picture?.let(::prepare) ?: call?.execute()?.use { response ->
                    val body = response.body ?: return@use null
                    if (!response.isSuccessful) {
                        transientFailure = response.code in 500..599 || response.code == 408 || response.code == 429
                        return@use null
                    }
                    if (body.contentLength() > MAX_BYTES) return@use null
                    val input = body.source()
                    if (input.request(MAX_BYTES + 1L)) null else prepare(input.readByteArray())
                }
            } catch (_: IOException) {
                transientFailure = true
                null
            } catch (_: IllegalArgumentException) { null }
            main.post {
                if (serial == current) {
                    transfer = null
                    job = null
                    // At most one retry per on-screen source. A bad image should
                    // never spend more bandwidth just because the player polls.
                    if (image == null && transientFailure && !sameSource)
                        retryAt = SystemClock.elapsedRealtime() + 60_000L
                    display(image)
                }
            }
        }
    }
}
