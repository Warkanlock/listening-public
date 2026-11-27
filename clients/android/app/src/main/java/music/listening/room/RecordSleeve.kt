package music.listening.room

import android.animation.ValueAnimator
import android.app.Dialog
import android.content.Context
import android.graphics.Bitmap
import android.graphics.Color
import android.graphics.ColorMatrix
import android.graphics.ColorMatrixColorFilter
import android.os.Build
import android.text.TextUtils
import android.view.Gravity
import android.view.View
import android.view.WindowInsets
import android.view.animation.DecelerateInterpolator
import android.widget.FrameLayout
import android.widget.ImageView
import android.widget.LinearLayout
import org.json.JSONObject

/** Cover artwork and track details shared by room and device-file playback. */
internal class RecordSleeve(context: Context, private val ui: RoomUi) : FrameLayout(context) {
    private var picture: Bitmap? = null
    private var viewer: Dialog? = null
    private val monochrome = ColorMatrixColorFilter(ColorMatrix().apply { setSaturation(0f) })
    private val fallback = ui.mark(Mark.ALBUM, if (ui.eInk) 64 else 90)
    private val art = ImageView(context).apply {
        importantForAccessibility = View.IMPORTANT_FOR_ACCESSIBILITY_NO
        scaleType = ImageView.ScaleType.CENTER
        setImageDrawable(fallback)
        if (ui.eInk) colorFilter = monochrome
    }
    private val expand = ImageView(context).apply {
        scaleType = ImageView.ScaleType.CENTER
        setImageDrawable(ui.mark(Mark.EXPAND, if (ui.eInk) 15 else 18))
        background = ui.surface(ui.paper, ui.ink)
        importantForAccessibility = View.IMPORTANT_FOR_ACCESSIBILITY_NO
        visibility = View.GONE
    }
    private val cover = FrameLayout(context).apply {
        background = ui.surface(if (ui.eInk) ui.paper else ui.fog)
        clipToOutline = true
        addView(art, LayoutParams(-1, -1))
        val badgeSize = ui.dp(if (ui.eInk) 22 else 26)
        addView(expand, LayoutParams(badgeSize, badgeSize, Gravity.TOP or Gravity.END).apply {
            topMargin = ui.dp(6)
            marginEnd = ui.dp(6)
        })
        setOnClickListener { showCover() }
        isClickable = false
        isFocusable = false
    }
    private val title = ui.heading("", 26f).apply {
        gravity = Gravity.CENTER
        maxLines = 2
        ellipsize = TextUtils.TruncateAt.END
    }
    private val artist = ui.label("", 17f, ui.secondary).apply {
        gravity = Gravity.CENTER
        maxLines = 2
        ellipsize = TextUtils.TruncateAt.END
    }
    private val artwork = resources.displayMetrics.let { metrics ->
        SleeveArtwork(minOf(metrics.widthPixels, metrics.heightPixels), ::display)
    }
    private val preferredSize = resources.configuration.let { size ->
        val limit = when {
            ui.compactPlayerLandscape && ui.eInk -> 104
            ui.compactPlayerLandscape -> 168
            ui.eInk -> 168
            else -> 240
        }
        ui.dp(minOf(limit, size.screenHeightDp * 37 / 100, size.screenWidthDp - 70).coerceAtLeast(80))
    }
    private var heightLimit = preferredSize
    private val coverSize get() = minOf(heightLimit, if (picture == null && ui.eInk) ui.dp(84) else preferredSize)

    init {
        val sleeve = ui.column().apply {
            val padding = ui.dp(8)
            setPadding(padding, 0, padding, 0)
            addView(cover, LinearLayout.LayoutParams(coverSize, coverSize).apply {
                gravity = Gravity.CENTER_HORIZONTAL
                bottomMargin = ui.dp(if (ui.eInk) 6 else 12)
            })
            addView(title, LinearLayout.LayoutParams(-1, -2))
            addView(artist, LinearLayout.LayoutParams(-1, -2))
        }
        addView(sleeve, LayoutParams(-1, -2))
    }

    fun limitArtworkHeight(maxHeight: Int) {
        heightLimit = minOf(preferredSize, maxOf(ui.dp(80), maxHeight))
        updateCoverSize()
    }

    private fun updateCoverSize() {
        val size = coverSize
        val bounds = cover.layoutParams
        if (bounds.width != size) {
            bounds.width = size
            bounds.height = size
            cover.requestLayout()
        }
    }

    private fun display(image: Bitmap?) {
        if (picture === image) return
        viewer?.dismiss()
        picture = image
        updateCoverSize()
        art.animate().cancel()
        art.alpha = 1f
        if (image == null) {
            art.scaleType = ImageView.ScaleType.CENTER
            art.setImageDrawable(fallback)
        } else {
            art.scaleType = ImageView.ScaleType.CENTER_CROP
            art.setImageBitmap(image)
            if (!ui.eInk && ValueAnimator.areAnimatorsEnabled()) {
                art.alpha = 0.5f
                art.animate().alpha(1f).setDuration(220).setInterpolator(DecelerateInterpolator()).start()
            }
        }
        expand.visibility = if (image == null) View.GONE else View.VISIBLE
        cover.isClickable = image != null
        cover.isFocusable = image != null
        describeCover()
    }

    private fun describeCover() {
        val label = picture?.let { "View cover for ${title.text}" }
        if (!TextUtils.equals(cover.contentDescription, label)) cover.contentDescription = label
    }

    private fun showCover() {
        val image = picture ?: return
        if (viewer != null) return
        val dialog = Dialog(context, android.R.style.Theme_Material_Light_NoActionBar)
        val close = ui.iconButton("Close artwork", Mark.CLOSE) { dialog.dismiss() }
        val frame = ui.column().apply {
            setBackgroundColor(if (ui.eInk) ui.paper else Color.BLACK)
            addView(close, LinearLayout.LayoutParams(ui.dp(48), ui.dp(48)).apply { gravity = Gravity.END })
            addView(ImageView(context).apply {
                scaleType = ImageView.ScaleType.FIT_CENTER
                setImageBitmap(image)
                if (ui.eInk) colorFilter = monochrome
                contentDescription = "Cover for ${title.text}"
            }, LinearLayout.LayoutParams(-1, 0, 1f))
        }
        if (Build.VERSION.SDK_INT >= 35) {
            frame.setOnApplyWindowInsetsListener { view, insets ->
                val bars = insets.getInsets(WindowInsets.Type.systemBars())
                view.setPadding(bars.left, bars.top, bars.right, bars.bottom)
                insets
            }
        }
        dialog.setContentView(frame)
        dialog.setTitle("Cover for ${title.text}")
        dialog.setOnDismissListener { viewer = null }
        viewer = dialog
        dialog.show()
    }

    fun bind(track: JSONObject, ref: String, embedded: ByteArray?) {
        val name = track.optString("title")
        val performer = track.optString("artist")
        if (!TextUtils.equals(title.text, name)) title.text = name
        if (!TextUtils.equals(artist.text, performer)) artist.text = performer
        artist.visibility = if (performer.isBlank()) View.GONE else View.VISIBLE
        describeCover()
        artwork.bind(ref, track.optString("cover"), embedded)
    }

    fun clear() = artwork.clear()

    override fun onDetachedFromWindow() {
        clear()
        super.onDetachedFromWindow()
    }
}
