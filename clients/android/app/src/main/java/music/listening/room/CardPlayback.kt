package music.listening.room

import android.app.PendingIntent
import android.content.Intent
import android.os.Handler
import android.os.Looper
import androidx.media3.common.AudioAttributes
import androidx.media3.common.C
import androidx.media3.common.Player
import androidx.media3.datasource.DataSource
import androidx.media3.datasource.DefaultDataSource
import androidx.media3.datasource.FileDataSource
import androidx.media3.exoplayer.ExoPlayer
import androidx.media3.exoplayer.source.DefaultMediaSourceFactory
import androidx.media3.session.MediaSession
import androidx.media3.session.MediaSessionService

/** The card player reads only device files and Android content URIs. */
class CardPlayback : MediaSessionService() {
    private lateinit var session: MediaSession
    private val handler = Handler(Looper.getMainLooper())
    private val checkpoint = object : Runnable {
        override fun run() {
            savePosition()
            if (session.player.isPlaying) handler.postDelayed(this, 5000)
        }
    }

    override fun onCreate() {
        super.onCreate()
        val source = DefaultMediaSourceFactory(this)
            .setDataSourceFactory(DefaultDataSource.Factory(this, DataSource.Factory { FileDataSource() }))
        val player = ExoPlayer.Builder(this).setMediaSourceFactory(source).build().apply {
            setAudioAttributes(AudioAttributes.Builder().setUsage(C.USAGE_MEDIA)
                .setContentType(C.AUDIO_CONTENT_TYPE_MUSIC).build(), true)
            setWakeMode(C.WAKE_MODE_LOCAL)
            setHandleAudioBecomingNoisy(true)
        }
        val open = PendingIntent.getActivity(this, 0, Intent(this, CardActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
        session = MediaSession.Builder(this, player).setId("card-player").setSessionActivity(open).build()
        RoomPlayback.pauseCardPlayback = { player.pause() }
        player.addListener(object : Player.Listener {
            override fun onPlayWhenReadyChanged(ready: Boolean, reason: Int) {
                if (ready) RoomPlayback.yieldToOtherPlayer(this@CardPlayback)
            }

            override fun onEvents(player: Player, events: Player.Events) {
                if (events.containsAny(Player.EVENT_MEDIA_ITEM_TRANSITION, Player.EVENT_MEDIA_METADATA_CHANGED,
                        Player.EVENT_IS_PLAYING_CHANGED, Player.EVENT_POSITION_DISCONTINUITY,
                        Player.EVENT_PLAYBACK_STATE_CHANGED)) {
                    handler.removeCallbacks(checkpoint)
                    checkpoint.run()
                }
            }
        })
    }

    override fun onGetSession(controllerInfo: MediaSession.ControllerInfo) = session

    private fun savePosition() = CardBookmark.save(this, session.player)

    override fun onDestroy() {
        handler.removeCallbacks(checkpoint)
        RoomPlayback.pauseCardPlayback = null
        savePosition()
        session.player.release()
        session.release()
        super.onDestroy()
    }
}
