package music.listening.room

import android.content.Context
import android.content.res.ColorStateList
import android.content.res.Configuration
import android.graphics.Color
import android.graphics.Paint
import android.graphics.Typeface
import android.graphics.drawable.GradientDrawable
import android.graphics.drawable.InsetDrawable
import android.graphics.drawable.StateListDrawable
import android.os.Build
import android.view.Gravity
import android.view.View
import android.view.ViewGroup
import android.view.ViewConfiguration
import android.widget.*

internal data class Entry(val title: String, val subtitle: String = "", val detail: String = "",
                          val info: (() -> Unit)? = null, val mark: Mark? = null,
                          val openArtist: (() -> Unit)? = null, val album: String = "",
                          val openAlbum: (() -> Unit)? = null, val destination: Boolean = false,
                          val leadingMark: Mark? = null,
                          val open: (() -> Unit)? = null)

/** Native controls for room and offline players. */
internal class RoomUi(private val context: Context) {
    val theme = RoomTheme.load(context)
    val eInk = theme == RoomTheme.E_INK
    private val palette by lazy { RoomPalettes.load(context, theme) }
    val compactPlayerLandscape get() = context.resources.configuration.let {
        it.orientation == Configuration.ORIENTATION_LANDSCAPE && (eInk || it.screenHeightDp < 550)
    }
    private val compact get() = eInk || context.resources.configuration.screenWidthDp < 380
    private fun tone(kompakt: Int, token: String) = if (eInk) Color.rgb(kompakt, kompakt, kompakt)
        else RoomPalettes.color(palette, token)
    val ink = tone(0, "text")
    val paper = tone(255, "panel")
    val card = tone(255, "card")
    val fog = tone(245, "tint")
    val secondary = tone(51, "muted")
    val accent = tone(0, "accent")
    val accentInk = tone(0, "accent-ink")
    val onAccent = tone(255, "on-accent")
    val rule = tone(117, "line-strong")
    private val controlEdge = tone(0, "control-edge")
    private val pressed = tone(200, "accent-tint")
    private val medium = Typeface.create("sans-serif-medium", Typeface.NORMAL)
    val headline: Typeface = Typeface.create("sans-serif", Typeface.BOLD)
    fun dp(value: Int) = (value * context.resources.displayMetrics.density + 0.5f).toInt()
    private fun LinearLayout.spaceChildren(gap: Int) {
        if (gap == 0) return
        dividerDrawable = GradientDrawable().apply { setColor(Color.TRANSPARENT); setSize(dp(gap), dp(gap)) }
        showDividers = LinearLayout.SHOW_DIVIDER_MIDDLE
    }
    fun column(gap: Int = 0) = LinearLayout(context).apply {
        orientation = LinearLayout.VERTICAL
        spaceChildren(gap)
    }
    fun row(gap: Int = 0) = LinearLayout(context).apply {
        gravity = Gravity.CENTER_VERTICAL
        spaceChildren(gap)
    }
    fun screen() = column(if (compact) 6 else 8).apply {
        setBackgroundColor(paper)
        val side = dp(if (compact) 12 else 16)
        val vertical = dp(if (compact) 6 else 8)
        setPadding(side, vertical, side, vertical)
        if (Build.VERSION.SDK_INT >= 35) setOnApplyWindowInsetsListener { view, insets ->
            val bars = insets.getInsets(android.view.WindowInsets.Type.systemBars() or
                android.view.WindowInsets.Type.displayCutout() or android.view.WindowInsets.Type.ime())
            view.setPadding(side + bars.left, vertical + bars.top, side + bars.right, vertical + bars.bottom)
            insets
        }
    }

    fun header(title: TextView, back: View? = null, trailing: View? = null) = row(6).apply {
        minimumHeight = dp(48)
        back?.let { addView(it, LinearLayout.LayoutParams(dp(48), dp(48))) }
        addView(title, LinearLayout.LayoutParams(0, -2, 1f))
        trailing?.let { addView(it, LinearLayout.LayoutParams(dp(48), dp(48))) }
    }

