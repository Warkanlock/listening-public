package music.listening.room

import android.app.Activity
import android.app.AlertDialog
import android.animation.ValueAnimator
import android.content.res.ColorStateList
import android.view.Gravity
import android.view.View
import android.widget.LinearLayout
import android.widget.ProgressBar
import android.widget.TextView

internal enum class SongDetailsView(val label: String, val mark: Mark) {
    DETAILS("Details", Mark.INFO), CREDITS("Credits", Mark.EXPLORE), ALBUM("Album", Mark.ALBUM)
}

/** Shared presentation for recording information and artist credits. */
internal class SongDetailsStyle(private val ui: RoomUi) {
    fun column() = ui.column().apply {
        setPadding(ui.dp(16), ui.dp(12), ui.dp(16), ui.dp(8))
    }

    fun progress(message: String) = ui.row(8).apply {
        gravity = Gravity.CENTER_VERTICAL
        accessibilityLiveRegion = View.ACCESSIBILITY_LIVE_REGION_POLITE
        if (!ui.eInk && ValueAnimator.areAnimatorsEnabled()) {
            val size = ui.dp(18)
            addView(ProgressBar(context).apply {
                isIndeterminate = true
                indeterminateTintList = ColorStateList.valueOf(ui.accent)
                importantForAccessibility = View.IMPORTANT_FOR_ACCESSIBILITY_NO
            }, LinearLayout.LayoutParams(size, size))
        }
        addView(ui.label(message, color = ui.secondary))
    }

    fun loading(message: String) = ui.column(8).apply {
        val padding = ui.dp(12)
        setPadding(padding, padding, padding, padding)
        background = ui.surface(ui.fog)
        addView(progress(message))
        for (width in if (ui.eInk) emptyList() else listOf(155, 112, 134)) {
            addView(View(context).apply {
                background = ui.surface(ui.rule, null)
                importantForAccessibility = View.IMPORTANT_FOR_ACCESSIBILITY_NO
            }, LinearLayout.LayoutParams(ui.dp(width), ui.dp(8)))
        }
    }

    fun fact(name: String, value: String) = ui.column().apply {
        addView(ui.line())
        addView(ui.column().apply {
            setPadding(0, ui.dp(8), 0, ui.dp(8))
            addView(ui.label(name, 13f, ui.secondary))
            addView(ui.label(value).apply { setTextIsSelectable(true) })
        })
    }

    fun navigation(current: SongDetailsView, hasAlbum: Boolean, open: (SongDetailsView) -> Unit) =
        ui.row(6).apply {
            for (view in SongDetailsView.entries) {
                if (view == SongDetailsView.ALBUM && !hasAlbum) continue
                addView(ui.tab(view.label, view.mark, selected = view == current) {
                    if (view != current) open(view)
                }, LinearLayout.LayoutParams(0, -2, 1f))
            }
        }

    fun dialog(activity: Activity, title: String, body: View, artist: String = "", closeLabel: String = "Close",
               backLabel: String? = null, titleView: TextView = ui.heading(title, 23f),
               canDismiss: () -> Boolean = { true }): AlertDialog {
        val header = ui.row().apply {
            val padding = ui.dp(16)
            setPadding(padding, padding, padding, ui.dp(12))
            addView(ui.column().apply {
                addView(titleView)
                if (artist.isNotBlank()) addView(ui.label(artist, color = ui.secondary))
            }, LinearLayout.LayoutParams(0, -2, 1f).apply { marginEnd = ui.dp(8) })
        }
        val content = ui.column().apply {
            background = ui.surface(ui.paper)
            clipToOutline = true
            addView(header)
            addView(ui.line())
            addView(body, LinearLayout.LayoutParams(-1, -2))
        }
        val dialog = AlertDialog.Builder(activity).setView(content).create()
        val dismissal = ui.iconButton(backLabel ?: closeLabel, if (backLabel == null) Mark.CLOSE else Mark.BACK) {
            if (canDismiss()) dialog.dismiss()
        }
        val size = ui.dp(48)
        if (backLabel == null) header.addView(dismissal, LinearLayout.LayoutParams(size, size))
        else header.addView(dismissal, 0, LinearLayout.LayoutParams(size, size).apply { marginEnd = ui.dp(8) })
        dialog.window?.apply {
            setTitle(title)
            setGravity(Gravity.CENTER)
            setBackgroundDrawableResource(android.R.color.transparent)
            if (ui.eInk) setWindowAnimations(0)
        }
        return dialog
    }
}
