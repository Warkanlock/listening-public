package music.listening.room

import android.app.Activity
import android.content.ActivityNotFoundException
import android.content.Intent
import android.net.Uri
import android.view.View
import android.view.inputmethod.EditorInfo
import android.view.inputmethod.InputMethodManager
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.Toast
import org.json.JSONArray
import org.json.JSONObject
import okhttp3.Call

/** Room credentials stay in the room HTTP client. External results open as ordinary web links. */
internal class CatalogBrowser(private val activity: Activity, private val ui: RoomUi,
                             configuration: JSONObject,
                             private val request: (JSONObject, (Call) -> Unit, (Result<JSONObject>) -> Unit) -> Unit,
                             private val add: (SongDraft) -> Unit,
                             private val importSong: (JSONObject, (Result<JSONObject>) -> Unit) -> Unit,
                             private val findCopy: (Int) -> Unit) : ScrollView(activity) {
    private val canImport = configuration.optBoolean("can_import")
    private val sources = configuration.getJSONArray("sources")
    private val body = ui.column()
    private val choices = ui.column()
    private val output = ui.column()
    private val query = ui.field("Song, artist or topic").apply {
        contentDescription = "Search catalogs"
        filters = arrayOf(android.text.InputFilter.LengthFilter(160))
        imeOptions = EditorInfo.IME_ACTION_SEARCH
        setOnEditorActionListener { _, action, _ ->
            if (action == EditorInfo.IME_ACTION_SEARCH) search(0) else false
        }
    }
    private var scope = "public"
    private var source = ""
    @Volatile private var revision = 0
    @Volatile private var pendingCall: Call? = null

    init {
        addView(body)
        body.addView(choices)
        body.addView(output)
        renderControls()
    }

    private fun cancelSearch() {
        revision++
        pendingCall?.cancel()
        pendingCall = null
    }

    override fun onDetachedFromWindow() {
        cancelSearch()
        super.onDetachedFromWindow()
    }

    private fun renderControls(): Unit = with(ui) {
        cancelSearch()
        output.removeAllViews()
        choices.removeAllViews()
        val scopes = row()
        choices.section(scopes)
        for ((key, title) in listOf("public" to "Public catalogs", "room" to "Private catalog")) {
            scopes.addView(button(title, key == scope) {
                if (scope != key) {
                    scope = key
                    source = ""
                    query.setText("")
                    renderControls()
                }
            }.apply { isSelected = key == scope }, LinearLayout.LayoutParams(0, -2, 1f).apply { marginEnd = dp(4) })
        }
        val available = (0 until sources.length()).map(sources::getJSONObject).filter { item ->
            val media = item.optJSONArray("media") ?: JSONArray()
            item.optString("scope", "public") == scope && (0 until media.length()).any { media.optString(it) == "music" }
        }
        if (available.isEmpty()) {
            choices.section(label(if (scope == "room") "No private music catalog connected. Your room owner can connect Jellyfin."
                else "No public music catalogs are available.", color = secondary))
            return@with
        }
        val panel = panel()
        val all = if (available.size == 1) available.first().getString("name") else "All ${available.size} sources"
        val recordings = available.any { it.optString("id") == "musicbrainz" }
        val songCatalogs = "Song catalogs · MusicBrainz + Apple Music"
        if (recordings && source.isEmpty()) source = "recordings"
        val selected = if (source == "recordings") songCatalogs else available.find { it.getString("id") == source }?.getString("name") ?: all
        panel.section(label("Search in", 14f, secondary), gap = 4)
        if (available.size == 1) panel.section(label(selected))
        else panel.section(button(selected) {
            val items = (if (recordings) listOf(Entry(songCatalogs) { source = "recordings"; renderControls() }) else emptyList()) +
                listOf(Entry(all) { source = if (recordings) "all-public" else ""; renderControls() }) + available.map { item ->
                Entry(item.getString("name")) { source = item.getString("id"); renderControls() }
            }
            choices(activity, "Choose a catalog", items)
        })
        (query.parent as? LinearLayout)?.removeView(query)
        panel.section(query, gap = 6)
        panel.section(button("Search", true) { search(0) }, gap = 8)
        panel.section(label(if (scope == "room") "Search your room’s connected library."
            else "Search by artist or song. Import metadata first, then choose Find a copy in your library.", 14f, secondary), gap = 0)
        choices.section(panel)
    }

    private fun search(page: Int, text: String = query.text.toString().trim()): Boolean {
        cancelSearch()
        val current = revision
        val body = JSONObject().put("media", "music").put("scope", scope).put("source", source)
            .put("query", text).put("page", page)
        activity.getSystemService(InputMethodManager::class.java).hideSoftInputFromWindow(query.windowToken, 0)
        output.removeAllViews()
        output.addView(ui.label("Searching…", color = ui.secondary).apply { accessibilityLiveRegion = View.ACCESSIBILITY_LIVE_REGION_POLITE })
        request(body, { call ->
            pendingCall = call
            if (current != revision) call.cancel()
        }) { result ->
            if (current == revision) {
                pendingCall = null
                output.removeAllViews()
                result.onSuccess { renderResults(it.getJSONArray("groups"), page, text) }.onFailure {
                    output.addView(ui.label(it.message ?: "Search failed. Try again.", color = ui.secondary))
                }
            }
        }
        return true
    }

    private fun renderResults(groups: JSONArray, page: Int, term: String) = with(ui) {
        var more = false
        val rows = (0 until groups.length()).map(groups::getJSONObject)
        val count = rows.sumOf { it.getJSONArray("items").length() }
        output.section(label("$count results" + if (term.isNotEmpty()) " for “$term”" else "", color = secondary).apply {
            accessibilityLiveRegion = View.ACCESSIBILITY_LIVE_REGION_POLITE
        })
        for (group in rows) {
            val name = group.getString("name")
            output.section(heading(name, 18f), gap = 4)
            val items = group.getJSONArray("items")
            if (items.length() == 0) output.section(label(group.optString("error").ifBlank { "No results on this page." }, 14f, secondary))
            for (i in 0 until items.length()) {
                val item = items.getJSONObject(i)
                val result = panel()
                result.section(heading(item.getString("title"), 17f), gap = 4)
                val subtitle = listOf("creator", "detail", "license").map { item.optString(it) }.filter(String::isNotBlank)
                if (subtitle.isNotEmpty()) result.section(label(subtitle.joinToString(" · "), 14f, secondary), gap = 6)
                val actions = row()
                actions.addView(button("Open source") { open(item.getString("url")) }, LinearLayout.LayoutParams(0, -2, 1f))
                if (item.optBoolean("song")) actions.addView(button("Add to playlist") {
                    add(SongDraft(item.optString("creator"), item.getString("title"), item.optString("album")))
                }, LinearLayout.LayoutParams(0, -2, 1f).apply { marginStart = dp(6) })
                if (canImport && (item.has("musicbrainz_id") || item.has("apple_id"))) {
                    fun showImported(id: Int, ready: Boolean) {
                        result.section(label(if (id > 0) "In library" else "Imported · waiting for library refresh", 14f, secondary))
                        if (id > 0 && !ready) result.section(button("Find a copy") { findCopy(id) })
                    }
                    if (item.optInt("track_id") > 0) showImported(item.getInt("track_id"), item.optBoolean("ready"))
                    else {
                        val save = button("Import song", true) {}
                        save.setOnClickListener {
                            save.isEnabled = false
                            importSong(item) { reply ->
                                reply.onSuccess {
                                    result.removeView(save)
                                    showImported(it.optInt("track_id"), it.optBoolean("ready"))
                                }.onFailure {
                                    save.isEnabled = true
                                    Toast.makeText(activity, it.message ?: "Could not import this song.", Toast.LENGTH_LONG).show()
                                }
                            }
                        }
                        result.section(save)
                    }
                }
                result.addView(actions)
                output.section(result, gap = 6)
            }
            if (group.optBoolean("more")) more = true
        }
        if (page > 0 || more) output.section(row().apply {
            addView(button("Previous") { search(page - 1, term) }.apply { isEnabled = page > 0 }, LinearLayout.LayoutParams(0, -2, 1f))
            addView(button("Next") { search(page + 1, term) }.apply { isEnabled = more }, LinearLayout.LayoutParams(0, -2, 1f))
        })
    }

    private fun open(url: String) {
        val uri = Uri.parse(url)
        if (!uri.userInfo.isNullOrEmpty() || uri.host.isNullOrBlank() || uri.scheme !in setOf("http", "https")) return
        try {
            activity.startActivity(Intent(Intent.ACTION_VIEW, uri))
        } catch (_: ActivityNotFoundException) {
            Toast.makeText(activity, "No app can open this link.", Toast.LENGTH_LONG).show()
        }
    }
}
