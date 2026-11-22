package music.listening.room

import android.content.Context
import android.graphics.Canvas
import android.graphics.Paint
import android.os.Bundle
import android.view.accessibility.AccessibilityNodeInfo
import android.view.accessibility.AccessibilityNodeInfo.AccessibilityAction
import android.view.KeyEvent
import android.view.MotionEvent
import android.widget.FrameLayout
import kotlin.math.roundToInt
import org.json.JSONObject

internal data class MoodPoint(val x: Float, val y: Float)
internal data class RoomMood(val id: String, val name: String, val genres: List<String>, val point: MoodPoint,
                             val supportsBlend: Boolean) {
    companion object {
        // Pre-blend rooms exposed the original five IDs without coordinates.
        private val legacy = mapOf("energetic" to MoodPoint(.29f, .22f), "upbeat" to MoodPoint(.76f, .29f),
            "focused" to MoodPoint(.54f, .51f), "reflective" to MoodPoint(.22f, .70f), "calm" to MoodPoint(.75f, .78f))
        fun read(row: JSONObject): RoomMood? {
            val id = row.getString("id")
            val placed = row.has("x") && row.has("y")
            val point = if (placed) MoodPoint(row.getInt("x") / 100f, row.getInt("y") / 100f) else legacy[id] ?: return null
            val tags = row.getJSONArray("genres")
            return RoomMood(id, row.getString("name"), (0 until tags.length()).map { tags.getString(it) }, point, placed)
        }
    }
}

internal data class MoodMix(val first: RoomMood, val second: RoomMood? = null, val percent: Int = 0) {
    val title get() = second?.let { first.name + " + " + it.name } ?: first.name
    val spokenMix get() = second?.let { "${first.name} ${100 - percent}% · ${it.name} ${percent}%" } ?: title
    val genres get() = (first.genres + second?.genres.orEmpty()).distinct().sorted()
    fun fields(genre: String) = JSONObject().put("mood", first.id).put("genre", genre).apply {
        second?.let { put("blend", it.id); put("blend_percent", percent) }
    }
}

