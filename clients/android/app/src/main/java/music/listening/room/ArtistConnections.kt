package music.listening.room

import android.animation.ValueAnimator
import android.app.Activity
import android.content.ActivityNotFoundException
import android.content.Intent
import android.graphics.Canvas
import android.graphics.Paint
import android.graphics.Path
import android.graphics.Rect
import android.net.Uri
import android.view.Gravity
import android.view.View
import android.view.animation.DecelerateInterpolator
import android.widget.Button
import android.widget.FrameLayout
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.Toast
import okhttp3.Call
import org.json.JSONObject
import java.util.concurrent.Executors
import java.util.concurrent.Future

private val catalogId = Regex("[1-9][0-9]{0,9}")
private val musicBrainzId = Regex("[a-f0-9]{8}(-[a-f0-9]{4}){3}-[a-f0-9]{12}")
private val musicBrainzKinds = setOf("artist", "recording", "release-group")
internal val metadataRequests = Executors.newFixedThreadPool(2)

internal fun JSONObject.creditTrackId(): String? = optString("id").takeIf(catalogId::matches)
internal fun JSONObject.creditRecordingId(): String? = optString("musicbrainz_id").takeIf(musicBrainzId::matches)

internal data class CreditTarget(val kind: String, val id: String) {
    val path: String get() = "/api/connections/$kind/$id"
}
internal data class CreditNode(val target: CreditTarget, val name: String, val detail: String)
internal data class CreditEdge(val node: CreditNode, val label: String, val note: String)
internal sealed interface Credits {
    data class Ready(val root: CreditNode, val branches: List<CreditEdge>) : Credits
    data class Unmatched(val artist: CreditNode?, val retry: Boolean) : Credits
}

private fun JSONObject.creditNode(): CreditNode? {
    val kind = optString("kind")
    val id = optString("id")
    val name = optString("title")
    if (kind !in musicBrainzKinds || !musicBrainzId.matches(id) || name.isBlank()) return null
    return CreditNode(CreditTarget(kind, id), name, optString("detail"))
}

private fun JSONObject.credits(): Credits = when (optString("state")) {
    "ready" -> {
        val root = requireNotNull(getJSONObject("root").creditNode())
        val branches = getJSONArray("edges")
        Credits.Ready(root, (0 until minOf(branches.length(), 9)).mapNotNull { index ->
            val branch = branches.optJSONObject(index) ?: return@mapNotNull null
            val node = branch.optJSONObject("node")?.creditNode() ?: return@mapNotNull null
            CreditEdge(node, branch.optString("label"), branch.optString("note"))
        })
    }
    "unmatched" -> Credits.Unmatched(optJSONObject("credit")?.creditNode(), optBoolean("retry"))
    else -> error("Unknown credit response")
}

internal fun RoomUi.showArtistConnections(activity: Activity, room: Room, track: JSONObject, album: Boolean = false,
                                          openView: ((SongDetailsView) -> Unit)? = null,
                                          cache: MutableMap<CreditTarget, Credits> = mutableMapOf()) {
    val id = track.creditTrackId()
    val target = when {
        id != null -> CreditTarget(if (album) "album" else "song", id)
        album -> return
        else -> CreditTarget("recording", track.creditRecordingId() ?: return)
    }
    val title = track.optString(if (album) "album" else "title")
    if (title.isBlank()) return
    ArtistConnectionsDialog(activity, this, room, target, track.optString("title").ifBlank { title },
        track.optString("artist"), id != null && track.optString("album").isNotBlank(), openView, cache).show()
}

internal class MetadataRequest {
    var future: Future<*>? = null
    private var call: Call? = null
    private var cancelled = false

    @Synchronized
    fun bind(value: Call) {
        if (cancelled) value.cancel() else call = value
    }

    @Synchronized
    fun cancel() {
        cancelled = true
        call?.cancel()
        future?.cancel(true)
    }
}

