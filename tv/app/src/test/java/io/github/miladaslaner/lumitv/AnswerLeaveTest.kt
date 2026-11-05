package io.github.miladaslaner.lumitv

import android.media.session.MediaSession
import android.os.PowerManager
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Before
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.Robolectric
import org.robolectric.RobolectricTestRunner
import org.robolectric.RuntimeEnvironment
import org.robolectric.Shadows.shadowOf
import org.robolectric.android.controller.ActivityController

/**
 * [AnswerActivity] wiring of a video a mic press paused: only BACK plays it again. Every other way the screen goes
 * away (HOME, another app, the screensaver's DreamActivity, the screen turning off) reaches the activity as onStop,
 * which ends the ask and leaves the video paused.
 */
@RunWith(RobolectricTestRunner::class)
class AnswerLeaveTest {
    private class FakeRuntime : AskRuntime {
        val resumed = mutableListOf<List<MediaSession.Token>>()
        var paused = 0
        override val voiceAvailable = false
        override fun startCapture(askId: String) = Unit
        override fun stopCapture() = Unit
        override suspend fun post(askId: String, text: String, source: String, answerId: String?, focusedToken: String?) =
            PostResult.Accepted
        override suspend fun postAudio(askId: String, wav: ByteArray, answerId: String?, focusedToken: String?) =
            PostResult.Accepted
        override fun send(json: String) = true
        override fun pausePlayback(): List<MediaSession.Token> = emptyList<MediaSession.Token>().also { paused++ }
        override fun resumePlayback(sessions: List<MediaSession.Token>) {
            resumed += sessions
        }
    }

    @get:Rule val lumi = LumiStateRule()

    private val rt = FakeRuntime()
    private lateinit var screen: ActivityController<AnswerActivity>

    @Before fun setUp() {
        AskController.install(rt)
        screen = Robolectric.buildActivity(AnswerActivity::class.java).setup()
        // A press while a video plays: paused, typing (no mic under Robolectric).
        AskController.dispatch(AskEvent.Press(voice = false, playing = true))
        assertEquals(1, rt.paused)
    }

    @After fun tearDown() {
        screen.destroy() // each test ends with its activity stopped; the rule clears the started-count behind it
    }

    @Test fun backResumesTheVideo() {
        screen.get().onBackPressedDispatcher.onBackPressed()
        screen.pause().stop() // finish() -> onStop sends Left, which must not resume a second time
        assertEquals(1, rt.resumed.size)
        assertEquals(AskState.Idle, AskController.state.value)
    }

    @Test fun homeOrAnotherAppOrScreensaverKeepsTheVideoPaused() {
        screen.pause().stop() // what HOME, another app and a DreamActivity covering Lumi all do to it
        assertEquals(0, rt.resumed.size)
        assertEquals(AskState.Idle, AskController.state.value)
        // Coming back to Lumi and pressing BACK later doesn't start it either.
        screen.start().resume()
        screen.get().onBackPressedDispatcher.onBackPressed()
        assertEquals(0, rt.resumed.size)
        screen.pause().stop()
    }

    @Test fun screenOffEndsTheAskAndKeepsTheVideoPaused() {
        val power = RuntimeEnvironment.getApplication().getSystemService(PowerManager::class.java)
        shadowOf(power).setIsInteractive(false)
        screen.pause().stop()
        assertEquals(0, rt.resumed.size)
        assertEquals(AskState.Idle, AskController.state.value)
    }
}