    fun scroll(body: View) = ScrollView(context).apply {
        if (eInk) {
            overScrollMode = View.OVER_SCROLL_NEVER
            isSmoothScrollingEnabled = false
        }
        addView(body)
    }

    fun notice(title: String, detail: String, action: Entry? = null) = panel().apply {
        addView(heading(title, 18f))
        addView(label(detail, 15f, secondary), LinearLayout.LayoutParams(-1, -2).apply { topMargin = dp(4) })
        action?.let { next ->
            addView(button(next.title, solid = true) { next.open?.invoke() }, LinearLayout.LayoutParams(-1, -2).apply {
                topMargin = dp(8)
            })
        }
    }
    fun label(value: String, size: Float = 16f, color: Int = ink) = TextView(context).apply {
        text = value
        textSize = size
        setTextColor(color)
    }

    fun heading(value: String, size: Float = 24f) = label(value, size).apply {
        typeface = headline
        if (Build.VERSION.SDK_INT >= 28) isAccessibilityHeading = true
    }

    fun statusChip(value: String) = label(value, 13f, accentInk).apply {
        background = surface(fog)
        setPadding(dp(8), dp(4), dp(8), dp(4))
    }

    fun surface(fill: Int, stroke: Int? = rule) = GradientDrawable().apply {
        setColor(fill)
        cornerRadius = if (eInk) 0f else dp(8).toFloat()
        if (stroke != null) setStroke(dp(1), stroke)
    }

    fun panel(fill: Int = card) = column().apply {
        background = surface(fill)
        val inset = dp(if (compact) 10 else 14)
        setPadding(inset, inset, inset, inset)
    }

    fun LinearLayout.section(view: View, gap: Int = if (compact) 8 else 12) {
        addView(view, LinearLayout.LayoutParams(-1, -2).apply { bottomMargin = dp(gap) })
    }

    fun button(name: String, solid: Boolean = false, action: () -> Unit) = Button(context).apply {
        text = name
        isAllCaps = false
        typeface = medium
        textSize = 15f
        minWidth = 0; minimumWidth = 0; minHeight = dp(48); minimumHeight = dp(48)
        setPadding(dp(10), dp(4), dp(10), dp(4))
        stateListAnimator = null
        val disabled = intArrayOf(-android.R.attr.state_enabled)
        val focus = intArrayOf(android.R.attr.state_focused)
        val down = intArrayOf(android.R.attr.state_pressed)
        val selected = intArrayOf(android.R.attr.state_selected)
        setTextColor(ColorStateList(arrayOf(disabled, focus, down, selected, intArrayOf()),
            intArrayOf(secondary, accentInk, accentInk, onAccent, if (solid) onAccent else accentInk)))
        background = StateListDrawable().apply {
            addState(disabled, surface(fog))
            addState(down, surface(pressed, accent))
            addState(focus, surface(pressed, accent))
            addState(selected, surface(accent, accent))
            addState(intArrayOf(), surface(if (solid) accent else paper, if (solid) accent else controlEdge))
        }
        setOnClickListener { action() }
    }

    fun mark(mark: Mark, size: Int, color: Int = ink) = RoomMark(mark, color, dp(size)).apply {
        setBounds(0, 0, intrinsicWidth, intrinsicHeight)
    }

