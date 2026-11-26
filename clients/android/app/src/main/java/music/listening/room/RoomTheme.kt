package music.listening.room

import android.app.Activity
import android.content.Context
import android.graphics.Color
import android.os.Build
import android.view.View
import org.json.JSONObject

internal enum class RoomTheme(val key: String, val label: String) {
    SEA_GLASS("sea-glass", "Sea Glass"),
    NIGHT("night", "Night"),
    PAPER("paper", "Paper");

    fun save(context: Context) {
        preferences(context).edit().putString("theme", key).apply()
    }

    fun applyWindow(activity: Activity, background: Int) {
        activity.setTheme(if (this == NIGHT) android.R.style.Theme_Material_NoActionBar
            else android.R.style.Theme_Material_Light_NoActionBar)
        activity.window.statusBarColor = background
        val decor = activity.window.decorView
        val flags = decor.systemUiVisibility
        decor.systemUiVisibility = if (this == NIGHT) flags and View.SYSTEM_UI_FLAG_LIGHT_STATUS_BAR.inv()
            else flags or View.SYSTEM_UI_FLAG_LIGHT_STATUS_BAR
    }

    companion object {
        val isKompakt get() = Build.MODEL.equals("Kompakt", ignoreCase = true)

        private fun preferences(context: Context) = context.getSharedPreferences("appearance", Context.MODE_PRIVATE)

        fun load(context: Context): RoomTheme {
            if (isKompakt) return PAPER
            val saved = preferences(context).getString("theme", null)
            return entries.firstOrNull { it.key == saved } ?: SEA_GLASS
        }
    }
}

internal object RoomPalettes {
    private var data: JSONObject? = null

    @Synchronized
    fun load(context: Context, theme: RoomTheme): JSONObject {
        val palettes = data ?: context.assets.open("room-palettes.json").bufferedReader().use {
            JSONObject(it.readText())
        }.also { data = it }
        return palettes.getJSONObject(theme.key)
    }

    fun color(palette: JSONObject, token: String) = Color.parseColor(palette.getString(token))
}
