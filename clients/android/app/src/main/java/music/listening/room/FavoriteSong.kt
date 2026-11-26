package music.listening.room

import org.json.JSONObject

/** A favorite in the room library or a verified download outside its catalog. */
internal sealed class FavoriteSong(val ref: String, val field: String, val key: Long) {
    class Catalog(index: Long) : FavoriteSong(index.toString(), "index", index)
    class Download(requestId: Long) : FavoriteSong("custom-$requestId", "request_id", requestId)

    companion object {
        fun from(track: JSONObject): FavoriteSong? {
            if (track.opt("favorite") !is Boolean || track.has("local_hash")) return null
            return when (val id = track.opt("id")) {
                is Int -> id.takeIf { it > 0 }?.let { Catalog(it.toLong()) }
                is Long -> id.takeIf { it > 0 }?.let(::Catalog)
                is String -> track.optLong("request_id").takeIf { it > 0 && id == "custom-$it" }?.let(::Download)
                else -> null
            }
        }
    }
}