    fun iconButton(name: String, glyph: Mark, solid: Boolean = false, outlined: Boolean = true,
                   compact: Boolean = false, action: () -> Unit) =
        ImageButton(context).apply {
            contentDescription = name
            tooltipText = name
            val size = if (solid) 25 else 23
            setImageDrawable(mark(glyph, if (compact) size * 4 / 5 else size, if (solid) onAccent else accentInk))
            scaleType = ImageView.ScaleType.CENTER
            val disabled = intArrayOf(-android.R.attr.state_enabled)
            val selected = intArrayOf(android.R.attr.state_selected)
            val active = if (solid) onAccent else accentInk
            val down = intArrayOf(android.R.attr.state_pressed)
            val focus = intArrayOf(android.R.attr.state_focused)
            imageTintList = ColorStateList(arrayOf(disabled, down, focus, selected, intArrayOf()),
                intArrayOf(secondary, active, active, onAccent, active))
            val rest = if (solid) accent else paper
            fun disc(color: Int, outline: Boolean = false) = GradientDrawable().apply {
                shape = if (eInk) GradientDrawable.RECTANGLE else GradientDrawable.OVAL
                setColor(color)
                if (outline || (outlined && glyph != Mark.INFO)) {
                    setStroke(dp(if (outline) 2 else 1), if (outline) ink else controlEdge)
                }
            }
            val buttonBackground = StateListDrawable().apply {
                addState(disabled, disc(fog))
                addState(down, disc(if (solid) secondary else pressed, true))
                addState(focus, disc(if (solid) secondary else fog, true))
                addState(selected, disc(accent, true))
                addState(intArrayOf(), disc(rest))
            }
            background = if (compact) InsetDrawable(buttonBackground, dp(5)) else buttonBackground
            minimumWidth = dp(48); minimumHeight = dp(48)
            setOnClickListener { action() }
        }

    fun actionStrip(actions: List<ImageButton>, alignEnd: Boolean = false) = HorizontalScrollView(context).apply {
        isFillViewport = true
        if (eInk) overScrollMode = View.OVER_SCROLL_NEVER
        addView(row(6).apply {
            gravity = (if (alignEnd) Gravity.END else Gravity.START) or Gravity.CENTER_VERTICAL
            actions.forEach { addView(it, LinearLayout.LayoutParams(dp(48), dp(48))) }
        }, FrameLayout.LayoutParams(-2, -2))
    }

    fun transport(previous: ImageButton, play: ImageButton, next: ImageButton) = row(16).apply {
        gravity = Gravity.CENTER
        addView(previous, LinearLayout.LayoutParams(dp(48), dp(48)))
        val playSize = dp(if (compact) 56 else 64)
        addView(play, LinearLayout.LayoutParams(playSize, playSize))
        addView(next, LinearLayout.LayoutParams(dp(48), dp(48)))
    }

    fun playerContent(sleeve: View, controls: View): LinearLayout =
        if (compactPlayerLandscape) {
            row(12).apply {
                addView(sleeve, LinearLayout.LayoutParams(0, -2, 1f))
                addView(controls, LinearLayout.LayoutParams(0, -2, 1f))
            }
        } else {
            column(if (compact) 6 else 8).apply {
                addView(sleeve, LinearLayout.LayoutParams(-1, -2))
                addView(controls, LinearLayout.LayoutParams(-1, -2))
            }
        }

    fun playbackIcon(button: ImageButton, name: String, showPause: Boolean) {
        if (button.contentDescription == name) return
        button.contentDescription = name
        button.tooltipText = name
        button.setImageDrawable(mark(if (showPause) Mark.PAUSE else Mark.PLAY, 25, onAccent))
    }

    fun queueButton(action: () -> Unit) = button("Queue", action = action).apply {
        setCompoundDrawablesRelative(mark(Mark.QUEUE, 19), null, null, null)
        compoundDrawablePadding = dp(8)
        gravity = Gravity.START or Gravity.CENTER_VERTICAL
        setPadding(dp(14), dp(4), dp(12), dp(4))
    }

    data class NavigationTab(val title: String, val selected: Boolean, val open: () -> Unit)

    fun navigation(tabs: List<NavigationTab>) = column().apply {
        addView(line())
        addView(row().apply {
            tabs.forEach { tab ->
                addView(button(tab.title, action = tab.open).apply {
                    gravity = Gravity.CENTER
                    background = StateListDrawable().apply {
                        val fill = if (eInk && tab.selected) ink else paper
                        addState(intArrayOf(android.R.attr.state_focused), surface(
                            if (eInk && tab.selected) ink else fog, if (eInk && tab.selected) paper else ink))
                        addState(intArrayOf(android.R.attr.state_pressed), surface(if (eInk && tab.selected) secondary else fog, null))
                        addState(intArrayOf(), surface(fill, null))
                    }
                    setTextColor(if (eInk && tab.selected) paper else ink)
                    setPadding(0, 0, 0, 0)
                    textSize = 14f
                    typeface = if (tab.selected) Typeface.DEFAULT_BOLD else Typeface.DEFAULT
                    if (tab.selected && !eInk) paintFlags = paintFlags or Paint.UNDERLINE_TEXT_FLAG
                    isSelected = tab.selected
                }, LinearLayout.LayoutParams(0, dp(48), 1f))
            }
        })
    }