private class ArtistConnectionsDialog(
    private val activity: Activity,
    private val ui: RoomUi,
    private val room: Room,
    private val origin: CreditTarget,
    private val title: String,
    private val artist: String,
    private val hasAlbum: Boolean,
    private val openView: ((SongDetailsView) -> Unit)?,
    private val cache: MutableMap<CreditTarget, Credits>,
) {
    private data class Page(val target: CreditTarget, val from: CreditEdge? = null)

    private val history = mutableListOf(Page(origin))
    private var pending: MetadataRequest? = null
    private val style = SongDetailsStyle(ui)
    private val subject = ui.heading("").apply {
        isFocusable = true
        accessibilityLiveRegion = View.ACCESSIBILITY_LIVE_REGION_POLITE
    }
    private val stage = ui.column()
    private val scroll = ScrollView(activity)
    private val back = ui.iconButton("Previous connection", Mark.BACK) {
        history.removeAt(history.lastIndex)
        load()
    }
    private val dialog = style.dialog(activity, title, scroll, artist)

    fun show() {
        scroll.addView(with(ui) {
            style.column().apply {
                if (openView != null) {
                    val selected = if (origin.kind == "album") SongDetailsView.ALBUM else SongDetailsView.CREDITS
                    section(style.navigation(selected, hasAlbum) {
                        dialog.dismiss()
                        openView.invoke(it)
                    })
                }
                section(row().apply {
                    addView(back, LinearLayout.LayoutParams(dp(48), dp(48)))
                    addView(subject, LinearLayout.LayoutParams(0, -2, 1f))
                }, gap = 8)
                addView(stage)
            }
        })
        scroll.addOnAttachStateChangeListener(object : View.OnAttachStateChangeListener {
            override fun onViewAttachedToWindow(view: View) = Unit
            override fun onViewDetachedFromWindow(view: View) = cancelRequest()
        })
        dialog.setOnDismissListener { cancelRequest() }
        dialog.show()
        load()
    }

    private fun follow(edge: CreditEdge) {
        history.add(Page(edge.node.target, edge))
        load()
    }

    private fun cancelRequest() {
        pending?.cancel()
        pending = null
    }

    private fun load() {
        cancelRequest()
        val page = history.last()
        back.visibility = if (history.size > 1) View.VISIBLE else View.GONE
        scroll.scrollTo(0, 0)
        stage.removeAllViews()
        cache[page.target]?.let { render(it, page.from); return }
        subject.text = page.from?.node?.name ?: title
        stage.addView(style.loading("Looking up credits…"))
        val request = MetadataRequest()
        pending = request
        request.future = metadataRequests.submit {
            val result = runCatching { room.metadata(page.target.path, request::bind).credits() }
            activity.runOnUiThread {
                if (!dialog.isShowing || activity.isDestroyed || pending !== request) return@runOnUiThread
                pending = null
                stage.removeAllViews()
                result.onSuccess {
                    if (it !is Credits.Unmatched || !it.retry) cache[page.target] = it
                    render(it, page.from)
                }.onFailure { failure ->
                    subject.text = "Credits unavailable"
                    val message = when (failure) {
                        is PairingRequired -> failure.message.orEmpty()
                        else -> "Couldn’t load these credits. Try again."
                    }
                    with(ui) {
                        stage.section(panel().apply {
                            section(label(message, color = secondary), gap = 8)
                            addView(button("Try again", action = ::load))
                        })
                    }
                }
            }
        }
    }

    private fun render(credits: Credits, from: CreditEdge?) = with(ui) {
        when (credits) {
            is Credits.Ready -> {
                val graph = CreditGraph(activity, ui, credits, ::follow)
                stage.section(graph)
                if (ValueAnimator.areAnimatorsEnabled()) {
                    graph.alpha = 0f
                    graph.translationY = ui.dp(8).toFloat()
                    graph.animate().alpha(1f).translationY(0f).setDuration(240)
                        .setInterpolator(DecelerateInterpolator()).start()
                }
                val root = credits.root
                subject.text = if (root.name == title) "Connections" else root.name
                if (!subject.isInTouchMode) subject.requestFocus()
                val facts = listOf(root.detail, from?.label.orEmpty(), from?.note.orEmpty())
                    .filter(String::isNotBlank).joinToString(" · ")
                stage.section(panel().apply {
                    if (facts.isNotBlank()) {
                        section(heading("Credit details", 19f), gap = 4)
                        section(label(facts, color = secondary), gap = 8)
                    }
                    if (credits.branches.isEmpty()) {
                        section(label("MusicBrainz has no additional connections for this credit.", color = secondary), gap = 8)
                    }
                    addView(button("MusicBrainz") { openSource(root) }.apply {
                        setCompoundDrawables(null, null, mark(Mark.EXTERNAL, 16, accentInk), null)
                        compoundDrawablePadding = dp(6)
                    })
                })
            }
            is Credits.Unmatched -> {
                val kind = if (origin.kind == "album") "album" else "recording"
                subject.text = if (credits.retry) "Credits unavailable" else "No confirmed match"
                stage.section(panel().apply {
                    val message = if (credits.retry) "These credits couldn’t load."
                        else "MusicBrainz has no confirmed match for this $kind."
                    section(label(message, color = secondary), gap = 8)
                    if (credits.retry) section(button("Try again", action = ::load), gap = 8)
                    val known = credits.artist
                    if (known != null) addView(button("Explore ${known.name}") {
                        follow(CreditEdge(known, "Library artist", ""))
                    })
                })
            }
        }
    }

    private fun openSource(root: CreditNode) {
        val target = root.target
        val link = Uri.Builder().scheme("https").authority("musicbrainz.org")
            .appendPath(target.kind).appendPath(target.id).build()
        try {
            activity.startActivity(Intent(Intent.ACTION_VIEW, link))
        } catch (_: ActivityNotFoundException) {
            Toast.makeText(activity, "No browser is available on this device.", Toast.LENGTH_SHORT).show()
        }
    }
}

