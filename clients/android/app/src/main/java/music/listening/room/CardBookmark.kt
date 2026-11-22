package music.listening.room

import android.content.Context
import android.net.Uri
import android.provider.DocumentsContract
import androidx.media3.common.MediaItem
import androidx.media3.common.Player

internal data class CardBookmark(val uri: Uri, val title: String, val position: Long,
                                 val playlist: String?, val occurrence: Int) {
    fun item() = MediaItem.Builder().setMediaId(uri.toString()).setUri(uri).build()

    companion object {
        const val PLAYLIST = "listening.card.playlist"

        fun load(context: Context, tree: Uri): CardBookmark? {
            val prefs = context.getSharedPreferences("card", Context.MODE_PRIVATE)
            val uri = prefs.getString("resume_uri", null)?.let(Uri::parse) ?: return null
            if (!belongsTo(uri, tree)) return null
            return CardBookmark(uri, prefs.getString("resume_title", null) ?: "Last song",
                prefs.getLong("resume_position", 0).coerceAtLeast(0),
                prefs.getString("resume_playlist", null), prefs.getInt("resume_occurrence", 0))
        }

        fun save(context: Context, player: Player) {
            val uri = player.currentMediaItem?.localConfiguration?.uri ?: return
            val prefs = context.getSharedPreferences("card", Context.MODE_PRIVATE)
            val tree = prefs.getString("tree", null)?.let(Uri::parse) ?: return
            if (!belongsTo(uri, tree) || player.playerError != null) return
            val title = player.mediaMetadata.title?.toString() ?: documentTitle(uri)
            val position = if (player.playbackState == Player.STATE_ENDED) 0 else player.currentPosition
            val playlist = player.currentMediaItem?.requestMetadata?.extras?.getString(PLAYLIST)
            prefs.edit().putString("resume_uri", uri.toString()).putString("resume_title", title)
                .putString("resume_playlist", playlist).putInt("resume_occurrence", player.currentMediaItemIndex)
                .putLong("resume_position", position).apply()
        }

        fun documentTitle(uri: Uri) =
            DocumentsContract.getDocumentId(uri).substringAfter(':').substringAfterLast('/').substringBeforeLast('.')

        private fun belongsTo(uri: Uri, tree: Uri): Boolean =
            uri.scheme == "content" && uri.authority == tree.authority &&
                runCatching {
                    val root = DocumentsContract.getTreeDocumentId(tree)
                    val prefix = if (root.endsWith(':')) root else root.trimEnd('/') + "/"
                    DocumentsContract.getTreeDocumentId(uri) == root &&
                        DocumentsContract.getDocumentId(uri).startsWith(prefix)
                }.getOrDefault(false)
    }
}
