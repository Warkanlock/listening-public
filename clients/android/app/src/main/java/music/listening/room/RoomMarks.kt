package music.listening.room

import android.content.res.ColorStateList
import android.graphics.Canvas
import android.graphics.ColorFilter
import android.graphics.Paint
import android.graphics.Path
import android.graphics.PixelFormat
import android.graphics.drawable.Drawable

/** Small monochrome marks with the same stroke weight as the room's controls. */
internal enum class Mark {
    ROOM, LIBRARY, EXPLORE, RADIO, PLAYING, INSIGHTS, PLAY, PAUSE, PREVIOUS, NEXT, SHUFFLE,
    SETTINGS, BROWSE, INFO, MAGNET, BACK, FORWARD, CLOSE, ALBUM, EXTERNAL, FOLDER, QUEUE, POSITION, MORE, EXPAND,
    EXTENSIONS, SEARCH, REFRESH
}

internal class RoomMark(private val mark: Mark, private val baseColor: Int, private val size: Int) : Drawable() {
    private val pen = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        color = baseColor
        strokeWidth = 1.8f
        strokeCap = Paint.Cap.ROUND
        strokeJoin = Paint.Join.ROUND
    }

    override fun draw(c: Canvas) {
        val restore = c.save()
        c.translate(bounds.left.toFloat(), bounds.top.toFloat())
        c.scale(bounds.width() / 24f, bounds.height() / 24f)
        fun line(x: Int, y: Int, x2: Int, y2: Int) =
            c.drawLine(x.toFloat(), y.toFloat(), x2.toFloat(), y2.toFloat(), pen)
        fun triangle(x: Int, y: Int, x2: Int, y2: Int, x3: Int, y3: Int) {
            pen.style = Paint.Style.FILL
            c.drawPath(Path().apply { moveTo(x.toFloat(), y.toFloat()); lineTo(x2.toFloat(), y2.toFloat())
                lineTo(x3.toFloat(), y3.toFloat()); close() }, pen)
        }
        pen.style = Paint.Style.STROKE
        when (mark) {
            Mark.ROOM, Mark.PLAYING -> {
                line(3, 10, 3, 14); line(7, 6, 7, 18); line(12, 2, 12, 22)
                line(17, 7, 17, 17); line(21, 10, 21, 14)
            }
            Mark.LIBRARY -> { line(4, 5, 4, 20); line(9, 4, 9, 20); line(14, 6, 14, 20); line(17, 5, 21, 19) }
            Mark.INSIGHTS -> {
                line(3, 20, 21, 20); line(5, 16, 9, 11); line(9, 11, 13, 14)
                line(13, 14, 19, 6); line(16, 6, 19, 6); line(19, 6, 19, 9)
            }
            Mark.EXPLORE -> { c.drawCircle(12f, 12f, 8.5f, pen); triangle(16, 7, 13, 14, 8, 17) }
            Mark.RADIO -> {
                c.drawRoundRect(3f, 9f, 21f, 20f, 1.5f, 1.5f, pen)
                line(4, 9, 19, 3); line(6, 14, 10, 14); c.drawCircle(16.5f, 15f, 1.9f, pen)
            }
            Mark.FOLDER -> {
                c.drawPath(Path().apply {
                    moveTo(3f, 6f); lineTo(9f, 6f); lineTo(12f, 9f)
                    lineTo(21f, 9f); lineTo(21f, 19f); lineTo(3f, 19f); close()
                }, pen)
            }
            Mark.BACK -> { line(20, 12, 4, 12); line(4, 12, 11, 5); line(4, 12, 11, 19) }
            Mark.FORWARD -> { line(4, 12, 20, 12); line(20, 12, 13, 5); line(20, 12, 13, 19) }
            Mark.CLOSE -> { line(6, 6, 18, 18); line(18, 6, 6, 18) }
            Mark.EXTENSIONS -> {
                line(8, 3, 8, 7); line(16, 3, 16, 7); line(12, 16, 12, 21)
                c.drawPath(Path().apply {
                    moveTo(6f, 10f); lineTo(6f, 7f); lineTo(18f, 7f); lineTo(18f, 10f)
                    cubicTo(18f, 18f, 6f, 18f, 6f, 10f)
                }, pen)
            }
            Mark.SEARCH -> { c.drawCircle(10f, 10f, 6.5f, pen); line(15, 15, 21, 21) }
            Mark.REFRESH -> {
                c.drawArc(4f, 4f, 20f, 20f, 10f, 320f, false, pen)
                line(20, 4, 20, 10); line(20, 10, 14, 10)
            }
            Mark.EXPAND -> {
                line(9, 4, 4, 4); line(4, 4, 4, 9)
                line(15, 4, 20, 4); line(20, 4, 20, 9)
                line(9, 20, 4, 20); line(4, 20, 4, 15)
                line(15, 20, 20, 20); line(20, 20, 20, 15)
            }
            Mark.QUEUE -> { line(4, 6, 20, 6); line(4, 12, 20, 12); line(4, 18, 14, 18) }
            Mark.POSITION -> { line(3, 12, 11, 12); c.drawCircle(14f, 12f, 3f, pen); line(17, 12, 21, 12) }
            Mark.MORE -> {
                pen.style = Paint.Style.FILL
                c.drawCircle(5f, 12f, 1.7f, pen)
                c.drawCircle(12f, 12f, 1.7f, pen)
                c.drawCircle(19f, 12f, 1.7f, pen)
            }
            Mark.ALBUM -> { c.drawCircle(12f, 12f, 9f, pen); c.drawCircle(12f, 12f, 3f, pen) }
            Mark.EXTERNAL -> { line(5, 19, 19, 5); line(7, 5, 19, 5); line(19, 5, 19, 17) }
            Mark.INFO -> { c.drawCircle(12f, 12f, 9f, pen); line(12, 11, 12, 17); c.drawPoint(12f, 7.5f, pen) }
            Mark.BROWSE -> { line(9, 7, 14, 12); line(14, 12, 9, 17) }
            Mark.MAGNET -> {
                c.drawPath(Path().apply {
                    moveTo(4f, 4f); lineTo(8f, 4f); lineTo(8f, 12f)
                    cubicTo(8f, 17.3f, 16f, 17.3f, 16f, 12f)
                    lineTo(16f, 4f); lineTo(20f, 4f); lineTo(20f, 12f)
                    cubicTo(20f, 22.6f, 4f, 22.6f, 4f, 12f); close()
                }, pen)
                line(4, 8, 8, 8); line(16, 8, 20, 8)
            }
            Mark.PLAY -> triangle(7, 4, 20, 12, 7, 20)
            Mark.PAUSE -> { pen.strokeWidth = 4f; line(8, 5, 8, 19); line(16, 5, 16, 19); pen.strokeWidth = 1.8f }
            Mark.NEXT -> { triangle(5, 5, 16, 12, 5, 19); line(19, 5, 19, 19) }
            Mark.PREVIOUS -> { triangle(19, 5, 8, 12, 19, 19); line(5, 5, 5, 19) }
            Mark.SHUFFLE -> {
                line(3, 6, 6, 6); line(6, 6, 16, 18); line(16, 18, 21, 18)
                line(17, 14, 21, 18); line(21, 18, 17, 22)
                line(3, 18, 6, 18); line(6, 18, 10, 13)
                line(13, 10, 16, 6); line(16, 6, 21, 6)
                line(17, 2, 21, 6); line(21, 6, 17, 10)
            }
            Mark.SETTINGS -> {
                line(3, 7, 8, 7); line(14, 7, 21, 7); c.drawCircle(11f, 7f, 2.6f, pen)
                line(3, 17, 12, 17); line(18, 17, 21, 17); c.drawCircle(15f, 17f, 2.6f, pen)
            }
        }
        c.restoreToCount(restore)
    }
    private var tint: ColorStateList? = null
    override fun setTintList(colors: ColorStateList?) {
        tint = colors
        onStateChange(state)
    }
    override fun isStateful() = tint?.isStateful == true
    override fun onStateChange(state: IntArray): Boolean {
        val next = tint?.getColorForState(state, baseColor) ?: baseColor
        if (pen.color == next) return false
        pen.color = next
        invalidateSelf()
        return true
    }
    override fun getIntrinsicWidth() = size
    override fun getIntrinsicHeight() = size
    override fun setAlpha(alpha: Int) { pen.alpha = alpha }
    override fun setColorFilter(filter: ColorFilter?) { pen.colorFilter = filter }
    @Deprecated("Deprecated in Java") override fun getOpacity() = PixelFormat.TRANSLUCENT
}
