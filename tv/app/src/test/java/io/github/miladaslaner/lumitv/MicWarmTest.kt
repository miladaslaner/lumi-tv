package io.github.miladaslaner.lumitv

import android.media.session.MediaSession
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner

/** The mic opening tells the server to page its speech model back in while the user is still talking. */
@RunWith(RobolectricTestRunner::class)
class MicWarmTest {
    /** Records what the controller asks of the runtime, in order. */
    private class FakeRuntime : AskRuntime {
        val calls = mutableListOf<String>()
        val sent = mutableListOf<String>()
        override val voiceAvailable = true
        override fun startCapture(askId: String) { calls += "capture $askId" }
        override fun stopCapture() = Unit
        override suspend fun post(askId: String, text: String, source: String, answerId: String?, focusedToken: String?) =
            PostResult.Accepted
        override suspend fun postAudio(askId: String, wav: ByteArray, answerId: String?, focusedToken: String?) =
            PostResult.Accepted
        override fun send(json: String) = true.also { sent += json; calls += "send " + JSONObject(json).getString("type") }
        override fun pausePlayback(): List<MediaSession.Token> = emptyList()
        override fun resumePlayback(sessions: List<MediaSession.Token>) = Unit
    }

    @get:Rule val lumi = LumiStateRule()

    @Test fun aMicPressSendsAskWarmForThatAskBeforeTheMicOpens() {
        val rt = FakeRuntime()
        AskController.install(rt)
        AskController.dispatch(AskEvent.Press(voice = true, playing = false))
        val id = AskController.state.value.askId
        val warms = rt.sent.map(::JSONObject).filter { it.getString("type") == "ask_warm" }
        assertEquals(listOf(id), warms.map { it.getString("ask_id") })
        // the warm-up goes out first, so it overlaps the user's speech instead of following it
        assertEquals(listOf("send ask_warm", "capture $id"), rt.calls)
    }

    @Test fun typingSendsNoWarmUp() {
        val rt = object : AskRuntime by FakeRuntime() {
            val sent = mutableListOf<String>()
            override val voiceAvailable = false
            override fun send(json: String) = true.also { sent += json }
        }
        AskController.install(rt)
        AskController.dispatch(AskEvent.Press(voice = false, playing = false))
        assertEquals(emptyList<String>(), rt.sent.filter { "ask_warm" in it })
    }
}
