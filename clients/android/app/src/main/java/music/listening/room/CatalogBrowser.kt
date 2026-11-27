package music.listening.room

import android.app.Activity
import android.content.ActivityNotFoundException
import android.content.Intent
import android.net.Uri
import android.text.InputFilter
import android.view.Gravity
import android.view.View
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.Toast
import androidx.core.view.OneShotPreDrawListener
import org.json.JSONArray
import org.json.JSONObject
import okhttp3.Call

internal data class CatalogBrowserState(
    val scope: String,
    val source: String,
    val availableSources: Set<String>,
    val draft: String,
    val search: Search?,
    val imported: Map<String, Import>,
    val scrollY: Int,
) {
    sealed interface Search {
        data class Loading(val page: Int, val query: String) : Search
        data class Results(val page: Int, val query: String, val groups: JSONArray) : Search
        data class Error(val message: String) : Search
    }

    data class Import(val trackId: Int, val ready: Boolean)
}

/** Room credentials stay in the room HTTP client. External results open as ordinary web links. */
internal class CatalogBrowser(private val activity: Activity, private val ui: RoomUi,
                             configuration: JSONObject,
                             private val request: (JSONObject, (Call) -> Unit, (Result<JSONObject>) -> Unit) -> Unit,
                             private val add: (SongDraft) -> Unit,
                             private val importSong: (JSONObject, (Result<JSONObject>) -> Unit) -> Unit,
                             private val findCopy: (Int) -> Unit,
                             restored: CatalogBrowserState? = null) : ScrollView(activity) {
    private val canImport = configuration.optBoolean("can_import")
    private val sources = configuration.getJSONArray("sources")
    private val body = ui.column()
    private val choices = ui.column()
    private val output = ui.column()
    private val searchBar = ui.searchBar(activity, restored?.draft.orEmpty(), "Song or artist") { search(0, it) }.apply {
        field.contentDescription = "Search catalogs"
        field.filters = arrayOf(InputFilter.LengthFilter(160))
    }
    private var scope = restored?.scope ?: "public"
    private var source = restored?.source.orEmpty()
    private var pending: MetadataRequest? = null
    private var displayed: CatalogBrowserState.Search? = null
    private val imported = mutableMapOf<String, CatalogBrowserState.Import>()

    init {
        if (ui.eInk) overScrollMode = View.OVER_SCROLL_NEVER
        addView(body)
        body.addView(choices)
        body.addView(output)
        val availableIds = availableSources().mapTo(mutableSetOf()) { it.getString("id") }
        val unchanged = restored != null && restored.availableSources == availableIds
        if (renderControls() && restored != null && unchanged && restored.source == source) {
            imported.putAll(restored.imported)
            when (val previous = restored.search) {
                is CatalogBrowserState.Search.Loading -> search(previous.page, previous.query, scrollToResults = false)
                else -> showSearch(previous)
            }
            if (restored.scrollY > 0) OneShotPreDrawListener.add(this) { scrollTo(0, restored.scrollY) }
        }
    }

    fun snapshot() = CatalogBrowserState(scope, source,
        availableSources().mapTo(mutableSetOf()) { it.getString("id") },
        searchBar.field.text.toString(), displayed, imported.toMap(), scrollY)

    private fun availableSources() = (0 until sources.length()).map(sources::getJSONObject).filter { item ->
        val media = item.optJSONArray("media") ?: JSONArray()
        item.optString("scope", "public") == scope && (0 until media.length()).any { media.optString(it) == "music" }
    }

    private fun cancelSearch() {
        pending?.cancel()
        pending = null
    }

    override fun onDetachedFromWindow() {
        cancelSearch()
        super.onDetachedFromWindow()
    }

    private fun renderControls(): Boolean = with(ui) {
        cancelSearch()
        showSearch(null)
        imported.clear()
        choices.removeAllViews()
        val scopes = row(6)
        choices.section(scopes)
        for ((key, title) in listOf("public" to "Public catalogs", "room" to "Private catalog")) {
            scopes.addView(button(title, key == scope) {
                if (scope != key) {
                    scope = key
                    source = ""
                    searchBar.field.setText("")
                    renderControls()
                }
            }.apply { isSelected = key == scope }, LinearLayout.LayoutParams(0, -2, 1f))
        }
        val available = availableSources()
        val recordings = available.any { it.optString("id") == "musicbrainz" }
        val chosen = available.any { it.optString("id") == source } ||
            (recordings && source in setOf("recordings", "all-public"))
        if (!chosen) source = if (recordings) "recordings" else ""
        if (available.isEmpty()) {
            choices.section(label(if (scope == "room") "No private music catalog connected. Your room owner can connect Jellyfin."
                else "No public music catalogs are available.", color = secondary))
            return@with false
        }
        val all = if (available.size == 1) available.first().getString("name") else "All ${available.size} sources"
        val songCatalogs = "MusicBrainz + Apple Music"
        val selected = if (source == "recordings") songCatalogs else available.find { it.getString("id") == source }?.getString("name") ?: all
        choices.section(label("Search in", 14f, secondary), gap = 4)
        if (available.size == 1) choices.section(label(selected))
        else choices.section(button(selected) {
            val items = (if (recordings) listOf(Entry(songCatalogs) { source = "recordings"; renderControls() }) else emptyList()) +
                listOf(Entry(all) { source = if (recordings) "all-public" else ""; renderControls() }) + available.map { item ->
                Entry(item.getString("name")) { source = item.getString("id"); renderControls() }
            }
            choices(activity, "Choose a catalog", items)
        }.apply {
            gravity = Gravity.START or Gravity.CENTER_VERTICAL
            setCompoundDrawablesRelative(null, null, mark(Mark.BROWSE, 17, accentInk), null)
        })
        (searchBar.view.parent as? LinearLayout)?.removeView(searchBar.view)
        choices.section(searchBar.view)
        true
    }

    private fun search(page: Int, text: String, scrollToResults: Boolean = true) {
        cancelSearch()
        val current = MetadataRequest()
        pending = current
        imported.clear()
        val body = JSONObject().put("media", "music").put("scope", scope).put("source", source)
            .put("query", text).put("page", page)
        showSearch(CatalogBrowserState.Search.Loading(page, text))
        request(body, current::bind) { result ->
            if (current === pending) {
                pending = null
                val search = result.fold(
                    { CatalogBrowserState.Search.Results(page, text, it.getJSONArray("groups")) },
                    { CatalogBrowserState.Search.Error(it.message ?: "Search failed. Try again.") }
                )
                showSearch(search)
                if (scrollToResults) OneShotPreDrawListener.add(this) {
                    if (displayed === search) scrollTo(0, output.top)
                }
            }
        }
    }

    private fun showSearch(search: CatalogBrowserState.Search?) {
        displayed = search
        output.removeAllViews()
        when (search) {
            is CatalogBrowserState.Search.Loading -> output.addView(ui.label("Searching…", color = ui.secondary).apply {
                accessibilityLiveRegion = View.ACCESSIBILITY_LIVE_REGION_POLITE
            })
            is CatalogBrowserState.Search.Results -> renderResults(search.groups, search.page, search.query)
            is CatalogBrowserState.Search.Error -> output.addView(ui.label(search.message, color = ui.secondary).apply {
                accessibilityLiveRegion = View.ACCESSIBILITY_LIVE_REGION_POLITE
            })
            null -> Unit
        }
    }

    private fun renderResults(groups: JSONArray, page: Int, term: String) = with(ui) {
        var more = false
        val rows = (0 until groups.length()).map(groups::getJSONObject)
        val count = rows.sumOf { it.getJSONArray("items").length() }
        val status = if (count == 1) "1 result" else "$count results"
        val caption = if (term.isBlank()) status else "$status for “$term”"
        output.section(label(caption, color = secondary).apply {
            accessibilityLiveRegion = View.ACCESSIBILITY_LIVE_REGION_POLITE
        })
        for (group in rows) {
            val name = group.getString("name")
            output.section(heading(name, 18f), gap = 4)
            val items = group.getJSONArray("items")
            if (items.length() == 0) output.section(label(group.optString("error").ifBlank { "No results on this page." }, 14f, secondary))
            for (index in 0 until items.length()) output.addView(resultRow(items.getJSONObject(index)))
            if (group.optBoolean("more")) more = true
        }
        if (page > 0 || more) output.section(row().apply {
            addView(button("Previous") { search(page - 1, term) }.apply { isEnabled = page > 0 }, LinearLayout.LayoutParams(0, -2, 1f))
            addView(button("Next") { search(page + 1, term) }.apply { isEnabled = more }, LinearLayout.LayoutParams(0, -2, 1f))
        })
    }

    private fun resultRow(item: JSONObject): View = with(ui) {
        val title = item.getString("title")
        val url = item.getString("url")
        val subtitle = listOf("creator", "detail", "license").map(item::optString).filter(String::isNotBlank)
        val importable = canImport && (item.has("musicbrainz_id") || item.has("apple_id"))
        val knownCopy = item.optInt("track_id").takeIf { importable && it > 0 }?.let {
            CatalogBrowserState.Import(it, item.optBoolean("ready"))
        }
        fun copyStatus(copy: CatalogBrowserState.Import?) = when {
            copy == null -> ""
            copy.trackId > 0 -> "In library"
            else -> "Waiting for library refresh"
        }
        val lookup = imported[url] ?: knownCopy
        val status = label(copyStatus(lookup), 14f, secondary).apply {
            visibility = if (lookup == null) View.GONE else View.VISIBLE
            accessibilityLiveRegion = View.ACCESSIBILITY_LIVE_REGION_POLITE
        }
        var importing = false
        val more = iconButton("Actions for $title", Mark.MORE, outlined = false) {
            val options = buildList {
                add(Entry("Open source") { open(url) })
                if (item.optBoolean("song")) add(Entry("Add to playlist") {
                    this@CatalogBrowser.add(SongDraft(item.optString("creator"), title, item.optString("album")))
                })
                val copy = imported[url] ?: knownCopy
                if (copy != null) {
                    if (copy.trackId > 0 && !copy.ready) add(Entry("Find a copy") { findCopy(copy.trackId) })
                } else if (importable && !importing) add(Entry("Import song") {
                    importing = true
                    status.visibility = View.VISIBLE
                    status.text = "Importing…"
                    importSong(item) { reply ->
                        importing = false
                        reply.onSuccess { result ->
                            val saved = CatalogBrowserState.Import(result.optInt("track_id"), result.optBoolean("ready"))
                            imported[url] = saved
                            status.text = copyStatus(saved)
                        }.onFailure {
                            status.visibility = View.GONE
                            Toast.makeText(activity, it.message ?: "Could not import this song.", Toast.LENGTH_LONG).show()
                        }
                    }
                })
            }
            choices(activity, title, options)
        }
        column().apply {
            addView(row(8).apply {
                addView(column(2).apply {
                    setPadding(0, dp(7), 0, dp(7))
                    addView(label(title))
                    if (subtitle.isNotEmpty()) addView(label(subtitle.joinToString(" · "), 14f, secondary))
                    addView(status)
                }, LinearLayout.LayoutParams(0, -2, 1f))
                addView(more, LinearLayout.LayoutParams(dp(48), dp(48)))
            })
            addView(line())
        }
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
