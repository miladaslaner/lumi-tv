package io.github.miladaslaner.lumitv

import android.Manifest
import android.content.Context
import android.content.SharedPreferences
import android.content.pm.PackageManager
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.launch
import kotlinx.coroutines.withTimeoutOrNull

/**
 * Records one spoken question after a mic press. Runs only while Lumi's answer screen is in front (while-in-use mic
 * access; [LinkService] never records). Audio stays in memory and is never logged.
 */
interface VoiceCapture {
    /** Capture can run on this TV; without it a press goes straight to typing. */
    val available: Boolean

    /** Starts listening for ask [askId]; [onEvent] may be called on any thread. */
    fun start(askId: String, onEvent: (CaptureEvent) -> Unit)

    /** Ends listening now and delivers what was heard. */
    fun stop()

    /** Ends listening and drops what was captured (the ask was left or replaced). */
    fun cancel()
}

sealed interface CaptureEvent {
    data object Started : CaptureEvent
    data class Partial(val text: String) : CaptureEvent
    /** The speech, as WAV; [endAtMs] is the end of speech on the [MessageBus.nowMs] clock. */
    class Audio(val wav: ByteArray, val endAtMs: Long) : CaptureEvent
    /** The speech, already transcribed on the TV; [endAtMs] as in [Audio]. */
    data class Text(val text: String, val endAtMs: Long) : CaptureEvent
    data object NothingHeard : CaptureEvent
    data class Failed(val reason: String) : CaptureEvent
}

/** No capture on this TV (yet): every press goes to typing. */
object NoCapture : VoiceCapture {
    override val available = false
    override fun start(askId: String, onEvent: (CaptureEvent) -> Unit) = onEvent(CaptureEvent.Failed("no capture"))
    override fun stop() = Unit
    override fun cancel() = Unit
}

/**
 * The ask event a capture event becomes; null for [CaptureEvent.Text] / [CaptureEvent.Audio], which end the capture
 * and are sent by [AskController.onCapture].
 */
fun askEventFor(event: CaptureEvent): AskEvent? = when (event) {
    CaptureEvent.Started -> AskEvent.CaptureStarted
    is CaptureEvent.Partial -> AskEvent.Partial(event.text)
    CaptureEvent.NothingHeard -> AskEvent.NothingHeard
    is CaptureEvent.Failed -> AskEvent.CaptureFailed(event.reason)
    is CaptureEvent.Text, is CaptureEvent.Audio -> null
}

/**
 * Which capture a mic press uses. Unless set with `CONFIGURE --es voice …`, it follows the server ([resolve]): the
 * remote's mic when the server says in `hello` that it hears speech (audio goes only to the PC, the first choice),
 * otherwise the TV's speech recognizer.
 */
enum class VoiceMode(val key: String) {
    /** Path B: the TV's speech recognizer ([RecognizerCapture]); the default when the server can't hear speech. */
    RECOGNIZER("recognizer"),

    /** Path A: the remote's mic as audio for the server's speech-to-text ([RemoteMicCapture]). */
    REMOTE_MIC("remote_mic"),

    /** No voice: every press types. */
    OFF("off");

    companion object {
        /** `CONFIGURE --es voice auto`: forget the stored mode, so it follows the server again. */
        const val AUTO = "auto"

        fun parseOrNull(key: String?): VoiceMode? = entries.firstOrNull { it.key == key }

        /**
         * The mode for a stored value [key] (set by CONFIGURE, it wins) and whether the server hears speech
         * ([serverSpeech], from `hello`; false while not connected or from an older server). Unset or unknown follows
         * the server.
         */
        fun resolve(key: String?, serverSpeech: Boolean): VoiceMode =
            parseOrNull(key) ?: if (serverSpeech) REMOTE_MIC else RECOGNIZER
    }
}

// Voice settings in the app's prefs ([PREFS]), set by the CONFIGURE broadcast.
const val PREF_VOICE = "voice"
const val PREF_RECOGNIZER = "recognizer"
const val PREF_RECOGNIZER_OFFLINE = "recognizer_offline"
const val PREF_MIC_SOURCE = "mic_source"
const val PREF_MIC_DEVICE_TYPE = "mic_device_type"
const val PREF_MIC_THRESHOLD = "mic_threshold"

private val COMPONENT = Regex("""^[A-Za-z_][\w.]*/[A-Za-z_.$][\w.$]*$""")

/** The voice settings a CONFIGURE broadcast carries; null = not given (left as stored). */
data class VoiceConfig(
    val mode: String? = null,
    val recognizer: String? = null,
    val offline: Boolean? = null,
    val micSource: Int? = null,
    val micDeviceType: Int? = null,
    val micThreshold: Int? = null,
) {
    val any: Boolean get() = listOf(mode, recognizer, offline, micSource, micDeviceType, micThreshold).any { it != null }

    /** What is wrong with these values, or null. */
    fun problem(): String? = when {
        mode != null && mode != VoiceMode.AUTO && VoiceMode.parseOrNull(mode) == null ->
            "voice must be auto, recognizer, remote_mic or off"
        recognizer != null && !COMPONENT.matches(recognizer) -> "recognizer must be <package>/<class>"
        micThreshold != null && micThreshold !in 1..Short.MAX_VALUE -> "mic_threshold must be 1..32767"
        micSource != null && micSource !in 0..10 -> "mic_source must be a MediaRecorder.AudioSource (0..10)"
        micDeviceType != null && micDeviceType < 0 -> "mic_device_type must be an AudioDeviceInfo type (0 = any)"
        else -> null
    }

    fun applyTo(prefs: SharedPreferences.Editor) {
        mode?.let { if (it == VoiceMode.AUTO) prefs.remove(PREF_VOICE) else prefs.putString(PREF_VOICE, it) }
        recognizer?.let { prefs.putString(PREF_RECOGNIZER, it) }
        offline?.let { prefs.putBoolean(PREF_RECOGNIZER_OFFLINE, it) }
        micSource?.let { prefs.putInt(PREF_MIC_SOURCE, it) }
        micDeviceType?.let { prefs.putInt(PREF_MIC_DEVICE_TYPE, it) }
        micThreshold?.let { prefs.putInt(PREF_MIC_THRESHOLD, it) }
    }
}

