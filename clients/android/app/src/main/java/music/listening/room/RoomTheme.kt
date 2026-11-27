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
    PAPER("paper", "Paper"),
    E_INK("e-ink", "E Ink");

    fun save(context: Context) {
        preferences(context).edit().putString("theme", key).apply()
    }

    fun applyWindow(activity: Activity, background: Int) {
        activity.setTheme(if (this == NIGHT) android.R.style.Theme_Material_NoActionBar
            else android.R.style.Theme_Material_Light_NoActionBar)
        activity.window.statusBarColor = background
        activity.window.navigationBarColor = if (this == E_INK) background else Color.BLACK
        val decor = activity.window.decorView
        val lightStatus = if (this == NIGHT) 0 else View.SYSTEM_UI_FLAG_LIGHT_STATUS_BAR
        val lightNavigation = if (this == E_INK) View.SYSTEM_UI_FLAG_LIGHT_NAVIGATION_BAR else 0
        val appearance = View.SYSTEM_UI_FLAG_LIGHT_STATUS_BAR or View.SYSTEM_UI_FLAG_LIGHT_NAVIGATION_BAR
        decor.systemUiVisibility = (decor.systemUiVisibility and appearance.inv()) or lightStatus or lightNavigation
    }

    companion object {
        val isKompakt get() = Build.MODEL.equals("Kompakt", ignoreCase = true)

        private fun preferences(context: Context) = context.getSharedPreferences("appearance", Context.MODE_PRIVATE)

        fun load(context: Context): RoomTheme {
            if (isKompakt) return E_INK
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