internal class MoodMap(context: Context, private val ui: RoomUi, private val moods: List<RoomMood>,
                       private val choose: (MoodMix) -> Unit) : FrameLayout(context) {
    private val initial = moods.firstOrNull { it.id == "focused" } ?: moods.first()
    private var committed = initial.point
    private var point = committed
    private val labels = moods.map { mood ->
        ui.button(mood.name) { update(mood.point); performClick() }.apply {
            textSize = 13f
            setPadding(ui.dp(6), 0, ui.dp(6), 0)
            addView(this, LayoutParams(-2, ui.dp(48)))
        }
    }
    private val paint = Paint(Paint.ANTI_ALIAS_FLAG)

    init {
        minimumHeight = ui.dp(255)
        background = ui.surface(ui.fog)
        clipToOutline = true
        setWillNotDraw(false)
        isFocusable = true
        isClickable = true
        contentDescription = "Blend moods. Use arrow keys to move and Enter to play."
    }

    val selection: MoodMix get() = mix(committed)

    private fun mix(position: MoodPoint = point): MoodMix {
        val nearest = moods.map { mood ->
            val dx = mood.point.x - position.x
            val dy = mood.point.y - position.y
            mood to dx * dx + dy * dy
        }.sortedBy { it.second }
        val (first, a) = nearest.first()
        val (second, b) = nearest.getOrNull(1) ?: return MoodMix(first)
        val weight = if (a + b > 0) (100 * a / (a + b)).roundToInt() else 0
        return if (first.supportsBlend && second.supportsBlend && weight >= 5) MoodMix(first, second, weight) else MoodMix(first)
    }

    private fun update(next: MoodPoint) {
        point = next
        val mix = mix()
        labels.forEachIndexed { i, button -> button.isSelected = moods[i] == mix.first || moods[i] == mix.second }
        invalidate()
    }

    override fun onLayout(changed: Boolean, left: Int, top: Int, right: Int, bottom: Int) {
        for ((index, mood) in moods.withIndex()) {
            val label = labels[index]
            val inset = ui.dp(6)
            val x = (mood.point.x * width - label.measuredWidth / 2).toInt()
                .coerceIn(inset, (width - label.measuredWidth - inset).coerceAtLeast(inset))
            val y = (mood.point.y * height - label.measuredHeight / 2).toInt()
            label.layout(x, y, x + label.measuredWidth, y + label.measuredHeight)
        }
    }

    override fun onDraw(canvas: Canvas) {
        super.onDraw(canvas)
        paint.style = Paint.Style.STROKE
        paint.strokeWidth = ui.dp(1).toFloat()
        paint.color = ui.rule
        for (mood in moods) {
            val x = mood.point.x * width
            val y = mood.point.y * height
            canvas.save()
            canvas.rotate(-24f, x, y)
            for (radius in listOf(.08f, .14f, .21f)) {
                canvas.drawOval(x - width * radius, y - height * radius * .7f,
                    x + width * radius, y + height * radius * .7f, paint)
            }
            canvas.restore()
        }
        paint.color = ui.accent
        canvas.drawCircle(point.x * width, point.y * height, ui.dp(8).toFloat(), paint)
        paint.style = Paint.Style.FILL
        paint.color = ui.secondary
        paint.textSize = ui.dp(12).toFloat()
        canvas.drawText("High energy", ui.dp(12).toFloat(), ui.dp(21).toFloat(), paint)
        canvas.drawText("Low energy", ui.dp(12).toFloat(), (height - ui.dp(12)).toFloat(), paint)
    }

    private fun pointAt(x: Float, y: Float): MoodPoint {
        val index = labels.indexOfFirst { x >= it.left && x < it.right && y >= it.top && y < it.bottom }
        return moods.getOrNull(index)?.point ?: MoodPoint((x / width).coerceIn(0f, 1f), (y / height).coerceIn(0f, 1f))
    }

    // Handle a single gesture across labels and the space between them.
    // Label clicks remain available to keyboard and accessibility services.
    override fun onInterceptTouchEvent(event: MotionEvent): Boolean = true

    override fun dispatchHoverEvent(event: MotionEvent): Boolean {
        when (event.actionMasked) {
            MotionEvent.ACTION_HOVER_ENTER, MotionEvent.ACTION_HOVER_MOVE -> update(pointAt(event.x, event.y))
            MotionEvent.ACTION_HOVER_EXIT -> update(committed)
        }
        return super.dispatchHoverEvent(event)
    }

    override fun onTouchEvent(event: MotionEvent): Boolean {
        val inside = event.x in 0f..width.toFloat() && event.y in 0f..height.toFloat()
        val next = pointAt(event.x, event.y)
        when (event.actionMasked) {
            MotionEvent.ACTION_DOWN, MotionEvent.ACTION_MOVE -> {
                parent?.requestDisallowInterceptTouchEvent(true)
                update(next)
            }
            MotionEvent.ACTION_UP -> {
                if (inside) { update(next); performClick() } else update(committed)
            }
            MotionEvent.ACTION_CANCEL -> update(committed)
            else -> return super.onTouchEvent(event)
        }
        return true
    }

    private fun step(key: Int): Boolean {
        val next = when (key) {
            KeyEvent.KEYCODE_DPAD_LEFT -> point.copy(x = (point.x - .05f).coerceAtLeast(0f))
            KeyEvent.KEYCODE_DPAD_RIGHT -> point.copy(x = (point.x + .05f).coerceAtMost(1f))
            KeyEvent.KEYCODE_DPAD_UP -> point.copy(y = (point.y - .05f).coerceAtLeast(0f))
            KeyEvent.KEYCODE_DPAD_DOWN -> point.copy(y = (point.y + .05f).coerceAtMost(1f))
            else -> return false
        }
        committed = next
        update(next)
        return true
    }

    override fun onKeyDown(keyCode: Int, event: KeyEvent) = step(keyCode) || super.onKeyDown(keyCode, event)

    override fun onInitializeAccessibilityNodeInfo(info: AccessibilityNodeInfo) {
        super.onInitializeAccessibilityNodeInfo(info)
        info.contentDescription = "Blend moods · " + mix().spokenMix
        for ((action, label) in listOf(AccessibilityAction.ACTION_SCROLL_LEFT to "More introspective",
            AccessibilityAction.ACTION_SCROLL_RIGHT to "Brighter", AccessibilityAction.ACTION_SCROLL_UP to "Higher energy",
            AccessibilityAction.ACTION_SCROLL_DOWN to "Lower energy")) {
            info.addAction(AccessibilityAction(action.id, label))
        }
    }

    override fun performAccessibilityAction(action: Int, arguments: Bundle?): Boolean {
        val key = when (action) {
            AccessibilityAction.ACTION_SCROLL_LEFT.id -> KeyEvent.KEYCODE_DPAD_LEFT
            AccessibilityAction.ACTION_SCROLL_RIGHT.id -> KeyEvent.KEYCODE_DPAD_RIGHT
            AccessibilityAction.ACTION_SCROLL_UP.id -> KeyEvent.KEYCODE_DPAD_UP
            AccessibilityAction.ACTION_SCROLL_DOWN.id -> KeyEvent.KEYCODE_DPAD_DOWN
            else -> return super.performAccessibilityAction(action, arguments)
        }
        step(key)
        announceForAccessibility(mix().spokenMix)
        return true
    }

    override fun performClick(): Boolean {
        super.performClick()
        committed = point
        choose(mix())
        return true
    }
}