private class CreditGraph(
    activity: Activity,
    private val ui: RoomUi,
    credits: Credits.Ready,
    follow: (CreditEdge) -> Unit,
) : FrameLayout(activity) {
    private val narrow = resources.configuration.screenWidthDp < 540
    private val paint = Paint(Paint.ANTI_ALIAS_FLAG).apply { style = Paint.Style.STROKE; strokeWidth = ui.dp(1).toFloat() }
    private val route = Path()
    private val root = graphButton(credits.root.name, credits.root.detail, true) {}
    private val branches = credits.branches.map { edge -> graphButton(edge.node.name, edge.label, false) { follow(edge) } }

    init {
        background = ui.surface(ui.fog)
        clipToOutline = true
        val split = (branches.size + 1) / 2
        val rails = ui.row().apply {
            gravity = if (narrow) Gravity.TOP else Gravity.CENTER_VERTICAL
            addView(rail(branches.take(split)), LinearLayout.LayoutParams(0, -2, 1f))
            if (!narrow) addView(root, LinearLayout.LayoutParams(0, -2, 1f).apply {
                marginStart = ui.dp(14)
                marginEnd = ui.dp(14)
            })
            addView(rail(branches.drop(split)), LinearLayout.LayoutParams(0, -2, 1f).apply {
                if (narrow) marginStart = ui.dp(10)
            })
        }
        addView(ui.column().apply {
            setPadding(ui.dp(10), ui.dp(12), ui.dp(10), ui.dp(8))
            if (narrow) addView(root, LinearLayout.LayoutParams(-2, -2).apply {
                gravity = Gravity.CENTER_HORIZONTAL
                bottomMargin = if (branches.isEmpty()) 0 else ui.dp(20)
            })
            if (!narrow || branches.isNotEmpty()) addView(rails)
        })
    }

    private fun rail(nodes: List<Button>) = ui.column().apply {
        nodes.forEach { button ->
            addView(button, LinearLayout.LayoutParams(-1, -2).apply { bottomMargin = ui.dp(7) })
        }
    }

    private fun graphButton(title: String, detail: String, hub: Boolean, action: () -> Unit) =
        ui.button(if (detail.isBlank()) title else "$title\n$detail", solid = hub, action = action).apply {
            contentDescription = listOf(title, detail).filter(String::isNotBlank).joinToString(" · ")
            textSize = if (hub) 15f else 13f
            typeface = ui.headline
            setPadding(ui.dp(6), ui.dp(8), ui.dp(6), ui.dp(8))
            minHeight = ui.dp(54)
            minimumHeight = ui.dp(54)
            if (hub) {
                isClickable = false
                isFocusable = false
                importantForAccessibility = View.IMPORTANT_FOR_ACCESSIBILITY_YES
            }
        }

    private fun center(child: View): Pair<Float, Float> {
        val rect = Rect(0, 0, child.width, child.height)
        offsetDescendantRectToMyCoords(child, rect)
        return rect.exactCenterX() to rect.exactCenterY()
    }

    override fun dispatchDraw(canvas: Canvas) {
        val (cx, cy) = center(root)
        paint.color = ui.rule
        for (ring in 1..5) canvas.drawCircle(cx, cy, ui.dp(ring * 32).toFloat(), paint)
        paint.color = ui.accent
        for (branch in branches) {
            val (x, y) = center(branch)
            route.rewind()
            route.moveTo(cx, cy)
            if (narrow) {
                val middle = (cy + y) / 2
                route.cubicTo(cx, middle, x, middle, x, y)
            } else {
                val middle = (cx + x) / 2
                route.cubicTo(middle, cy, middle, y, x, y)
            }
            canvas.drawPath(route, paint)
        }
        super.dispatchDraw(canvas)
    }
}
