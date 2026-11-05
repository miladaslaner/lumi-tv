package io.github.miladaslaner.lumitv

import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.speech.RecognitionListener
import android.speech.RecognitionService
import android.speech.RecognizerIntent
import android.speech.SpeechRecognizer
import android.util.Log

/**
 * The recognizer Lumi binds when none is configured: Google's speech services, which is what a Google TV's
 * `voice_recognition_service` setting names before Lumi takes the assistant role. Always named explicitly, never the
 * system default: once Lumi is the assistant the default can become Lumi's own stub.
 */
const val DEFAULT_RECOGNIZER =
    "com.google.android.tts/com.google.android.apps.speech.tts.googletts.service.GoogleTTSRecognitionService"

/** Longest a question may be listened to (the recognizer is told to stop then and returns what it has). */
private const val MAX_LISTEN_MS = 15_000L

/** A recognizer that has not ended this long after the start is given up on (bind refused, service hung). */
private const val GIVE_UP_MS = MAX_LISTEN_MS + 3_000L

/** One English question, free form, with live partial results; on-device when [preferOffline] and available. */
fun recognizerIntent(preferOffline: Boolean): Intent = Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH)
    .putExtra(RecognizerIntent.EXTRA_LANGUAGE_MODEL, RecognizerIntent.LANGUAGE_MODEL_FREE_FORM)
    .putExtra(RecognizerIntent.EXTRA_LANGUAGE, "en-US")
    .putExtra(RecognizerIntent.EXTRA_PARTIAL_RESULTS, true)
    .putExtra(RecognizerIntent.EXTRA_MAX_RESULTS, 1)
    .putExtra(RecognizerIntent.EXTRA_PREFER_OFFLINE, preferOffline)

/** A recognizer error as the capture's end: silence is "nothing heard", the rest is a failure with its reason. */
fun mapRecognizerError(code: Int): CaptureEvent = when (code) {
    SpeechRecognizer.ERROR_NO_MATCH, SpeechRecognizer.ERROR_SPEECH_TIMEOUT -> CaptureEvent.NothingHeard
    SpeechRecognizer.ERROR_INSUFFICIENT_PERMISSIONS -> CaptureEvent.Failed("permission")
    SpeechRecognizer.ERROR_NETWORK, SpeechRecognizer.ERROR_NETWORK_TIMEOUT, SpeechRecognizer.ERROR_SERVER ->
        CaptureEvent.Failed("network")
    else -> CaptureEvent.Failed("recognizer")
}

/**
 * One recognition turned into capture events (pure; the [RecognitionListener] in [RecognizerCapture] feeds it).
 * The question's end time is the recognizer's end of speech, not the later final result. Only the first end counts.
 */
class RecognitionEvents(private val clock: () -> Long, private val onEvent: (CaptureEvent) -> Unit) {
    private var endAt: Long? = null

    var done = false
        private set

    fun ready() = emitUnlessDone(CaptureEvent.Started)

    fun partial(text: String?) {
        if (!text.isNullOrBlank()) emitUnlessDone(CaptureEvent.Partial(text.trim()))
    }

    fun endOfSpeech() {
        if (endAt == null) endAt = clock()
    }

    fun results(text: String?) =
        finish(if (text.isNullOrBlank()) CaptureEvent.NothingHeard else CaptureEvent.Text(text.trim(), endAt ?: clock()))

    fun error(code: Int) = finish(mapRecognizerError(code))

    /** The recognizer never ended: fail instead of listening forever. */
    fun giveUp() = finish(CaptureEvent.Failed("recognizer"))

    private fun finish(e: CaptureEvent) {
        if (done) return
        emitUnlessDone(e)
        done = true
    }

    private fun emitUnlessDone(e: CaptureEvent) {
        if (!done) onEvent(e)
    }
}

/**
 * Path B: the TV's own speech recognizer, bound explicitly by [component] (`package/class`, see [DEFAULT_RECOGNIZER]).
 * Partial results show live on the listening screen; the final text is sent like a typed question with
 * `source = "voice"`. The recognizer records and may send audio to its provider; Lumi itself keeps no audio.
 * Main thread only (`SpeechRecognizer` requires it).
 */
class RecognizerCapture(
    context: Context,
    private val component: () -> String,
    private val preferOffline: () -> Boolean,
    private val clock: () -> Long = MessageBus::nowMs,
) : VoiceCapture {
    private val app = context.applicationContext
    private val main = Handler(Looper.getMainLooper())
    private var recognizer: SpeechRecognizer? = null
    private var session: RecognitionEvents? = null

    override val available: Boolean
        get() {
            val cn = ComponentName.unflattenFromString(component()) ?: return false
            val bound = Intent(RecognitionService.SERVICE_INTERFACE).setComponent(cn)
            return SpeechRecognizer.isRecognitionAvailable(app) && app.packageManager.queryIntentServices(bound, 0).isNotEmpty()
        }

    override fun start(askId: String, onEvent: (CaptureEvent) -> Unit) {
        cancel()
        val cn = ComponentName.unflattenFromString(component()) ?: return onEvent(CaptureEvent.Failed("recognizer"))
        val r = try {
            SpeechRecognizer.createSpeechRecognizer(app, cn)
        } catch (e: RuntimeException) {
            Log.w(TAG, "ask $askId recognizer: ${e.javaClass.simpleName}")
            return onEvent(CaptureEvent.Failed("recognizer"))
        }
        val events = RecognitionEvents(clock) { e ->
            if (e is CaptureEvent.Text || e is CaptureEvent.NothingHeard || e is CaptureEvent.Failed) {
                Log.i(TAG, "ask $askId recognizer: ${e.javaClass.simpleName}")
            }
            onEvent(e)
        }
        recognizer = r
        session = events
        // Destroyed after its callback returns, unless a newer capture already replaced it.
        r.setRecognitionListener(Listener(events) { main.post { if (session === events) release() } })
        r.startListening(recognizerIntent(preferOffline()))
        main.postDelayed({ if (session === events) r.stopListening() }, MAX_LISTEN_MS)
        main.postDelayed({
            if (session === events) {
                events.giveUp()
                cancel()
            }
        }, GIVE_UP_MS)
    }

    override fun stop() {
        recognizer?.stopListening()
    }

    override fun cancel() {
        val r = recognizer ?: return
        r.cancel()
        release()
    }

    private fun release() {
        main.removeCallbacksAndMessages(null)
        recognizer?.destroy()
        recognizer = null
        session = null
    }

    /** Hands the recognizer's callbacks (main thread) to [events]; [ended] once it has a result or an error. */
    private class Listener(private val events: RecognitionEvents, private val ended: () -> Unit) : RecognitionListener {
        override fun onReadyForSpeech(params: Bundle?) = events.ready()
        override fun onBeginningOfSpeech() = Unit
        override fun onRmsChanged(rmsdB: Float) = Unit
        override fun onBufferReceived(buffer: ByteArray?) = Unit
        override fun onEndOfSpeech() = events.endOfSpeech()
        override fun onPartialResults(partialResults: Bundle?) = events.partial(first(partialResults))
        override fun onEvent(eventType: Int, params: Bundle?) = Unit

        override fun onResults(results: Bundle?) {
            events.results(first(results))
            ended()
        }

        override fun onError(error: Int) {
            events.error(error)
            ended()
        }

        private fun first(b: Bundle?): String? = b?.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION)?.firstOrNull()
    }
}
