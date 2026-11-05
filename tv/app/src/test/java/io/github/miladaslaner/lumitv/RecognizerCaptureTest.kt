package io.github.miladaslaner.lumitv

import android.speech.RecognizerIntent
import android.speech.SpeechRecognizer
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner

@RunWith(RobolectricTestRunner::class)
class RecognizerCaptureTest {
    @Test fun mapsEveryRecognizerError() {
        assertEquals(CaptureEvent.NothingHeard, mapRecognizerError(SpeechRecognizer.ERROR_NO_MATCH))
        assertEquals(CaptureEvent.NothingHeard, mapRecognizerError(SpeechRecognizer.ERROR_SPEECH_TIMEOUT))
        assertEquals(CaptureEvent.Failed("permission"), mapRecognizerError(SpeechRecognizer.ERROR_INSUFFICIENT_PERMISSIONS))
        assertEquals(CaptureEvent.Failed("network"), mapRecognizerError(SpeechRecognizer.ERROR_NETWORK))
        assertEquals(CaptureEvent.Failed("network"), mapRecognizerError(SpeechRecognizer.ERROR_NETWORK_TIMEOUT))
        assertEquals(CaptureEvent.Failed("network"), mapRecognizerError(SpeechRecognizer.ERROR_SERVER))
        assertEquals(CaptureEvent.Failed("recognizer"), mapRecognizerError(SpeechRecognizer.ERROR_CLIENT))
        assertEquals(CaptureEvent.Failed("recognizer"), mapRecognizerError(SpeechRecognizer.ERROR_AUDIO))
        assertEquals(CaptureEvent.Failed("recognizer"), mapRecognizerError(SpeechRecognizer.ERROR_RECOGNIZER_BUSY))
        assertEquals(CaptureEvent.Failed("recognizer"), mapRecognizerError(12345))
    }

    @Test fun recognizerIntentExtrasExact() {
        val i = recognizerIntent(preferOffline = true)
        assertEquals(RecognizerIntent.ACTION_RECOGNIZE_SPEECH, i.action)
        assertEquals(
            setOf(
                RecognizerIntent.EXTRA_LANGUAGE_MODEL, RecognizerIntent.EXTRA_LANGUAGE, RecognizerIntent.EXTRA_PARTIAL_RESULTS,
                RecognizerIntent.EXTRA_MAX_RESULTS, RecognizerIntent.EXTRA_PREFER_OFFLINE,
            ),
            i.extras!!.keySet(),
        )
        assertEquals(RecognizerIntent.LANGUAGE_MODEL_FREE_FORM, i.getStringExtra(RecognizerIntent.EXTRA_LANGUAGE_MODEL))
        assertEquals("en-US", i.getStringExtra(RecognizerIntent.EXTRA_LANGUAGE))
        assertTrue(i.getBooleanExtra(RecognizerIntent.EXTRA_PARTIAL_RESULTS, false))
        assertEquals(1, i.getIntExtra(RecognizerIntent.EXTRA_MAX_RESULTS, 0))
        assertTrue(i.getBooleanExtra(RecognizerIntent.EXTRA_PREFER_OFFLINE, false))
        assertEquals(false, recognizerIntent(preferOffline = false).getBooleanExtra(RecognizerIntent.EXTRA_PREFER_OFFLINE, true))
    }

    @Test fun defaultRecognizerIsGoogleTtsNeverTheSystemDefault() {
        assertEquals(
            "com.google.android.tts/com.google.android.apps.speech.tts.googletts.service.GoogleTTSRecognitionService",
            DEFAULT_RECOGNIZER,
        )
    }

    private class Driver {
        var now = 1_000L
        val events = mutableListOf<CaptureEvent>()
        val session = RecognitionEvents(clock = { now }) { events += it }
    }

    @Test fun captureEndsWithTextAndTheOnEndOfSpeechTime() {
        val d = Driver()
        d.session.ready()
        d.now = 1_500L
        d.session.partial("something like")
        d.session.partial("something like the expanse")
        d.now = 3_200L
        d.session.endOfSpeech()
        d.now = 3_900L // the final result comes later than the end of speech
        d.session.results(" something like the expanse ")
        assertEquals(
            listOf(
                CaptureEvent.Started,
                CaptureEvent.Partial("something like"),
                CaptureEvent.Partial("something like the expanse"),
                CaptureEvent.Text("something like the expanse", 3_200L),
            ),
            d.events,
        )
        assertTrue(d.session.done)
    }

    @Test fun blankResultIsNothingHeard() {
        val d = Driver()
        d.session.ready()
        d.session.partial("") // blank partials are not shown
        d.session.results("  ")
        assertEquals(listOf(CaptureEvent.Started, CaptureEvent.NothingHeard), d.events)
    }

    @Test fun errorEndsTheCaptureOnce() {
        val d = Driver()
        d.session.ready()
        d.session.error(SpeechRecognizer.ERROR_SPEECH_TIMEOUT)
        d.session.results("late") // a recognizer that reports twice: only the first end counts
        d.session.error(SpeechRecognizer.ERROR_CLIENT)
        assertEquals(listOf(CaptureEvent.Started, CaptureEvent.NothingHeard), d.events)
    }

    @Test fun aRecognizerThatNeverEndsIsGivenUpOn() {
        val d = Driver()
        d.session.ready()
        d.session.giveUp()
        d.session.results("late")
        assertEquals(listOf(CaptureEvent.Started, CaptureEvent.Failed("recognizer")), d.events)
    }

    @Test fun resultWithoutEndOfSpeechUsesNow() {
        val d = Driver()
        d.now = 7_000L
        d.session.results("comedy")
        assertEquals(listOf(CaptureEvent.Text("comedy", 7_000L)), d.events)
    }
}
