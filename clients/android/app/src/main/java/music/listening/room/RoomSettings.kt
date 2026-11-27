package music.listening.room

import android.view.View

internal enum class RoomSetting(val title: String) {
    APPEARANCE("Appearance"),
    PLAYBACK("Playback"),
    OFFLINE("Offline library"),
    CONNECTION("Room connection"),
    EXTENSIONS("Extensions"),
    INSIGHTS("Playback insights"),
    UPDATES("App updates"),
}

internal fun RoomUi.settingsIndex(open: (RoomSetting) -> Unit): View =
    index(RoomSetting.entries.map { setting -> Entry(setting.title) { open(setting) } })
