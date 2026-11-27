package music.listening.room

import android.content.ActivityNotFoundException
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.view.View
import android.widget.ScrollView
import androidx.core.content.FileProvider
import okhttp3.Call
import okhttp3.Callback
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import org.json.JSONException
import org.json.JSONObject
import java.io.File
import java.io.IOException
import java.util.concurrent.TimeUnit

private const val RELEASE_REPO = "Warkanlock/listening-public"
private const val RELEASES = "https://github.com/$RELEASE_REPO/releases"
private const val MAX_METADATA_BYTES = 512L * 1024
internal const val APK_INSTALL_REQUEST = 82
internal class ApkUpdateProvider : FileProvider()

private val sha256Pattern = Regex("sha256:([0-9a-f]{64})")
private val releaseClient = OkHttpClient.Builder()
    .callTimeout(12, TimeUnit.SECONDS)
    .followRedirects(false)
    .build()

private data class ReleaseVersion(val major: Int, val minor: Int, val patch: Int) : Comparable<ReleaseVersion> {
    override fun compareTo(other: ReleaseVersion) = compareValuesBy(this, other, { it.major }, { it.minor }, { it.patch })

    companion object {
        private val syntax = Regex("""([0-9]+)\.([0-9]+)\.([0-9]+)""")

        fun parse(name: String): ReleaseVersion? {
            val parts = syntax.matchEntire(name)?.groupValues ?: return null
            return ReleaseVersion(parts[1].toIntOrNull() ?: return null,
                parts[2].toIntOrNull() ?: return null, parts[3].toIntOrNull() ?: return null)
        }
    }
}

private sealed interface ApkUpdate {
    data class Current(val version: String) : ApkUpdate
    data class Available(val apk: ReleaseApk) : ApkUpdate
    data class Unavailable(val message: String) : ApkUpdate
}

private fun latestApk(release: JSONObject, installed: String): ApkUpdate {
    val tag = release.getString("tag_name")
    val version = tag.removePrefix("v")
    val latest = ReleaseVersion.parse(version)
    if (!tag.startsWith("v") || latest == null || release.getBoolean("draft") || release.getBoolean("prerelease")) {
        return ApkUpdate.Unavailable("Couldn't read the latest release version.")
    }
    val filename = "listening-room-$version.apk"
    val url = "$RELEASES/download/$tag/$filename"
    val assets = release.getJSONArray("assets")
    val asset = (0 until assets.length()).asSequence().map(assets::getJSONObject).firstOrNull {
        it.optString("name") == filename && it.optString("state") == "uploaded" &&
            it.optString("browser_download_url") == url
    }
        ?: return ApkUpdate.Unavailable("The latest release doesn't include a Listening APK.")
    val current = ReleaseVersion.parse(installed)
        ?: return ApkUpdate.Unavailable("Couldn't compare the installed version with $version.")
    if (latest <= current) return ApkUpdate.Current(version)
    val digest = sha256Pattern.matchEntire(asset.optString("digest"))?.groupValues?.get(1)
    val size = asset.optLong("size")
    if (digest == null || size !in 1..MAX_RELEASE_APK_BYTES) {
        return ApkUpdate.Unavailable("GitHub hasn't provided a verifiable APK for $version.")
    }
    return ApkUpdate.Available(ReleaseApk(version, url, digest, size))
}

@Suppress("DEPRECATION")
private fun RoomActivity.openApkInstaller(file: File, onResult: (Boolean) -> Unit) {
    val apk = FileProvider.getUriForFile(this, "$packageName.apk-updates", file)
    val install = Intent(Intent.ACTION_INSTALL_PACKAGE)
        .setDataAndType(apk, "application/vnd.android.package-archive")
        .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
        .putExtra(Intent.EXTRA_RETURN_RESULT, true)
    val resolver = packageManager.resolveActivity(install, PackageManager.MATCH_DEFAULT_ONLY or PackageManager.MATCH_SYSTEM_ONLY)
        ?: throw ActivityNotFoundException("Android's package installer is unavailable.")
    install.setClassName(resolver.activityInfo.packageName, resolver.activityInfo.name)
    startApkInstall(install, onResult)
}

