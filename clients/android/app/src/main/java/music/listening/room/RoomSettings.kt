package music.listening.room

internal enum class RoomSetting(val title: String, val detail: String) {
    APPEARANCE("Appearance", "Color and E Ink themes"),
    UPDATES("App updates", "Check for a newer APK"),
    PLAYBACK("Playback", "Audio quality, buffering, and outputs"),
    OFFLINE("Offline library", "Sync and play music on this phone"),
    CONNECTION("Room connection", "Server address and device access"),
    EXTENSIONS("Extensions", "Download sources and health"),
    INSIGHTS("Playback insights", "Traffic, queue, and buffer"),
}
