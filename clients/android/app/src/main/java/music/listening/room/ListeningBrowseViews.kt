package music.listening.room

import android.app.Activity
import android.graphics.drawable.StateListDrawable
import android.text.Editable
import android.text.SpannableString
import android.text.Spanned
import android.text.TextWatcher
import android.text.style.ForegroundColorSpan
import android.text.style.RelativeSizeSpan
import android.text.style.TypefaceSpan
import android.view.Gravity
import android.view.KeyEvent
import android.view.View
import android.view.inputmethod.EditorInfo
import android.view.inputmethod.InputMethodManager
import android.widget.EditText
import android.widget.LinearLayout
import java.text.NumberFormat

internal data class SearchBar(val view: View, val field: EditText)

internal fun RoomUi.searchBar(activity: Activity, query: String, placeholder: String,
                              integrated: Boolean = false, submit: (String) -> Unit): SearchBar {
    val field = field(placeholder).apply {
        setText(query)
        imeOptions = EditorInfo.IME_ACTION_SEARCH
    }
    fun search() {
        activity.getSystemService(InputMethodManager::class.java)?.hideSoftInputFromWindow(field.windowToken, 0)
        if (eInk) field.clearFocus()
        submit(field.text.toString().trim())
    }
    field.setOnEditorActionListener { _, action, event ->
        val key = event?.keyCode
        when {
            key == KeyEvent.KEYCODE_ENTER || key == KeyEvent.KEYCODE_NUMPAD_ENTER -> {
                if (event?.action == KeyEvent.ACTION_DOWN && event.repeatCount == 0) search()
                true
            }
            action == EditorInfo.IME_ACTION_SEARCH -> {
                search()
                true
            }
            else -> false
        }
    }
    val view = if (integrated) roomSearchField(field, ::search) else row(6).apply {
        addView(field, LinearLayout.LayoutParams(0, -2, 1f))
        addView(button("Search", solid = true, action = ::search))
    }
    return SearchBar(view, field)
}

private fun RoomUi.roomSearchField(field: EditText, search: () -> Unit) = row().apply {
    fun outline(focused: Boolean) = surface(paper, if (focused) ink else rule).apply {
        if (focused) setStroke(dp(2), ink)
    }
    background = outline(false)
    setPadding(dp(2), dp(2), dp(2), dp(2))
    field.background = null
    field.setPaddingRelative(dp(12), 0, dp(4), 0)
    field.setOnFocusChangeListener { _, focused -> background = outline(focused) }
    addView(field, LinearLayout.LayoutParams(0, -2, 1f))
    val clear = iconButton("Clear search", Mark.CLOSE, outlined = false) {
        field.text.clear()
        search()
    }
    fun showClear() {
        val visibility = if (field.text.isEmpty()) View.GONE else View.VISIBLE
        if (clear.visibility != visibility) clear.visibility = visibility
    }
    showClear()
    field.addTextChangedListener(object : TextWatcher {
        override fun beforeTextChanged(text: CharSequence?, start: Int, count: Int, after: Int) = Unit
        override fun onTextChanged(text: CharSequence?, start: Int, before: Int, count: Int) = Unit
        override fun afterTextChanged(text: Editable?) = showClear()
    })
    addView(clear, LinearLayout.LayoutParams(dp(48), dp(48)))
    addView(iconButton("Search", Mark.SEARCH, outlined = false, action = search),
        LinearLayout.LayoutParams(dp(48), dp(48)))
}

internal class SearchFeedback(private val ui: RoomUi, openCatalogs: (() -> Unit)?) {
    private var retryAction: (() -> Unit)? = null
    private val titleView = ui.heading("", 21f)
    private val detailView = ui.label("", 15f, ui.secondary)
    private val retryButton = ui.button("Try again") { retryAction?.invoke() }
    private val message = ui.column(4).apply {
        accessibilityLiveRegion = View.ACCESSIBILITY_LIVE_REGION_POLITE
        addView(titleView)
        addView(detailView)
        addView(retryButton, LinearLayout.LayoutParams(-2, -2))
    }
    private val resultCount = ui.heading("", 17f).apply {
        minHeight = ui.dp(36)
        gravity = Gravity.CENTER_VERTICAL
        accessibilityLiveRegion = View.ACCESSIBILITY_LIVE_REGION_POLITE
    }
    private val catalogButton = openCatalogs?.let { ui.catalogAction(it) }
    private val summary = ui.row(8).apply {
        addView(resultCount, LinearLayout.LayoutParams(0, -2, 1f))
        catalogButton?.let { addView(it, LinearLayout.LayoutParams(-2, -2)) }
    }
    private val leadingRule = ui.line()
    private val trailingRule = ui.line()
    val view = ui.column(4).apply {
        addView(message)
        addView(leadingRule)
        addView(summary)
        addView(trailingRule)
    }