internal fun RoomActivity.showApkUpdate() {
    if (apkUpdateDialog?.isShowing == true) return
    @Suppress("DEPRECATION")
    val installed = packageManager.getPackageInfo(packageName, 0).versionName.orEmpty()
    val ui = RoomUi(this)
    val style = SongDetailsStyle(ui)
    val content = style.column()
    val status = ui.column(8).apply { accessibilityLiveRegion = View.ACCESSIBILITY_LIVE_REGION_POLITE }
    val browserFallback = ui.label("", 14f, ui.secondary).apply {
        setTextIsSelectable(true)
        accessibilityLiveRegion = View.ACCESSIBILITY_LIVE_REGION_POLITE
        visibility = View.GONE
    }
    val dialog = style.dialog(this, "APK updates", ScrollView(this).apply { addView(content) })
    var metadataCall: Call? = null
    var transfer: ApkTransfer? = null
    fun visible() = dialog.isShowing && !isFinishing && !isDestroyed

    fun openInBrowser(url: String) {
        try {
            startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(url)).addCategory(Intent.CATEGORY_BROWSABLE))
        } catch (_: ActivityNotFoundException) {
            browserFallback.text = "Open this link on another device.\n$url"
            browserFallback.visibility = View.VISIBLE
        }
    }

    with(ui) {
        content.section(style.fact("Installed version", installed.ifBlank { "Unknown" }))
        content.section(status)
        content.section(button("Open latest release on GitHub") { openInBrowser("$RELEASES/latest") })
        content.section(browserFallback)
    }

    fun showAvailable(apk: ReleaseApk, error: String? = null) {
        if (!visible()) return
        status.removeAllViews()
        status.addView(ui.label(error ?: "Version ${apk.version} is available."))
        status.addView(ui.button("Update and install", solid = true) {
            status.removeAllViews()
            status.addView(style.progress("Downloading and verifying version ${apk.version}…"))
            val download = ApkTransfer(applicationContext, apk)
            transfer = download
            download.start { result ->
                runOnUiThread {
                    if (!visible() || transfer !== download) return@runOnUiThread
                    transfer = null
                    result.onSuccess { file ->
                        fun install() {
                            status.removeAllViews()
                            status.addView(ui.label("The update is ready. Confirm installation in Android."))
                            try {
                                openApkInstaller(file) { installed ->
                                    if (installed && visible()) dialog.dismiss()
                                    else showAvailable(apk, "Installation ended. You can try again.")
                                }
                            } catch (_: ActivityNotFoundException) {
                                showAvailable(apk, "Android's installer is unavailable. Open the release on GitHub.")
                            } catch (_: SecurityException) {
                                showAvailable(apk, "Android blocked installation. Check the device's app install settings.")
                            }
                        }
                        if (dialog.window?.decorView?.hasWindowFocus() == true) install()
                        else {
                            status.removeAllViews()
                            status.addView(ui.label("Version ${apk.version} is ready to install."))
                            status.addView(ui.button("Install update", solid = true, action = ::install))
                        }
                    }.onFailure {
                        showAvailable(apk, it.message ?: "Couldn't download the update. Try again.")
                    }
                }
            }
        })
    }

    val request = Request.Builder().url("https://api.github.com/repos/$RELEASE_REPO/releases/latest")
        .header("Accept", "application/vnd.github+json")
        .build()
    val unavailable = ApkUpdate.Unavailable("Couldn't check for an update. Try again or open the latest release on GitHub.")

    fun checkForUpdate() {
        if (!visible() || metadataCall != null) return
        status.removeAllViews()
        status.addView(style.progress("Checking the latest release…"))
        val check = releaseClient.newCall(request)
        metadataCall = check

        fun render(result: ApkUpdate) = runOnUiThread {
            if (!visible() || metadataCall !== check) return@runOnUiThread
            metadataCall = null
            status.removeAllViews()
            with(ui) {
                when (result) {
                    is ApkUpdate.Current -> status.addView(label("No newer APK is available. The latest release is ${result.version}."))
                    is ApkUpdate.Available -> showAvailable(result.apk)
                    is ApkUpdate.Unavailable -> {
                        status.addView(label(result.message))
                        status.addView(button("Retry", action = ::checkForUpdate))
                    }
                }
            }
        }

        check.enqueue(object : Callback {
            override fun onFailure(call: Call, e: IOException) = render(unavailable)

            override fun onResponse(call: Call, response: Response) {
                val result = try {
                    response.use {
                        if (!it.isSuccessful) return@use unavailable
                        val body = it.peekBody(MAX_METADATA_BYTES + 1)
                        if (body.contentLength() > MAX_METADATA_BYTES) unavailable
                        else latestApk(JSONObject(body.string()), installed)
                    }
                } catch (_: IOException) {
                    unavailable
                } catch (_: JSONException) {
                    unavailable
                }
                render(result)
            }
        })
    }

    dialog.setOnDismissListener {
        if (apkUpdateDialog === dialog) apkUpdateDialog = null
        metadataCall?.cancel()
        metadataCall = null
        transfer?.cancel()
    }
    apkUpdateDialog = dialog
    dialog.show()
    checkForUpdate()
}
