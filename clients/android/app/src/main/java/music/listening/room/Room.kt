package music.listening.room

import android.content.Context
import android.os.Build
import org.json.JSONArray
import org.json.JSONObject
import java.io.IOException
import java.net.HttpCookie
import okhttp3.Call
import okhttp3.HttpUrl.Companion.toHttpUrlOrNull
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import java.net.URI
import java.net.URLDecoder
import java.net.URLEncoder
import java.util.UUID

class PairingRequired : IOException("Pairing expired. Use a fresh invitation from the room’s Devices settings.")
class RoomUnreachable(tailscale: Boolean, cause: IOException) : IOException(
    if (tailscale) "Room unreachable. Connect Tailscale and try again."
    else "Room unreachable. Check your network and try again.", cause)
class CatalogLoading : IOException("Catalog is still loading. Try again in a moment.")

private class Reply(val status: Int, val cookie: String?, val text: String)

internal class Room(val origin: String, val id: String, private val token: String, val network: RoomNetwork,
                    val replicaId: String = id, val cardOrigin: String = origin) {
    val cookieHeader: String get() = "lr_token=$token"

    companion object {
        private const val PREFS = "paired_room"
        private const val CARD_WIFI = "paired_room_wifi"
        private const val RECOVERY = "offline_pairing"

        fun load(context: Context, recovering: Boolean = false, homeWifi: Boolean = false,
                 onCall: (Call) -> Unit = {}): Room? {
            val names = when {
                recovering -> listOf(RECOVERY)
                homeWifi -> listOf(CARD_WIFI, PREFS)
                else -> listOf(PREFS)
            }
            for (name in names) {
                val prefs = context.getSharedPreferences(name, Context.MODE_PRIVATE)
                val origin = prefs.getString("origin", null) ?: continue
                val id = prefs.getString("device", null) ?: continue
                val token = prefs.getString("credential", null) ?: continue
                val proof = if (homeWifi) RoomProof(id, token) else null
                return Room(origin, id, token, RoomNetwork(context, origin, homeWifi, onCall, proof),
                    prefs.getString("replica", id) ?: id, prefs.getString("card_origin", origin) ?: origin)
            }
            return null
        }

        fun forget(context: Context) {
            for (name in listOf(PREFS, CARD_WIFI, RECOVERY))
                context.getSharedPreferences(name, Context.MODE_PRIVATE).edit().clear().apply()
        }

        private fun address(link: String): Pair<String, String?> {
            val uri = runCatching { URI(link.trim()) }.getOrNull()
            val scheme = uri?.scheme?.lowercase(java.util.Locale.ROOT)
            require(uri != null && scheme in setOf("http", "https") && uri.host != null &&
                uri.rawUserInfo == null && uri.rawFragment == null &&
                (uri.rawPath in setOf("", "/") || (uri.rawPath == "/pair" && uri.rawQuery != null)) &&
                (uri.port == -1 || uri.port in 1..65535)) {
                "Enter a full server URL starting with http:// or https://."
            }
            val query = uri.rawQuery
            require(query == null || (query.startsWith("token=") && '&' !in query)) {
                "Use the server URL or a pairing link from Devices settings."
            }
            val token = query?.substringAfter("=")?.let { URLDecoder.decode(it, "UTF-8") }
            require(token == null || token.isNotBlank()) { "The pairing link is missing its token." }
            val origin = requireNotNull("$scheme://${uri.rawAuthority}".toHttpUrlOrNull()) {
                "Check the server hostname and port."
            }.toString().removeSuffix("/")
            return origin to token
        }

        fun connect(context: Context, value: String, homeWifi: Boolean, expectedOrigin: String?): Connection {
            val (origin, token) = address(value)
            if (token != null || homeWifi || expectedOrigin != null) {
                return Connection.Paired(pair(context, value, homeWifi = homeWifi, expectedOrigin = expectedOrigin))
            }
            val network = RoomNetwork(context, origin)
            val request = Request.Builder().url("$origin/api/room/discover").build()
            try {
                network.newCall(request).execute().use { response ->
                    if (response.code == 204 && response.header("X-Listening-Room") == "1") {
                        return Connection.NeedsInvitation(origin)
                    }
                }
            } catch (error: IOException) {
                throw RoomUnreachable(network.tailscale, error)
            }
            throw IOException("No Listening Room found at this URL. Check the address and port.")
        }

        fun pair(context: Context, link: String, recover: PhoneLibrary? = null, homeWifi: Boolean = false,
                 expectedOrigin: String? = null): Room {
            val (origin, token) = address(link)
            require(expectedOrigin == null || origin == expectedOrigin) {
                "This invitation uses a different server URL. Go back to connect using that link."
            }
            if (homeWifi && token == null) {
                val identities = listOfNotNull(load(context, homeWifi = true), load(context))
                    .distinctBy { it.id to it.token }
                require(identities.isNotEmpty()) { "Pair this phone first with the invitation from Devices settings." }
                for (paired in identities) {
                    val network = RoomNetwork(context, origin, homeWifi = true, proof = RoomProof(paired.id, paired.token))
                    val local = Room(origin, paired.id, paired.token, network, paired.replicaId, paired.cardOrigin)
                    // The Wi-Fi route proves the room's identity before sending the device cookie.
                    try { local.requireCatalog() } catch (_: PairingRequired) { continue }
                    local.save(context, CARD_WIFI)
                    return local
                }
                throw PairingRequired()
            }
            requireNotNull(token) { "Paste a pairing link from the server’s Devices settings." }
            val useProof = homeWifi
            val invitationProof = if (useProof) RoomProof(null, token) else null
            val network = RoomNetwork(context, origin, homeWifi, proof = invitationProof)
            val exchange = fetch(network, origin, "/?token=${encode(token)}", null, null)
            checkPairingStatus(exchange, 303)
            if (exchange.cookie == null) throw PairingRequired()
            val id = UUID.randomUUID().toString()
            val registration = fetch(network, origin, "/api/device", exchange.cookie, registration(id))
            checkPairingStatus(registration, 200)
            if (registration.cookie == null || registration.cookie == exchange.cookie) {
                throw PairingRequired()
            }
            val routed = if (useProof) RoomNetwork(context, origin, homeWifi, proof = RoomProof(id, registration.cookie)) else network
            val room = Room(origin, id, registration.cookie, routed, recover?.device ?: id)
            room.save(context, when {
                recover != null -> RECOVERY
                homeWifi -> CARD_WIFI
                else -> PREFS
            })
            return room
        }

        private fun checkPairingStatus(reply: Reply, expected: Int) {
            if (reply.status == 401 || reply.status == 403) throw PairingRequired()
            if (reply.status != expected) throw IOException("The room returned HTTP ${reply.status}. Try again.")
        }

        fun finishRecovery(context: Context, library: PhoneLibrary): Room {
            val room = load(context, recovering = true) ?: throw IOException("Paste a fresh invitation first.")
            require(room.replicaId == library.device) { "That invitation belongs to another pairing." }
            library.bind(room.get("/api/replica/identity").getString("collection"))
            room.save(context, PREFS)
            context.getSharedPreferences(RECOVERY, Context.MODE_PRIVATE).edit().clear().apply()
            return room
        }

        fun encode(value: String): String = URLEncoder.encode(value, "UTF-8")
        private fun registration(id: String) = JSONObject().put("id", id)
            .put("name", Build.MODEL.take(60).ifBlank { "Android" })

        private fun fetch(network: RoomNetwork, origin: String, path: String, token: String?,
                          body: JSONObject?, output: String? = null, onCall: (Call) -> Unit = {}): Reply {
            require(path.startsWith("/") && !path.startsWith("//"))
            val request = Request.Builder().url(origin + path)
            if (token != null) request.header("Cookie", "lr_token=$token")
            if (output != null) request.header("X-Listening-Output", output)
                .header("X-Listening-Output-Name", encode(Build.MODEL.take(60)).replace("+", "%20"))
            if (body != null) request.header("Origin", origin)
                .post(body.toString().toRequestBody("application/json".toMediaType()))
            return try {
                network.newCall(request.build()).also(onCall).execute().use { response ->
                    val cookie = response.header("Set-Cookie")?.let { raw ->
                        HttpCookie.parse(raw).firstOrNull { it.name == "lr_token" }?.value
                    }
                    Reply(response.code, cookie, response.body?.string().orEmpty())
                }
            } catch (error: PairingRequired) {
                throw error
            } catch (error: IOException) {
                throw RoomUnreachable(network.tailscale, error)
            }
        }
    }

    sealed interface Connection {
        data class NeedsInvitation(val origin: String) : Connection
        data class Paired(val room: Room) : Connection
    }

    private fun save(context: Context, name: String) {
        check(context.getSharedPreferences(name, Context.MODE_PRIVATE).edit()
            .putString("origin", origin).putString("device", id).putString("replica", replicaId)
            .putString("card_origin", cardOrigin).putString("credential", token).commit())
    }

    private fun request(path: String, body: JSONObject? = null, output: String? = null,
                        onCall: (Call) -> Unit = {}): JSONObject {
        val reply = fetch(network, origin, path, token, body, output, onCall)
        if (reply.status == 401) throw PairingRequired()
        if (reply.status !in 200..299) {
            val reason = runCatching { JSONObject(reply.text).getString("error") }.getOrNull()
            val failure = IOException(reason ?: "Room returned HTTP ${reply.status}.")
            if (reply.status in setOf(502, 503, 504)) throw RoomUnreachable(network.tailscale, failure)
            throw failure
        }
        return JSONObject(reply.text)
    }

    fun requireCatalog() {
        val reply = fetch(network, origin, "/api/room", token, null)
        if (reply.status == 404) return // Older rooms expose the catalog directly.
        if (reply.status == 401) throw PairingRequired()
        if (reply.status != 200) throw IOException("The room returned HTTP ${reply.status}. Try again.")
        if (JSONObject(reply.text).optString("state") == "catalog_loading") throw CatalogLoading()
    }

    fun get(path: String) = request(path)
    fun requestWeeklySongs(ids: List<String>) =
        request("/api/discover/weekly", JSONObject().put("ids", JSONArray(ids)))
    fun searchCatalog(query: JSONObject, onCall: (Call) -> Unit) =
        request("/api/discovery/search", query, onCall = onCall)
    fun importCatalogSong(item: JSONObject) = request("/api/discovery/import", JSONObject()
        .put("artist", item.getString("creator")).put("title", item.getString("title"))
        .put("album", item.optString("album")).put("musicbrainz_id", item.opt("musicbrainz_id"))
        .put("apple_id", item.opt("apple_id")).put("duration_ms", item.opt("duration_ms")))
    fun requestCatalogCopy(id: Int) = request("/api/requests", JSONObject().put("track_id", id))
    fun addPlaylistSong(song: JSONObject) = request("/api/playlists/add-song", song, output = id)
    fun metadata(path: String, onCall: (Call) -> Unit) = request(path, onCall = onCall)
    fun extensionHealth(onCall: (Call) -> Unit) = request("/api/extensions/health", JSONObject(), onCall = onCall)
    fun pickRequest(item: JSONObject): JSONObject? {
        val artist = item.getString("artist")
        val title = item.getString("title")
        val mbid = item.optString("musicbrainz_id")
        // Older rooms ignore the query parameters and return the recent feed.
        if (artist.length > 200 || title.length > 200) return null
        val rows = get("/api/requests?artist=${encode(artist)}&title=${encode(title)}&musicbrainz_id=${encode(mbid)}").getJSONArray("requests")
        return (0 until rows.length()).asSequence().map { rows.getJSONObject(it) }.firstOrNull { row ->
            if (!row.isNull("track_index")) return@firstOrNull false
            if (mbid.isNotBlank()) {
                row.optString("musicbrainz_id") == mbid || row.optString("original_recording_id") == mbid
            } else {
                (row.optString("musicbrainz_id").isBlank() &&
                    row.optString("artist") == artist && row.optString("title") == title) ||
                    (row.optString("original_artist") == artist && row.optString("original_title") == title)
            }
        }
    }
    fun requestCopy(item: JSONObject, replacing: JSONObject? = null) = request("/api/requests", JSONObject().apply {
        for (key in listOf("artist", "title", "album", "musicbrainz_id", "duration", "version", "performance"))
            if (item.has(key)) put(key, item.get(key))
        if (replacing != null) {
            put("request_id", replacing.getLong("id"))
            put("format_revision", replacing.getLong("format_revision"))
        }
    })
    fun requestById(id: Long): JSONObject? {
        val rows = get("/api/requests?id=$id").getJSONArray("requests")
        return (0 until rows.length()).asSequence().map(rows::getJSONObject).firstOrNull { it.optLong("id") == id }
    }
    private fun copyRequestFields(row: JSONObject, format: String) =
        JSONObject().put("request_id", row.getLong("id")).put("format_revision", row.getLong("format_revision"))
            .put("requested_format", format)
    fun confirmFormat(row: JSONObject, format: String, copyKey: String? = null) = request("/api/requests/format",
        copyRequestFields(row, format).apply { if (copyKey != null) put("copy_key", copyKey) })
    fun browseCopies(row: JSONObject, format: String, fresh: Boolean = false) =
        request("/api/requests/copies", copyRequestFields(row, format).put("fresh", fresh))
    fun retryCopy(row: JSONObject) = request("/api/requests/retry", JSONObject().put("request_id", row.getLong("id")).apply {
        if (row.has("format_revision")) put("format_revision", row.getLong("format_revision"))
    })
    fun recordingVersions(item: JSONObject): List<JSONObject> {
        val id = item.optString("musicbrainz_id").ifBlank { item.optString("id") }
        val query = "${item.optString("artist").take(70)} - ${item.optString("title")}".take(160)
        val path = if (id.matches(Regex("[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}")))
            "/api/discover/recording/$id" else "/api/discover/matches?q=${encode(query)}"
        val leads = get(path).getJSONArray("leads")
        return (0 until leads.length()).mapNotNull { index ->
            val lead = leads.optJSONObject(index) ?: return@mapNotNull null
            lead.optJSONObject("track")?.takeIf {
                lead.optString("kind") == "recording" && it.optDouble("duration", 0.0) > 0 &&
                    it.optString("musicbrainz_id").let { mbid -> mbid.isNotBlank() && mbid != id }
            }
        }.distinctBy { it.optString("musicbrainz_id") }
    }
    fun heartbeat() = request("/api/device", registration(id))
    fun radioPresence(source: String?): Boolean {
        val body = JSONObject().put("output", id).put("source", source ?: JSONObject.NULL)
        val reply = fetch(network, origin, "/api/radio/presence", token, body)
        // Older rooms do not offer a Radio playback lease.
        return reply.status in 200..299 || reply.status == 404
    }
    fun session() = request("/api/session", output = id)
    fun insights() = request("/api/insights", output = id)
    fun action(op: String, fields: JSONObject = JSONObject()): JSONObject =
        request("/api/session", fields.put("op", op).put("device_id", id), output = id)

    fun playbackAudioUrl(path: String, profile: MobileAudio): String {
        val url = audioUrl(path, profile)
        val separator = if ('?' in url) "&" else "?"
        return url + separator + "output=" + encode(id)
    }

    fun audioUrl(path: String, profile: MobileAudio = MobileAudio.Original): String {
        require(Regex("""/api/audio/(custom/)?[0-9]+(?:\?recording=[a-f0-9]{64})?""").matches(path)) { "Invalid audio link from the room." }
        val separator = if ('?' in path) "&" else "?"
        return origin + path + if (profile == MobileAudio.Original) "" else separator + "format=" + profile.wire
    }
}
