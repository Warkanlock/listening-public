package music.listening.room

import android.content.Context
import android.content.pm.PackageInfo
import android.content.pm.PackageManager
import android.os.Build
import okhttp3.Call
import okhttp3.OkHttpClient
import okhttp3.Request
import java.io.File
import java.io.IOException
import java.io.InputStream
import java.io.OutputStream
import java.security.MessageDigest
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicReference

internal const val MAX_RELEASE_APK_BYTES = 128L * 1024 * 1024

internal data class ReleaseApk(val version: String, val url: String, val sha256: String, val size: Long)

private const val RELEASE_ASSETS = "https://github.com/Warkanlock/listening-public/releases/download"
private val releaseVersion = Regex("[0-9]+\\.[0-9]+\\.[0-9]+")
private val apkDigest = Regex("[0-9a-f]{64}")
private val releaseAssetHosts = setOf("release-assets.githubusercontent.com", "objects.githubusercontent.com")
private val downloadWorker = Executors.newSingleThreadExecutor()
private val deliveredApks = mutableSetOf<File>()
private val downloadClient = OkHttpClient.Builder()
    .callTimeout(2, TimeUnit.MINUTES)
    .readTimeout(20, TimeUnit.SECONDS)
    .followRedirects(false)
    .addInterceptor { chain ->
        var request = chain.request()
        repeat(3) {
            val response = chain.proceed(request)
            if (!response.isRedirect) return@addInterceptor response
            request = response.use {
                val next = response.header("Location")?.let(request.url::resolve)
                if (next == null || next.scheme != "https" || next.port != 443 || next.host !in releaseAssetHosts ||
                    next.username.isNotEmpty() || next.password.isNotEmpty()) {
                    throw IOException("GitHub returned an invalid APK download location.")
                }
                request.newBuilder().url(next).build()
            }
        }
        throw IOException("GitHub redirected the APK download too many times.")
    }
    .build()

internal class ApkTransfer(context: Context, private val release: ReleaseApk) {
    private val context = context.applicationContext
    private val started = AtomicBoolean()
    private val cancelled = AtomicBoolean()
    private val activeCall = AtomicReference<Call?>()

    fun start(onComplete: (Result<File>) -> Unit) {
        check(started.compareAndSet(false, true))
        downloadWorker.execute {
            val result = runCatching { verifiedApk() }
            if (!cancelled.get()) {
                result.getOrNull()?.let(deliveredApks::add)
                onComplete(result)
            }
        }
    }

    fun cancel() {
        cancelled.set(true)
        activeCall.get()?.cancel()
    }

    private fun ensureActive() {
        if (cancelled.get()) throw IOException("APK download cancelled.")
    }

    private fun verifiedApk(): File {
        ensureActive()
        val expectedUrl = "$RELEASE_ASSETS/v${release.version}/listening-room-${release.version}.apk"
        if (!releaseVersion.matches(release.version) || release.url != expectedUrl ||
            !apkDigest.matches(release.sha256) || release.size !in 1..MAX_RELEASE_APK_BYTES) {
            throw IOException("The APK release metadata is invalid.")
        }

        val directory = File(context.cacheDir, "apk-updates")
        if (!directory.isDirectory && !directory.mkdirs()) throw IOException("Couldn't open the APK download cache.")
        val apk = File(directory, "listening-room-${release.version}-${release.sha256}.apk")
        pruneCache(directory, apk)

        if (apk.isFile && runCatching { apk.inputStream().use { copyAndVerify(it) } }.isSuccess) {
            ensureActive()
            verifyPackage(apk)
            return apk
        }

        val staged = File.createTempFile("listening-part-", ".apk", directory)
        try {
            val request = Request.Builder().url(release.url).header("Accept-Encoding", "identity").build()
            val call = downloadClient.newCall(request)
            activeCall.set(call)
            try {
                ensureActive()
                call.execute().use { response ->
                    if (!response.isSuccessful) throw IOException("GitHub couldn't provide the APK.")
                    val body = response.body ?: throw IOException("GitHub returned an empty APK download.")
                    if (body.contentLength() >= 0 && body.contentLength() != release.size) {
                        throw IOException("The APK download size differs from the release.")
                    }
                    staged.outputStream().use { copyAndVerify(body.byteStream(), it) }
                }
            } finally {
                activeCall.set(null)
            }
            verifyPackage(staged)
            ensureActive()
            if (!staged.renameTo(apk)) throw IOException("Couldn't store the verified APK.")
            return apk
        } finally {
            staged.delete()
        }
    }

    private fun copyAndVerify(source: InputStream, target: OutputStream? = null) {
        val digest = MessageDigest.getInstance("SHA-256")
        val buffer = ByteArray(DEFAULT_BUFFER_SIZE)
        var bytes = 0L
        while (true) {
            ensureActive()
            val count = source.read(buffer)
            if (count < 0) break
            bytes += count
            if (bytes > release.size) throw IOException("The APK download size differs from the release.")
            digest.update(buffer, 0, count)
            target?.write(buffer, 0, count)
        }
        if (bytes != release.size) throw IOException("The APK download size differs from the release.")
        val actual = digest.digest().joinToString("") { "%02x".format(it) }
        if (actual != release.sha256) throw IOException("The APK did not match GitHub's checksum.")
    }

    @Suppress("DEPRECATION")
    private fun verifyPackage(apk: File) {
        val flags = if (Build.VERSION.SDK_INT >= 28) PackageManager.GET_SIGNING_CERTIFICATES else PackageManager.GET_SIGNATURES
        val manager = context.packageManager
        val installed = manager.getPackageInfo(context.packageName, flags)
        val downloaded = manager.getPackageArchiveInfo(apk.absolutePath, flags)
            ?: throw IOException("Android couldn't read the downloaded APK.")
        if (downloaded.packageName != context.packageName || downloaded.versionName != release.version ||
            downloaded.updateVersionCode <= installed.updateVersionCode) {
            throw IOException("The APK isn't a newer version of the installed app.")
        }
        val installedSigners = installed.updateSigners
        if (installedSigners.isEmpty() || downloaded.updateSigners != installedSigners) {
            throw IOException("The APK's signing certificate doesn't match the installed app.")
        }
    }
}

@Suppress("DEPRECATION")
private val PackageInfo.updateVersionCode: Long
    get() = if (Build.VERSION.SDK_INT >= 28) longVersionCode else versionCode.toLong()

@Suppress("DEPRECATION")
private val PackageInfo.updateSigners
    get() = (if (Build.VERSION.SDK_INT >= 28) signingInfo?.apkContentsSigners else signatures).orEmpty().toSet()

private fun pruneCache(directory: File, selected: File) {
    deliveredApks.removeAll { !it.isFile }
    val files = directory.listFiles().orEmpty()
    files.filter { it.name.startsWith("listening-part-") }.forEach(File::delete)
    files.filter { it != selected && it !in deliveredApks && it.name.startsWith("listening-room-") }
        .sortedByDescending(File::lastModified)
        .drop(1)
        .forEach(File::delete)
}
