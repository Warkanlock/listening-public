package music.listening.room

import android.app.Activity
import android.app.AlertDialog
import android.content.Context

internal enum class MobileAudio(val wire: String, val label: String, val kbps: Int) {
    Original("original", "Original", 0),
    Opus("opus", "Compact Opus", 128),
    Aac("aac", "Compact AAC", 192);

    companion object {
        private fun preferences(context: Context) = context.getSharedPreferences("mobile_audio", Context.MODE_PRIVATE)

        fun load(context: Context): MobileAudio =
            entries.firstOrNull { it.wire == preferences(context).getString("format", null) } ?: Original

        fun choose(activity: Activity, selected: (MobileAudio) -> Unit) {
            AlertDialog.Builder(activity).setTitle("Mobile audio")
                .setSingleChoiceItems(entries.map { it.label }.toTypedArray(), load(activity).ordinal) { dialog, index ->
                    val profile = entries[index]
                    preferences(activity).edit().putString("format", profile.wire).apply()
                    dialog.dismiss()
                    selected(profile)
                }.setNegativeButton("Cancel", null).show()
        }
    }
}
