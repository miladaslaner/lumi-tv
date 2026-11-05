package io.github.miladaslaner.lumitv

import android.content.Context
import android.content.Intent
import org.json.JSONObject
import org.robolectric.RuntimeEnvironment
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner

@RunWith(RobolectricTestRunner::class)
class ConfigReceiverTest {
    @get:Rule val lumi = LumiStateRule()

    private val context: Context = RuntimeEnvironment.getApplication()
    private val prefs get() = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)

    private fun configure(block: Intent.() -> Unit) =
        ConfigReceiver().onReceive(context, Intent("io.github.miladaslaner.lumitv.CONFIGURE").apply(block))

    @Test fun voiceSettingsAloneKeepTheLink() {
        prefs.edit().putString(PREF_URL, "ws://10.0.0.2:8765/tv").putString(PREF_TOKEN, "t").commit()
        configure {
            putExtra("voice", "remote_mic")
            putExtra("mic_threshold", 900)
            putExtra("recognizer_offline", false)
        }
        assertEquals("remote_mic", prefs.getString(PREF_VOICE, null))
        assertEquals(900, prefs.getInt(PREF_MIC_THRESHOLD, 0))
        assertFalse(prefs.getBoolean(PREF_RECOGNIZER_OFFLINE, true))
        assertEquals("ws://10.0.0.2:8765/tv", prefs.getString(PREF_URL, null))
        assertEquals("t", prefs.getString(PREF_TOKEN, null))
    }

    @Test fun voiceAutoForgetsTheStoredMode() {
        configure { putExtra("voice", "recognizer") }
        assertEquals("recognizer", prefs.getString(PREF_VOICE, null))
        configure { putExtra("voice", "auto") }
        assertFalse(prefs.contains(PREF_VOICE)) // follows the server's `hello` again
    }

    @Test fun pressUsesTheServersSpeechUnlessConfigured() {
        try {
            MessageBus.publish(ServerMsg.Hello("Lumi", JSONObject(), JSONObject(), speech = true))
            assertEquals(VoiceMode.REMOTE_MIC, voiceMode(prefs))
            MessageBus.publish(ServerMsg.Hello("Lumi", JSONObject(), JSONObject(), speech = false))
            assertEquals(VoiceMode.RECOGNIZER, voiceMode(prefs))
            MessageBus.publish(ServerMsg.Hello("Lumi", JSONObject(), JSONObject(), speech = true))
            configure { putExtra("voice", "recognizer") }
            assertEquals(VoiceMode.RECOGNIZER, voiceMode(prefs)) // CONFIGURE wins
        } finally {
            MessageBus.reset()
        }
        prefs.edit().remove(PREF_VOICE).commit()
        assertEquals(VoiceMode.RECOGNIZER, voiceMode(prefs)) // not connected: no server to hear speech
    }

    @Test fun badVoiceValueChangesNothing() {
        configure {
            putExtra("voice", "whisper")
            putExtra("mic_threshold", 900)
        }
        assertNull(prefs.getString(PREF_VOICE, null))
        assertFalse(prefs.contains(PREF_MIC_THRESHOLD))
    }

    @Test fun recognizerIsStoredByName() {
        configure { putExtra("recognizer", DEFAULT_RECOGNIZER) }
        assertEquals(DEFAULT_RECOGNIZER, prefs.getString(PREF_RECOGNIZER, null))
    }

    @Test fun nothingGivenStoresNothing() {
        configure { }
        assertEquals(emptyMap<String, Any?>(), prefs.all)
    }
}