    fun showMessage(title: String? = null, detail: String? = null, retry: (() -> Unit)? = null) {
        view.setPadding(0, ui.dp(10), 0, ui.dp(6))
        retryAction = retry
        titleView.visibility = if (title != null) View.VISIBLE else View.GONE
        detailView.visibility = if (detail != null) View.VISIBLE else View.GONE
        retryButton.visibility = if (retry != null) View.VISIBLE else View.GONE
        message.visibility = if (title != null || detail != null || retry != null) View.VISIBLE else View.GONE
        titleView.text = title.orEmpty()
        detailView.text = detail.orEmpty()
        resultCount.visibility = View.GONE
        summary.visibility = if (catalogButton != null) View.VISIBLE else View.GONE
        leadingRule.visibility = summary.visibility
        trailingRule.visibility = summary.visibility
        leadingRule.layoutParams = LinearLayout.LayoutParams(-1, ui.dp(1)).apply {
            if (message.visibility == View.VISIBLE) topMargin = ui.dp(8)
        }
        showCatalog(expanded = true)
    }

    fun showResults(count: String) {
        view.setPadding(0, ui.dp(6), 0, ui.dp(4))
        retryAction = null
        message.visibility = View.GONE
        summary.visibility = View.VISIBLE
        resultCount.visibility = View.VISIBLE
        resultCount.text = count
        leadingRule.visibility = View.GONE
        trailingRule.visibility = View.VISIBLE
        showCatalog(expanded = false)
    }

    private fun showCatalog(expanded: Boolean) {
        val button = catalogButton ?: return
        val title = "Browse catalogs"
        button.textSize = if (expanded) 17f else 15f
        button.text = if (expanded) SpannableString("$title\nSearch sources outside this room").apply {
            val start = title.length + 1
            setSpan(RelativeSizeSpan(14f / 17f), start, length, Spanned.SPAN_EXCLUSIVE_EXCLUSIVE)
            setSpan(ForegroundColorSpan(ui.secondary), start, length, Spanned.SPAN_EXCLUSIVE_EXCLUSIVE)
            setSpan(TypefaceSpan("sans-serif"), start, length, Spanned.SPAN_EXCLUSIVE_EXCLUSIVE)
        } else title
        button.layoutParams = LinearLayout.LayoutParams(if (expanded) -1 else -2, -2)
    }

    private fun RoomUi.catalogAction(open: () -> Unit) =
        button("Browse catalogs", action = open).apply {
            background = StateListDrawable().apply {
                addState(intArrayOf(android.R.attr.state_focused), surface(fog, ink))
                addState(intArrayOf(android.R.attr.state_pressed), surface(fog, null))
                addState(intArrayOf(), surface(paper, null))
            }
            setPaddingRelative(0, dp(8), dp(4), dp(8))
            gravity = Gravity.START or Gravity.CENTER_VERTICAL
            compoundDrawablePadding = dp(8)
            setCompoundDrawablesRelative(null, null, mark(Mark.BROWSE, 16), null)
        }
}

internal fun RoomUi.playlistHeader(playlist: RoomPlaylist, play: () -> Unit, add: () -> Unit): View = column(8).apply {
    val number = NumberFormat.getIntegerInstance()
    val total = playlist.row.optInt("entries", playlist.playable)
    val trackCount = "${number.format(total)} ${if (total == 1) "song" else "songs"}"
    val counts = if (playlist.playable == total && total > 0) "$trackCount · All playable"
        else "$trackCount · ${number.format(playlist.playable)} playable"
    addView(label(counts, 14f, secondary))
    if (playlist.linked) addView(label(playlist.syncLabel, 14f, secondary))
    addView(row(6).apply {
        addView(button("Play playlist", solid = true, action = play).apply { isEnabled = playlist.playable > 0 },
            LinearLayout.LayoutParams(0, -2, 1f))
        if (playlist.canAdd) {
            addView(button("Add song", action = add), LinearLayout.LayoutParams(0, -2, 1f))
        }
    })
    if (!playlist.canAdd) addView(label(when {
        playlist.needsWritePermission -> "Reconnect Spotify in room settings to edit this playlist."
        playlist.spotify -> "Check the Spotify link and device permissions in room settings."
        playlist.linked -> "Edit this playlist at its source."
        else -> "The room owner can allow this device to add songs."
    }, 14f, secondary))
}
