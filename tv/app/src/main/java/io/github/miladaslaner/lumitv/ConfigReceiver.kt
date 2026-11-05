package io.github.miladaslaner.lumitv

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.util.Log

/**
 * One-time setup over adb:
 * `adb shell am broadcast -n io.github.miladaslaner.lumitv/.ConfigReceiver -a io.github.miladaslaner.lumitv.CONFIGURE --es url ws://<PC>:8765/tv --es token <token>`
 * Voice settings (optional, alone or with the link; see [VoiceConfig]): `--es voice auto|recognizer|remote_mic|off`
 * (auto, the default: remote_mic when the server hears speech, else recognizer),
 * `--es recognizer <package>/<class>`, `--ez recognizer_offline true|false`, `--ei mic_source <AudioSource>`,
 * `--ei mic_device_type <AudioDeviceInfo type>`, `--ei mic_threshold <rms>`.
 * The broadcast must name the receiver (`-n`): implicit broadcasts to manifest receivers are dropped.
 * The manifest guards this receiver with the DUMP permission, so only adb/shell can send it.
 */
class ConfigReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        val url = intent.getStringExtra("url")
        val token = intent.getStringExtra("token")
        val voice = voiceConfig(intent)
        voice.problem()?.let {
            Log.w(TAG, "CONFIGURE $it")
            return
        }
        val link = url != null || token != null
        if (!link && !voice.any) {
            Log.w(TAG, "CONFIGURE needs --es url and --es token (or voice settings)")
            return
        }
        if (link && (url.isNullOrBlank() || token.isNullOrBlank())) {
            Log.w(TAG, "CONFIGURE needs --es url and --es token")
            return
        }
        if (link && !isWebSocketUrl(url!!)) {
            Log.w(TAG, "CONFIGURE url must start with ws:// or wss://")
            return
        }
        val prefs = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit()
        if (link) prefs.putString(PREF_URL, url).putString(PREF_TOKEN, token)
        voice.applyTo(prefs)
        prefs.apply()
        Log.i(TAG, if (voice.any) "configured (voice ${voice.mode ?: "unchanged"})" else "configured")
        if (!link) return
        if (!LinkService.reconfigure(context)) {
            Log.w(TAG, "could not start service from background (DENIED): run `adb shell appops set io.github.miladaslaner.lumitv SYSTEM_ALERT_WINDOW allow`, then re-send CONFIGURE or open the app once")
        }
    }

    private fun voiceConfig(i: Intent): VoiceConfig {
        fun int(key: String) = if (i.hasExtra(key)) i.getIntExtra(key, 0) else null
        return VoiceConfig(
            mode = i.getStringExtra("voice"),
            recognizer = i.getStringExtra("recognizer"),
            offline = if (i.hasExtra("recognizer_offline")) i.getBooleanExtra("recognizer_offline", true) else null,
            micSource = int("mic_source"),
            micDeviceType = int("mic_device_type"),
            micThreshold = int("mic_threshold"),
        )
    }
}
