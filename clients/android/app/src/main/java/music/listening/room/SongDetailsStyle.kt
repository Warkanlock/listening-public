package music.listening.room

import android.app.Activity
import android.app.AlertDialog
import android.animation.ValueAnimator
import android.content.res.ColorStateList
import android.graphics.drawable.StateListDrawable
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

    fun fact(name: String, value: String) = ui.column().apply {
        addView(ui.line())
        addView(ui.column().apply {
            setPadding(0, ui.dp(8), 0, ui.dp(8))
            addView(ui.label(name, 13f, ui.secondary))
            addView(ui.label(value).apply { setTextIsSelectable(true) })
        })
    }

    fun link(title: String, detail: String = "", open: () -> Unit) = with(ui) {
        column().apply {
            addView(row(8).apply {
                minimumHeight = dp(48)
                setPadding(0, dp(6), dp(6), dp(6))
                background = StateListDrawable().apply {
                    addState(intArrayOf(android.R.attr.state_pressed), surface(fog, null))
                    addState(intArrayOf(android.R.attr.state_focused), surface(fog, ink))
                    addState(intArrayOf(), surface(paper, null))
                }
                addView(column(2).apply {
                    addView(label(title))
                    if (detail.isNotBlank()) addView(label(detail, 14f, secondary))
                }, LinearLayout.LayoutParams(0, -2, 1f))
                addView(android.widget.ImageView(context).apply {
                    setImageDrawable(mark(Mark.BROWSE, 17))
                    importantForAccessibility = View.IMPORTANT_FOR_ACCESSIBILITY_NO
                })
                isFocusable = true
                setOnClickListener { open() }
            })
            addView(line())
        }
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
               compact: Boolean = false, canDismiss: () -> Boolean = { true }): AlertDialog {
        val header = ui.row().apply {
            val padding = ui.dp(if (compact) 12 else 16)
            setPadding(padding, padding, padding, ui.dp(if (compact) 9 else 12))
            addView(ui.column().apply {
                addView(titleView)
                if (artist.isNotBlank()) addView(ui.label(artist, if (compact) 12f else 16f, ui.secondary))
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
        val dismissalMark = if (backLabel == null) Mark.CLOSE else Mark.BACK
        val dismissal = ui.iconButton(backLabel ?: closeLabel, dismissalMark, compact = compact) {
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