    fun tab(name: String, glyph: Mark, selected: Boolean, action: () -> Unit) =
        button(name, action = action).apply {
            if (eInk) {
                if (selected) paintFlags = paintFlags or Paint.UNDERLINE_TEXT_FLAG
            } else {
                setCompoundDrawables(null, mark(glyph, 18), null, null)
                compoundDrawableTintList = textColors
                compoundDrawablePadding = dp(2)
            }
            setPadding(0, dp(4), 0, dp(4))
            textSize = 13f
            typeface = if (selected) Typeface.DEFAULT_BOLD else Typeface.DEFAULT
            isSelected = selected
        }

    fun field(hintText: String) = EditText(context).apply {
        hint = hintText; setSingleLine(); textSize = 16f
        setTextColor(ink); setHintTextColor(secondary)
        background = StateListDrawable().apply {
            addState(intArrayOf(android.R.attr.state_focused), surface(card).apply { setStroke(dp(2), accent) })
            addState(intArrayOf(), surface(card, controlEdge))
        }
        setPadding(dp(13), 0, dp(10), 0)
        minHeight = dp(48)
    }

    fun profileLink(label: TextView, open: (() -> Unit)?) {
        label.setOnClickListener(open?.let { View.OnClickListener { open() } })
        label.isClickable = open != null
        label.isFocusable = open != null
        val underline = android.graphics.Paint.UNDERLINE_TEXT_FLAG
        label.paintFlags = if (open == null) label.paintFlags and underline.inv()
            else label.paintFlags or underline
    }

    fun choices(activity: android.app.Activity, title: String, options: List<Entry>) {
        val style = SongDetailsStyle(this)
        val body = style.column()
        val dialog = style.dialog(activity, title, scroll(body))
        for (option in options) body.section(button(option.title) {
            dialog.dismiss()
            option.open?.invoke()
        }.apply {
            option.mark?.let { setCompoundDrawablesRelative(mark(it, 19, accent), null, null, null) }
            if (option.subtitle.isNotEmpty()) text = option.title + "\n" + option.subtitle
        }, gap = 6)
        dialog.show()
    }

    fun showQueue(activity: android.app.Activity, tracks: List<Entry>) {
        val style = SongDetailsStyle(this)
        lateinit var dialog: android.app.AlertDialog
        val body = if (tracks.isEmpty()) style.column().apply {
            addView(label("No songs up next. Play a song or add one from its options.", 15f, secondary))
        }
        else index(tracks.map { track ->
            track.copy(open = { dialog.dismiss(); track.open?.invoke() })
        }).apply { setPadding(dp(12), 0, dp(12), 0) }
        dialog = style.dialog(activity, "Queue", body, closeLabel = "Close queue")
        dialog.show()
    }

    fun line() = View(context).apply { setBackgroundColor(rule); layoutParams = LinearLayout.LayoutParams(-1, dp(1)) }

    fun index(items: List<Entry>, header: View? = null) = ListView(context).apply {
        if (eInk) {
            overScrollMode = View.OVER_SCROLL_NEVER
            setFriction(ViewConfiguration.getScrollFriction() * 3)
        }
        divider = null
        itemsCanFocus = true
        if (header != null) addHeaderView(header, null, false)
        adapter = object : BaseAdapter() {
            override fun getCount() = items.size
            override fun getItem(position: Int) = items[position]
            override fun getItemId(position: Int) = position.toLong()
            override fun areAllItemsEnabled() = false
            override fun isEnabled(position: Int) = items[position].open != null
            override fun getViewTypeCount() = 2
            override fun getItemViewType(position: Int) = if (isEnabled(position)) 1 else 0
            override fun getView(position: Int, reused: View?, parent: ViewGroup): View {
                val item = items[position]
                if (item.open == null) {
                    return ((reused as? TextView) ?: label("", 17f).apply {
                        typeface = headline
                        setPadding(dp(8), dp(5), dp(8), dp(5))
                        setBackgroundColor(fog)
                        if (android.os.Build.VERSION.SDK_INT >= 28) isAccessibilityHeading = true
                    }).apply { text = item.title }
                }
                val entry = (reused as? IndexRow) ?: IndexRow()
                entry.bind(item)
                return entry
            }
        }
    }

