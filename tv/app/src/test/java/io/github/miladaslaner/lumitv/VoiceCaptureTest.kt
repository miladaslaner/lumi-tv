package io.github.miladaslaner.lumitv

import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.MutableStateFlow
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class VoiceCaptureTest {
    private class Fake(override val available: Boolean = true) : VoiceCapture {
        val calls = mutableListOf<String>()
        var emit: (CaptureEvent) -> Unit = {}
        override fun start(askId: String, onEvent: (CaptureEvent) -> Unit) {
            calls += "start $askId"
            emit = onEvent
        }
        override fun stop() { calls += "stop" }
        override fun cancel() { calls += "cancel" }
    }

    private class Setup(mode: VoiceMode = VoiceMode.RECOGNIZER, granted: Boolean = true, onScreen: Boolean = true) {
        val recognizer = Fake()
        val mic = Fake()
        var mode = mode
        var granted = granted
        val screen = MutableStateFlow(onScreen)
        val events = mutableListOf<CaptureEvent>()
        val capture = TvVoiceCapture(
            mapOf(VoiceMode.RECOGNIZER to recognizer, VoiceMode.REMOTE_MIC to mic),
            mode = { this.mode }, micGranted = { this.granted }, onScreen = screen,
            scope = CoroutineScope(Dispatchers.Unconfined), screenWaitMs = 50,
        )
        fun start(id: String = "a1a1a1a1") = capture.start(id) { events += it }
    }

    @Test fun defaultModeFollowsTheServer() {
        // Unset: the remote's mic when the server hears speech (audio only to the PC), else the TV's recognizer.
        assertEquals(VoiceMode.REMOTE_MIC, VoiceMode.resolve(null, serverSpeech = true))
        assertEquals(VoiceMode.RECOGNIZER, VoiceMode.resolve(null, serverSpeech = false))
        // A bad stored value is as unset.
        assertNull(VoiceMode.parseOrNull("whisper"))
        assertEquals(VoiceMode.REMOTE_MIC, VoiceMode.resolve("whisper", serverSpeech = true))
        assertEquals(VoiceMode.RECOGNIZER, VoiceMode.resolve("whisper", serverSpeech = false))
    }

    @Test fun configuredModeWinsOverTheServer() {
        assertEquals(VoiceMode.RECOGNIZER, VoiceMode.resolve("recognizer", serverSpeech = true))
        assertEquals(VoiceMode.REMOTE_MIC, VoiceMode.resolve("remote_mic", serverSpeech = false))
        assertEquals(VoiceMode.OFF, VoiceMode.resolve("off", serverSpeech = true))
    }

    @Test fun modePicksTheCapture() {
        val s = Setup()
        s.start("a1a1a1a1")
        assertEquals(listOf("start a1a1a1a1"), s.recognizer.calls)
        s.mode = VoiceMode.REMOTE_MIC
        s.start("b2b2b2b2")
        assertEquals(listOf("start a1a1a1a1", "cancel"), s.recognizer.calls) // a new start ends the old capture
        assertEquals(listOf("start b2b2b2b2"), s.mic.calls)
    }

    @Test fun withoutRecordAudioNothingIsAvailable() {
        val s = Setup(granted = false)
        assertFalse(s.capture.available)
        s.start()
        assertEquals(listOf(CaptureEvent.Failed("permission")), s.events)
        assertTrue(s.recognizer.calls.isEmpty())
    }

    @Test fun offIsUnavailable() {
        val s = Setup(mode = VoiceMode.OFF)
        assertFalse(s.capture.available)
        s.start()
        assertTrue(s.recognizer.calls.isEmpty() && s.mic.calls.isEmpty())
        assertEquals(listOf(CaptureEvent.Failed("unavailable")), s.events) // not "permission": the mic is granted
    }

    @Test fun recordsOnlyOnceTheAnswerScreenIsInFront() {
        val s = Setup(onScreen = false)
        s.start()
        assertTrue(s.recognizer.calls.isEmpty())
        s.screen.value = true
        assertEquals(listOf("start a1a1a1a1"), s.recognizer.calls)
    }

    @Test fun screenThatNeverComesFailsTheCapture() {
        val s = Setup(onScreen = false)
        s.start()
        val deadline = System.currentTimeMillis() + 2_000
        while (s.events.isEmpty() && System.currentTimeMillis() < deadline) Thread.sleep(10)
        assertEquals(listOf(CaptureEvent.Failed("not on screen")), s.events)
        assertTrue(s.recognizer.calls.isEmpty())
    }

    @Test fun cancelWhileWaitingForTheScreenNeverRecords() {
        val s = Setup(onScreen = false)
        s.start()
        s.capture.cancel()
        s.screen.value = true
        Thread.sleep(100)
        assertTrue(s.recognizer.calls.isEmpty())
        assertTrue(s.events.isEmpty())
    }

    @Test fun screenGoingAwayCancelsTheCaptureInBothModes() {
        for (mode in listOf(VoiceMode.RECOGNIZER, VoiceMode.REMOTE_MIC)) {
            val s = Setup(mode = mode)
            val used = if (mode == VoiceMode.RECOGNIZER) s.recognizer else s.mic
            s.start()
            used.emit(CaptureEvent.Started)
            used.emit(CaptureEvent.Partial("something like"))
            s.screen.value = false // HOME, another app, or the screen went off
            assertEquals(mode.key, listOf("start a1a1a1a1", "cancel"), used.calls)
            assertEquals(
                mode.key,
                listOf(CaptureEvent.Started, CaptureEvent.Partial("something like"), CaptureEvent.Failed("not on screen")),
                s.events,
            )
        }
    }

    @Test fun nothingHeardBeforeTheScreenWentAwayIsSent() {
        val s = Setup(mode = VoiceMode.REMOTE_MIC)
        s.start()
        s.screen.value = false
        val wav = wav(ShortArray(16) { 1000 })
        s.mic.emit(CaptureEvent.Audio(wav, 5L)) // the recording thread finished just as the screen went away
        s.mic.emit(CaptureEvent.Text("something", 5L))
        assertEquals(listOf(CaptureEvent.Failed("not on screen")), s.events)
        assertTrue(wav.all { it == 0.toByte() }) // the dropped clip is zeroed
    }

    @Test fun eventsOfAnOldCaptureAreDropped() {
        val s = Setup()
        s.start("a1a1a1a1")
        val old = s.recognizer.emit
        s.start("b2b2b2b2")
        old(CaptureEvent.Text("late", 5L))
        assertTrue(s.events.isEmpty())
    }

    @Test fun anEndedCaptureIsNotCancelledByTheScreenLater() {
        val s = Setup()
        s.start()
        s.recognizer.emit(CaptureEvent.Text("comedy", 5L))
        s.screen.value = false
        assertEquals(listOf("start a1a1a1a1"), s.recognizer.calls)
        assertEquals(listOf<CaptureEvent>(CaptureEvent.Text("comedy", 5L)), s.events)
    }

    @Test fun voiceConfigChecksItsValues() {
        assertNull(VoiceConfig(mode = "remote_mic", micThreshold = 900).problem())
        assertNull(VoiceConfig(recognizer = DEFAULT_RECOGNIZER).problem())
        assertEquals("voice must be auto, recognizer, remote_mic or off", VoiceConfig(mode = "whisper").problem())
        assertNull(VoiceConfig(mode = "auto").problem())
        assertEquals("recognizer must be <package>/<class>", VoiceConfig(recognizer = "com.google.android.tts").problem())
        assertEquals("mic_threshold must be 1..32767", VoiceConfig(micThreshold = 0).problem())
        assertEquals("mic_source must be a MediaRecorder.AudioSource (0..10)", VoiceConfig(micSource = 99).problem())
        assertEquals("mic_device_type must be an AudioDeviceInfo type (0 = any)", VoiceConfig(micDeviceType = -1).problem())
        assertFalse(VoiceConfig().any)
        assertTrue(VoiceConfig(offline = false).any)
    }
}
