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
internal class RecordSleeve(context: Context, private val ui: RoomUi,
                            favoriteAction: View? = null) : FrameLayout(context) {
    private var picture: Bitmap? = null
    private var viewer: Dialog? = null
    private val monochrome = ColorMatrixColorFilter(ColorMatrix().apply { setSaturation(0f) })
    private val fallback = ui.mark(Mark.ALBUM, if (ui.eInk) 54 else 96)
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
        val badgeSize = ui.dp(if (ui.eInk) 24 else 30)
        addView(expand, LayoutParams(badgeSize, badgeSize, Gravity.TOP or Gravity.END).apply {
            topMargin = ui.dp(6)
            marginEnd = ui.dp(6)
        })
        setOnClickListener { showCover() }
        isClickable = false
        isFocusable = false
    }
    private val title = ui.heading("", if (ui.eInk) 25f else 29f).apply {
        maxLines = 2
        ellipsize = TextUtils.TruncateAt.END
    }
    private val artist = ui.label("", if (ui.eInk) 16f else 17f).apply {
        maxLines = 1
        ellipsize = TextUtils.TruncateAt.END
    }
    private val album = ui.label("", 13f, ui.secondary).apply {
        maxLines = 1
        ellipsize = TextUtils.TruncateAt.END
    }
    private val artwork = resources.displayMetrics.let { metrics ->
        SleeveArtwork(minOf(metrics.widthPixels, metrics.heightPixels), ::display)
    }
    private val preferredSize = resources.configuration.let {
        ui.dp(minOf(216, it.screenHeightDp * 36 / 100, it.screenWidthDp - 72).coerceAtLeast(100))
    }

    init {
        val metadata = ui.column(1).apply {
            addView(title)
            addView(artist)
            addView(album)
        }
        val caption = if (favoriteAction == null) metadata else ui.row(8).apply {
            addView(metadata, LinearLayout.LayoutParams(0, -2, 1f))
            addView(favoriteAction, LinearLayout.LayoutParams(ui.dp(48), ui.dp(48)))
        }
        val sleeve = if (ui.eInk) {
            ui.row(14).apply {
                val size = ui.dp(104)
                addView(cover, LinearLayout.LayoutParams(size, size))
                addView(caption, LinearLayout.LayoutParams(0, -2, 1f))
            }
        } else {
            ui.column(10).apply {
                addView(cover, LinearLayout.LayoutParams(preferredSize, preferredSize).apply {
                    gravity = Gravity.CENTER_HORIZONTAL
                })
                addView(caption)
            }
        }
        addView(sleeve, LayoutParams(-1, -2))
    }

    fun limitArtworkHeight(maxHeight: Int) {
        val size = minOf(preferredSize, maxOf(ui.dp(96), maxHeight))
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
        val label = if (picture == null) null else "View cover for ${title.text}, ${artist.text}"
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
        for ((label, key) in listOf(title to "title", artist to "artist", album to "album")) {
            val value = track.optString(key)
            if (!TextUtils.equals(label.text, value)) label.text = value
            label.visibility = if (value.isEmpty()) View.GONE else View.VISIBLE
        }
        describeCover()
        artwork.bind(ref, track.optString("cover"), embedded)
    }

    fun linkProfiles(openArtist: (() -> Unit)?, openAlbum: (() -> Unit)?) {
        ui.profileLink(artist, openArtist)
        ui.profileLink(album, openAlbum)
    }

    fun clear() = artwork.clear()

    override fun onDetachedFromWindow() {
        clear()
        super.onDetachedFromWindow()
    }
}
