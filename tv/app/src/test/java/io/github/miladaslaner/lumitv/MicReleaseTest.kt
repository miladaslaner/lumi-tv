package io.github.miladaslaner.lumitv

import android.Manifest
import android.os.Bundle
import android.speech.SpeechRecognizer
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.MutableStateFlow
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.RuntimeEnvironment
import org.robolectric.Shadows.shadowOf
import org.robolectric.shadows.ShadowAudioRecord
import org.robolectric.shadows.ShadowLooper
import org.robolectric.shadows.ShadowSpeechRecognizer
import java.util.Collections
import java.util.concurrent.atomic.AtomicInteger

/** The real captures behind [TvVoiceCapture]: the mic is released the moment Lumi's screen goes away. */
@RunWith(RobolectricTestRunner::class)
class MicReleaseTest {
    private val app = RuntimeEnvironment.getApplication()
    private val screen = MutableStateFlow(true)
    private val events: MutableList<CaptureEvent> = Collections.synchronizedList(mutableListOf())

    @After fun tearDown() = ShadowAudioRecord.clearSource()

    /** [real], said to be available (the TV's recognizer isn't installed under Robolectric). */
    private fun gated(mode: VoiceMode, real: VoiceCapture) = TvVoiceCapture(
        mapOf(mode to object : VoiceCapture by real { override val available = true }),
        mode = { mode }, micGranted = { true }, onScreen = screen, scope = CoroutineScope(Dispatchers.Unconfined),
    )

    @Test fun recognizerIsCancelledAndDestroyedWhenTheScreenGoesAway() {
        val capture = gated(VoiceMode.RECOGNIZER, RecognizerCapture(app, { DEFAULT_RECOGNIZER }, { true }))
        capture.start("a1a1a1a1") { events += it }
        ShadowLooper.idleMainLooper() // the recognizer binds and starts listening on the main looper
        val recognizer = shadowOf(ShadowSpeechRecognizer.getLatestSpeechRecognizer())
        recognizer.triggerOnReadyForSpeech(Bundle())
        screen.value = false
        assertTrue(recognizer.isDestroyed)
        // Anything the recognizer still reports is never sent.
        recognizer.triggerOnEndOfSpeech()
        recognizer.triggerOnResults(Bundle().apply {
            putStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION, arrayListOf("something like the expanse"))
        })
        assertEquals(listOf(CaptureEvent.Started, CaptureEvent.Failed("not on screen")), events.toList())
    }

    @Test fun recognizerResultWhileOnScreenIsDelivered() {
        val capture = gated(VoiceMode.RECOGNIZER, RecognizerCapture(app, { DEFAULT_RECOGNIZER }, { true }, clock = { 42L }))
        capture.start("a1a1a1a1") { events += it }
        ShadowLooper.idleMainLooper() // the recognizer binds and starts listening on the main looper
        val recognizer = shadowOf(ShadowSpeechRecognizer.getLatestSpeechRecognizer())
        recognizer.triggerOnEndOfSpeech()
        recognizer.triggerOnResults(Bundle().apply {
            putStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION, arrayListOf("comedy"))
        })
        ShadowLooper.idleMainLooper()
        assertEquals(listOf<CaptureEvent>(CaptureEvent.Text("comedy", 42L)), events.toList())
        assertTrue(recognizer.isDestroyed) // released once it ended
    }

    @Test fun remoteMicStopsRecordingWhenTheScreenGoesAway() {
        shadowOf(app).grantPermissions(Manifest.permission.RECORD_AUDIO)
        val reads = AtomicInteger()
        // Someone talking without pause, delivered in real time (20 ms per frame).
        val source = object : ShadowAudioRecord.AudioRecordSource {
            override fun readInShortArray(a: ShortArray, offset: Int, size: Int, isBlocking: Boolean): Int {
                Thread.sleep(20)
                reads.incrementAndGet()
                for (i in offset until offset + size) a[i] = if (i % 2 == 0) 3000 else -3000
                return size
            }
        }
        ShadowAudioRecord.setSourceProvider { source }
        val mic = RemoteMicCapture(app, { MicSettings(DEFAULT_MIC_SOURCE, 0, micThreshold(300)) })
        val capture = gated(VoiceMode.REMOTE_MIC, mic)
        capture.start("a1a1a1a1") { events += it }
        waitUntil { reads.get() >= 10 } // 200 ms of speech recorded
        screen.value = false
        val after = reads.get()
        Thread.sleep(200)
        assertTrue("still reading the mic: ${reads.get() - after} more frames", reads.get() - after <= 1)
        assertEquals(listOf(CaptureEvent.Started, CaptureEvent.Failed("not on screen")), events.toList())
    }

    private fun waitUntil(cond: () -> Boolean) {
        val deadline = System.currentTimeMillis() + 3_000
        while (!cond() && System.currentTimeMillis() < deadline) Thread.sleep(5)
        assertTrue(cond())
    }
}
