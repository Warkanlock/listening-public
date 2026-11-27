package music.listening.room

import android.app.Activity
import android.content.ActivityNotFoundException
import android.content.Intent
import android.net.Uri
import android.view.View
import android.widget.ScrollView
import okhttp3.Call
import okhttp3.Callback
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import org.json.JSONException
import org.json.JSONObject
import java.io.IOException
import java.util.concurrent.TimeUnit

private const val RELEASE_REPO = "Warkanlock/listening-public"
private const val RELEASES = "https://github.com/$RELEASE_REPO/releases"
private const val MAX_METADATA_BYTES = 512L * 1024
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
    data class Available(val version: String, val url: String) : ApkUpdate
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
    val hasApk = (0 until assets.length()).any { index ->
        val asset = assets.getJSONObject(index)
        asset.optString("name") == filename && asset.optString("state") == "uploaded" &&
            asset.optString("browser_download_url") == url
    }
    if (!hasApk) return ApkUpdate.Unavailable("The latest release doesn't include a Listening APK.")
    val current = ReleaseVersion.parse(installed)
        ?: return ApkUpdate.Unavailable("Couldn't compare the installed version with $version.")
    return if (latest > current) ApkUpdate.Available(version, url) else ApkUpdate.Current(version)
}

internal fun Activity.showApkUpdate() {
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
        status.addView(style.progress("Checking the latest release…"))
        content.section(button("Open latest release on GitHub") { openInBrowser("$RELEASES/latest") })
        content.section(browserFallback)
    }

    val request = Request.Builder().url("https://api.github.com/repos/$RELEASE_REPO/releases/latest")
        .header("Accept", "application/vnd.github+json")
        .build()
    val call = releaseClient.newCall(request)
    dialog.setOnDismissListener { call.cancel() }
    dialog.show()

    fun render(result: ApkUpdate) = runOnUiThread {
        if (!dialog.isShowing || isFinishing || isDestroyed) return@runOnUiThread
        status.removeAllViews()
        with(ui) {
            when (result) {
                is ApkUpdate.Current -> status.addView(label("No newer APK is available. The latest release is ${result.version}."))
                is ApkUpdate.Available -> {
                    status.addView(label("Version ${result.version} is available. Download the APK, then open it to install the update."))
                    status.addView(button("Download APK", solid = true) { openInBrowser(result.url) })
                }
                is ApkUpdate.Unavailable -> status.addView(label(result.message))
            }
        }
    }

    val unavailable = ApkUpdate.Unavailable("Couldn't check for an update. Try again or open the latest release on GitHub.")
    call.enqueue(object : Callback {
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