/**
 * The capture a press uses on the TV: the configured [mode]'s ([captures]), only with the RECORD_AUDIO grant
 * ([micGranted]; without it every press types). The mic is open only while Lumi's answer screen is in front
 * ([onScreen], while-in-use mic access):
 * - a press from another app dispatches before the screen is up, so the start waits for it (at most [screenWaitMs],
 *   else the capture fails);
 * - the screen going away (HOME, another app, screen off: all stop the activity) cancels the capture at once
 *   (recognizer cancelled and destroyed, `AudioRecord` stopped and released) and fails it as "not on screen";
 * - a capture's events are handed on only on the main thread, while it is still the current capture and the screen
 *   is still in front, so nothing heard before the screen went away is sent (a dropped WAV is zeroed).
 * Main thread only.
 */
class TvVoiceCapture(
    private val captures: Map<VoiceMode, VoiceCapture>,
    private val mode: () -> VoiceMode,
    private val micGranted: () -> Boolean,
    private val onScreen: StateFlow<Boolean>,
    private val scope: CoroutineScope,
    private val screenWaitMs: Long = 3_000,
) : VoiceCapture {
    private var active: VoiceCapture? = null
    private var waiting: Job? = null
    private var watching: Job? = null
    /** Identifies the current capture; events of any other are dropped. */
    private var session: Any? = null

    private fun selected(): VoiceCapture = if (micGranted()) captures[mode()] ?: NoCapture else NoCapture

    override val available: Boolean get() = selected().available

    override fun start(askId: String, onEvent: (CaptureEvent) -> Unit) {
        cancel()
        val capture = selected()
        if (!capture.available) return onEvent(CaptureEvent.Failed(if (micGranted()) "unavailable" else "permission"))
        val token = Any()
        session = token
        val deliver: (CaptureEvent) -> Unit = { e ->
            scope.launch {
                if (session === token && onScreen.value) {
                    if (e.ends) endSession()
                    onEvent(e)
                } else if (e is CaptureEvent.Audio) {
                    e.wav.fill(0)
                }
            }
        }
        fun begin() {
            active = capture
            watching = scope.launch {
                onScreen.first { !it }
                if (session !== token) return@launch
                cancel()
                onEvent(CaptureEvent.Failed("not on screen"))
            }
            capture.start(askId, deliver)
        }
        if (onScreen.value) return begin()
        waiting = scope.launch {
            val shown = withTimeoutOrNull(screenWaitMs) { onScreen.first { it } }
            waiting = null
            if (session !== token) return@launch
            if (shown == null) {
                endSession()
                onEvent(CaptureEvent.Failed("not on screen"))
            } else begin()
        }
    }

    override fun stop() {
        active?.stop()
    }

    override fun cancel() {
        val capture = active
        endSession()
        capture?.cancel()
    }

    /** The capture ended (or is being cancelled): stop waiting and watching; its later events are dropped. */
    private fun endSession() {
        session = null
        waiting?.cancel()
        waiting = null
        watching?.cancel()
        watching = null
        active = null
    }
}

/** The capture is over after this event. */
private val CaptureEvent.ends: Boolean
    get() = this is CaptureEvent.Text || this is CaptureEvent.Audio || this is CaptureEvent.NothingHeard ||
        this is CaptureEvent.Failed

/** The mode a press uses now: CONFIGURE's `voice` if set, else what the connected server's `hello` allows. */
fun voiceMode(prefs: SharedPreferences): VoiceMode =
    VoiceMode.resolve(prefs.getString(PREF_VOICE, null), MessageBus.hello.value?.speech == true)

/** The TV's capture, configured from the app's prefs. */
fun tvVoiceCapture(context: Context): TvVoiceCapture {
    val app = context.applicationContext
    val prefs = app.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
    val recognizer = RecognizerCapture(
        app,
        component = { prefs.getString(PREF_RECOGNIZER, null) ?: DEFAULT_RECOGNIZER },
        preferOffline = { prefs.getBoolean(PREF_RECOGNIZER_OFFLINE, true) },
    )
    val mic = RemoteMicCapture(app, settings = {
        MicSettings(
            source = prefs.getInt(PREF_MIC_SOURCE, DEFAULT_MIC_SOURCE),
            deviceType = prefs.getInt(PREF_MIC_DEVICE_TYPE, 0),
            threshold = micThreshold(prefs.getInt(PREF_MIC_THRESHOLD, DEFAULT_MIC_THRESHOLD)),
            maxMs = MessageBus.limits.audioMs,
        )
    })
    return TvVoiceCapture(
        mapOf(VoiceMode.RECOGNIZER to recognizer, VoiceMode.REMOTE_MIC to mic),
        mode = { voiceMode(prefs) },
        micGranted = { app.checkSelfPermission(Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED },
        onScreen = AnswerActivity.onScreen,
        scope = CoroutineScope(SupervisorJob() + Dispatchers.Main.immediate),
    )
}
