package music.listening.room

import android.app.Activity
import android.view.KeyEvent
import android.view.View
import android.view.inputmethod.EditorInfo
import android.view.inputmethod.InputMethodManager
import android.widget.EditText
import android.widget.LinearLayout
import java.text.NumberFormat

internal data class SearchBar(val view: View, val field: EditText)

internal fun RoomUi.searchBar(activity: Activity, query: String, placeholder: String, submit: (String) -> Unit): SearchBar {
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
    val view = row(6).apply {
        addView(field, LinearLayout.LayoutParams(0, -2, 1f))
        addView(button("Search", solid = true, action = ::search))
    }
    return SearchBar(view, field)
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
