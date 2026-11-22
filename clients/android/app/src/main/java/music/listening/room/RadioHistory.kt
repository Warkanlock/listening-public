package music.listening.room

import android.content.Context
import android.net.Uri
import org.json.JSONArray
import org.json.JSONObject

internal enum class RadioSource { LIBRARY, LIVE, YOUTUBE }

internal data class RadioHistory(
    val source: RadioSource, val name: String, val ref: String, val title: String, val artist: String
) {
    fun snapshot(): JSONObject = JSONObject().put("radio", true).put("radio_pending", true)
        .put("radio_external", source == RadioSource.YOUTUBE)
        .put("current", JSONObject().put("title", title).put("artist", artist).apply {
            if (source == RadioSource.LIBRARY) put("id", ref)
        })
        .put("current_ref", ref).put("playing", false).put("position", 0)

    fun available(stations: JSONObject): Boolean = when (source) {
        RadioSource.LIBRARY -> true
        RadioSource.LIVE -> stations.optJSONObject("live")?.optJSONObject(name)?.optString("url") == ref
        RadioSource.YOUTUBE -> {
            val mixes = stations.optJSONArray("stations")
            stations.optBoolean("youtube_enabled", true) && mixes != null &&
                (0 until mixes.length()).any { index ->
                    val mix = mixes.optJSONObject(index)
                    mix?.optString("name") == name && mixUri(videoIds(mix?.optJSONArray("seeds")))?.toString() == ref
                }
        }
    }

    fun save(context: Context, device: String) {
        val value = JSONObject().put("source", source.name).put("name", name).put("ref", ref)
            .put("title", title).put("artist", artist)
        prefs(context).edit().putString(device, value.toString()).apply()
    }

    companion object {
        fun liveUri(ref: String): Uri? = Uri.parse(ref).takeIf {
            it.scheme == "https" && it.encodedAuthority == "ice2.somafm.com" &&
                it.encodedPath?.matches(Regex("/[A-Za-z0-9_-]+-128-mp3")) == true &&
                it.encodedQuery == null && it.fragment == null
        }

        fun videoIds(seeds: JSONArray?): List<String> =
            (0 until (seeds?.length() ?: 0)).mapNotNull { index ->
                seeds?.optString(index)?.takeIf { it.matches(Regex("[A-Za-z0-9_-]{11}")) }
            }

        fun mixUri(ids: List<String>): Uri? = if (ids.isEmpty()) null else
            Uri.parse("https://www.youtube.com/watch_videos").buildUpon()
                .appendQueryParameter("video_ids", ids.joinToString(",")).build()

        fun youtubeUri(ref: String): Uri? = Uri.parse(ref).takeIf {
            it.scheme == "https" && it.encodedAuthority == "www.youtube.com" && it.encodedPath == "/watch_videos"
        }

        fun read(context: Context, device: String): RadioHistory? {
            val stored = prefs(context).getString(device, null) ?: return null
            return runCatching {
                val item = JSONObject(stored)
                RadioHistory(RadioSource.valueOf(item.getString("source")), item.getString("name"),
                    item.getString("ref"), item.getString("title"), item.getString("artist"))
            }.getOrNull()
        }

        fun clear(context: Context, device: String) {
            prefs(context).edit().remove(device).apply()
        }

        private fun prefs(context: Context) = context.getSharedPreferences("radio-history", Context.MODE_PRIVATE)
    }
}