    private inner class IndexRow : LinearLayout(context) {
        private val title = label("", 17f).apply {
            typeface = medium
            maxLines = 2
            ellipsize = android.text.TextUtils.TruncateAt.END
        }
        private val subtitle = label("", 14f, secondary).apply {
            maxLines = 2
            ellipsize = android.text.TextUtils.TruncateAt.END
        }
        private val album = label("", 13f, secondary).apply {
            maxLines = 1
            ellipsize = android.text.TextUtils.TruncateAt.END
        }
        private val leading = ImageView(context).apply { importantForAccessibility = View.IMPORTANT_FOR_ACCESSIBILITY_NO }
        private val arrow = ImageView(context).apply { setImageDrawable(mark(Mark.BROWSE, 17)); importantForAccessibility = View.IMPORTANT_FOR_ACCESSIBILITY_NO }
        private val detail = label("", 14f, secondary).apply {
            maxWidth = dp(92)
            gravity = Gravity.END
            maxLines = 2
            ellipsize = android.text.TextUtils.TruncateAt.END
        }
        private var bound: Entry? = null
        private val info = iconButton("Song information", Mark.INFO) { bound?.info?.invoke() }
        private val body = row().apply {
            minimumHeight = dp(if (compact) 48 else 50)
            val vertical = dp(if (compact) 4 else 7)
            setPadding(dp(5), vertical, dp(5), vertical)
            addView(leading, LayoutParams(dp(22), dp(24)).apply { marginEnd = dp(9) })
            addView(column().apply {
                addView(title)
                addView(subtitle)
                addView(album)
            }, LayoutParams(0, -2, 1f))
            addView(arrow)
            addView(detail.apply { setPadding(dp(8), 0, 0, 0) })
            background = StateListDrawable().apply {
                addState(intArrayOf(android.R.attr.state_pressed), surface(pressed, accent))
                addState(intArrayOf(android.R.attr.state_focused), surface(pressed, accent))
                addState(intArrayOf(), surface(paper, null))
            }
            isFocusable = true
            setOnClickListener { bound?.open?.invoke() }
        }
        init {
            orientation = VERTICAL
            addView(row().apply {
                addView(body, LayoutParams(0, -2, 1f))
                addView(info, LayoutParams(dp(48), dp(48)))
            })
            addView(line())
        }
        fun bind(item: Entry) {
            bound = item
            info.visibility = if (item.info == null) View.GONE else View.VISIBLE
            info.contentDescription = "Song information for ${item.title}"
            title.text = item.title
            leading.setImageDrawable(item.leadingMark?.let { mark(it, 20, accentInk) })
            leading.visibility = if (item.leadingMark == null) View.GONE else View.VISIBLE
            subtitle.text = item.subtitle
            profileLink(subtitle, item.openArtist)
            album.text = item.album
            album.visibility = if (item.album.isEmpty()) View.GONE else View.VISIBLE
            profileLink(album, item.openAlbum)
            subtitle.setCompoundDrawablesRelative(item.mark?.let { mark(it, 16, accent) }, null, null, null)
            subtitle.compoundDrawablePadding = dp(4)
            subtitle.visibility = if (item.subtitle.isEmpty()) View.GONE else View.VISIBLE
            arrow.visibility = if (item.destination) View.VISIBLE else View.GONE
            detail.text = item.detail
            detail.visibility = if (item.detail.isEmpty()) View.GONE else View.VISIBLE
        }
    }
}
