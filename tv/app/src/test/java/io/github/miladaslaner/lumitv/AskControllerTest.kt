package io.github.miladaslaner.lumitv

import android.media.session.PlaybackState
import io.github.miladaslaner.lumitv.AskEffect.ArmTimeout
import io.github.miladaslaner.lumitv.AskEffect.PausePlayback
import io.github.miladaslaner.lumitv.AskEffect.PostTyped
import io.github.miladaslaner.lumitv.AskEffect.ResumePlayback
import io.github.miladaslaner.lumitv.AskEffect.SendCancel
import io.github.miladaslaner.lumitv.AskEffect.StartCapture
import io.github.miladaslaner.lumitv.AskEffect.StopCapture
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class AskControllerTest {
    private val id1 = "ask1".padEnd(12, 'x')
    private val id2 = "ask2".padEnd(12, 'x')

    /** A running machine: state, the paused flag, and every effect emitted so far. */
    private class Run(var state: AskState = AskState.Idle, var paused: Boolean = false, val limits: AskLimits = AskLimits.DEFAULT) {
        val effects = mutableListOf<AskEffect>()

        /** Ids handed out in order (ask1xxxxxxxx, ask2xxxxxxxx, …), so the tests can name them. */
        private var next = 0
        private val ids = { "ask${++next}".padEnd(12, 'x') }

        fun on(vararg events: AskEvent): Run = apply {
            events.forEach { e ->
                val (s, p, fx) = reduce(state, paused, e, ids, limits)
                state = s
                paused = p
                effects += fx
            }
        }
        fun last(event: AskEvent): List<AskEffect> {
            val before = effects.size
            on(event)
            return effects.drop(before)
        }
    }

    private val voicePress = AskEvent.Press(voice = true, playing = false)

    @Test fun newAskIdIsTwelveUrlSafeChars() {
        val a = newAskId()
        assertTrue(a, Regex("^[A-Za-z0-9_-]{12}$").matches(a))
        assertNotEquals(a, newAskId())
    }

    @Test fun pressWhilePlayingPausesOnce() {
        val r = Run().on(AskEvent.Press(voice = true, playing = true))
        assertEquals(AskState.Listening(id1, null), r.state)
        assertEquals(listOf(PausePlayback, StartCapture(id1)), r.effects.filter { it is StartCapture || it is PausePlayback })
        assertTrue(r.paused)
        r.on(AskEvent.Press(voice = true, playing = true))
        assertEquals(1, r.effects.count { it == PausePlayback })
        assertTrue(r.paused)
    }

    @Test fun pressWithNothingPlayingDoesNotPause() {
        val r = Run().on(voicePress)
        assertFalse(r.paused)
        assertFalse(PausePlayback in r.effects)
    }

    @Test fun pressWithoutVoiceStartsTyping() {
        val r = Run().on(AskEvent.Press(voice = false, playing = false))
        assertEquals(AskState.Typing(id1, ""), r.state)
        assertTrue(r.effects.none { it is StartCapture })
    }

    @Test fun staleAskIdIgnored() {
        val r = Run().on(AskEvent.Press(voice = false, playing = false), AskEvent.Submit("something funny"))
        val before = r.state
        for (e in listOf(
            AskEvent.Heard("old", "x"), AskEvent.ShowArrived("old"), AskEvent.Error("old", "failed"),
            AskEvent.Reply("old", "x"), AskEvent.Timeout("old"),
        )) {
            assertTrue(r.last(e).isEmpty())
            assertEquals(before, r.state)
        }
    }

    @Test fun typedSubmitPostsAndArmsTheTimeout() {
        val r = Run().on(AskEvent.Press(voice = false, playing = false))
        assertEquals(listOf(PostTyped(id1, "something funny"), ArmTimeout(id1, 55_000L)), r.last(AskEvent.Submit("something funny")))
        assertEquals(AskState.Sending(id1), r.state)
        // A blank submit does nothing.
        assertTrue(Run().on(AskEvent.Press(voice = false, playing = false)).last(AskEvent.Submit("  ")).isEmpty())
    }

    @Test fun voiceCapturedSendsAndArmsTheTimeout() {
        val r = Run().on(voicePress, AskEvent.CaptureStarted, AskEvent.Partial("something"))
        assertEquals(AskState.Listening(id1, "something"), r.state)
        assertEquals(listOf(ArmTimeout(id1, 65_000L)), r.last(AskEvent.Captured))
        assertEquals(AskState.Sending(id1), r.state)
        // Upload (5 s) + speech-to-text (10 s) + Claude (45 s) fit before the TV gives up.
        assertTrue(AskLimits.DEFAULT.voiceTimeoutMs >= 5_000 + 10_000 + 45_000)
    }

    @Test fun theDeadlinesFollowTheServersLimits() {
        // the contract: typed = ask_timeout_s + 10 s; spoken = ask_timeout_s + speech_timeout_s + 10 s
        val slow = AskLimits(askTimeoutS = 90.0, speechTimeoutS = 20.0)
        assertEquals(100_000L, slow.typedTimeoutMs)
        assertEquals(120_000L, slow.voiceTimeoutMs)
        val typed = Run(limits = slow).on(AskEvent.Press(voice = false, playing = false))
        assertEquals(listOf(PostTyped(id1, "x"), ArmTimeout(id1, 100_000L)), typed.last(AskEvent.Submit("x")))
        val spoken = Run(limits = slow).on(voicePress)
        assertEquals(listOf(ArmTimeout(id1, 120_000L)), spoken.last(AskEvent.Captured))
        val more = Run(state = AskState.Answered(id1), limits = slow)
        assertEquals(listOf(PostTyped(id1, "more"), ArmTimeout(id1, 100_000L)), more.last(AskEvent.More("more")))
    }

    @Test fun heardThenShowGoesAnswered() {
        val r = Run().on(voicePress, AskEvent.Captured, AskEvent.Posted)
        assertEquals(AskState.Sending(id1), r.state)
        r.on(AskEvent.Heard(id1, "something funny"))
        assertEquals(AskState.Looking(id1, "something funny"), r.state)
        r.on(AskEvent.ShowArrived(id1))
        assertEquals(AskState.Answered(id1), r.state)
    }

    @Test fun desktopShowDoesNotEndAsk() {
        val r = Run().on(voicePress, AskEvent.Captured, AskEvent.Heard(id1, "q"))
        assertTrue(r.last(AskEvent.ShowArrived(null)).isEmpty())
        assertEquals(AskState.Looking(id1, "q"), r.state)
    }

    @Test fun backAfterAnswerResumesPausedVideo() {
        val r = Run().on(AskEvent.Press(voice = true, playing = true), AskEvent.Captured, AskEvent.Heard(id1, "q"), AskEvent.ShowArrived(id1))
        val fx = r.last(AskEvent.Back)
        assertEquals(AskState.Idle, r.state)
        assertFalse(r.paused)
        assertEquals(listOf(ResumePlayback), fx) // the server is done: no cancel
    }

    @Test fun backWhileLookingCancelsAndResumes() {
        val r = Run().on(AskEvent.Press(voice = true, playing = true), AskEvent.Captured, AskEvent.Heard(id1, "q"))
        assertEquals(listOf(SendCancel(id1), ResumePlayback), r.last(AskEvent.Back))
    }

    @Test fun backWhileListeningStopsCapture() {
        val r = Run().on(voicePress)
        assertEquals(listOf(StopCapture), r.last(AskEvent.Back))
        assertEquals(AskState.Idle, r.state)
    }

    @Test fun backWithoutPauseDoesNotResume() {
        val r = Run().on(voicePress, AskEvent.Captured, AskEvent.Error(id1, "failed"))
        assertTrue(r.last(AskEvent.Back).none { it == ResumePlayback })
    }

    @Test fun openedPickNeverResumes() {
        val r = Run().on(AskEvent.Press(voice = true, playing = true), AskEvent.Captured, AskEvent.Heard(id1, "q"), AskEvent.ShowArrived(id1))
        assertTrue(r.last(AskEvent.Opened).isEmpty())
        assertEquals(AskState.Idle, r.state)
        assertFalse(r.paused)
        // Nothing is paused by us any more, so a later BACK resumes nothing either.
        assertTrue(r.last(AskEvent.Back).isEmpty())
    }

    // ---- leaving without BACK (HOME, another app, the screensaver, the screen off: AnswerActivity.onStop) ----

    private fun pausedRun(vararg events: AskEvent) = Run().on(AskEvent.Press(voice = true, playing = true), *events)

    @Test fun leftWhileListeningStopsCaptureAndKeepsTheVideoPaused() {
        val r = pausedRun()
        assertEquals(listOf(StopCapture), r.last(AskEvent.Left))
        assertEquals(AskState.Idle, r.state)
        assertFalse(r.paused)
    }

    @Test fun leftWhileLookingCancelsAndKeepsTheVideoPaused() {
        val r = pausedRun(AskEvent.Captured, AskEvent.Heard(id1, "q"))
        assertEquals(listOf(SendCancel(id1)), r.last(AskEvent.Left))
        assertEquals(AskState.Idle, r.state)
        assertFalse(r.paused)
    }

    @Test fun leftWhileSendingCancelsAndKeepsTheVideoPaused() {
        val r = pausedRun(AskEvent.Captured)
        assertEquals(listOf(SendCancel(id1)), r.last(AskEvent.Left))
        assertFalse(r.paused)
    }

    @Test fun leftAfterAnswerFailureOrReplyKeepsTheVideoPaused() {
        for (end in listOf(AskEvent.ShowArrived(id1), AskEvent.Error(id1, "login"), AskEvent.Reply(id1, "Which mood?"))) {
            val r = pausedRun(AskEvent.Captured, end)
            assertTrue(r.paused)
            assertTrue("$end", r.last(AskEvent.Left).isEmpty())
            assertEquals(AskState.Idle, r.state)
            assertFalse(r.paused)
        }
    }

    @Test fun leftWhileTypingKeepsTheVideoPaused() {
        val r = Run().on(AskEvent.Press(voice = false, playing = true))
        assertTrue(r.last(AskEvent.Left).isEmpty())
        assertFalse(r.paused)
    }

    @Test fun afterLeavingAndComingBackBackResumesNothing() {
        // HOME ended the ask; the user opens Lumi again and presses BACK: the video was not ours to resume any more.
        val r = pausedRun(AskEvent.Captured, AskEvent.ShowArrived(id1), AskEvent.Left)
        assertTrue(r.last(AskEvent.Back).isEmpty())
        assertEquals(0, r.effects.count { it == ResumePlayback })
    }

    @Test fun backThenTheActivityStopIsOneResume() {
        // BACK resumes and finishes the screen; its onStop then sends Left, which must do nothing more.
        val r = pausedRun(AskEvent.Captured, AskEvent.ShowArrived(id1))
        assertEquals(listOf(ResumePlayback), r.last(AskEvent.Back))
        assertTrue(r.last(AskEvent.Left).isEmpty())
        assertEquals(1, r.effects.count { it == ResumePlayback })
    }

    @Test fun leftWithNothingGoingOnDoesNothing() {
        val r = Run()
        assertTrue(r.last(AskEvent.Left).isEmpty())
        assertEquals(AskState.Idle, r.state)
    }

    @Test fun spokenAudioRefusedWith503IsNoSpeech() {
        assertEquals(AskEvent.Error(id1, "no_speech"), postOutcome(id1, PostResult.Rejected(503), audio = true))
        assertEquals(AskEvent.Error(id1, "failed"), postOutcome(id1, PostResult.Rejected(503), audio = false))
        assertEquals(AskEvent.Error(id1, "failed"), postOutcome(id1, PostResult.Rejected(400), audio = true))
        assertEquals(AskEvent.Posted, postOutcome(id1, PostResult.Accepted, audio = true))
        assertEquals(AskEvent.PostFailed, postOutcome(id1, PostResult.Unreachable, audio = false))
        val r = Run().on(voicePress, AskEvent.Captured, postOutcome(id1, PostResult.Rejected(503), audio = true))
        assertEquals(AskState.Failed(id1, "no_speech"), r.state)
    }

    @Test fun failedThenBackResumes() {
        val r = Run().on(AskEvent.Press(voice = true, playing = true), AskEvent.Captured, AskEvent.Error(id1, "login"))
        assertEquals(AskState.Failed(id1, "login"), r.state)
        assertTrue(r.paused) // still paused while the error is on screen
        assertEquals(listOf(ResumePlayback), r.last(AskEvent.Back))
        assertFalse(r.paused)
    }

    @Test fun okOnAnErrorEndsTheAskAndLeavesTheVideoPausedForBack() {
        val r = Run().on(AskEvent.Press(voice = true, playing = true), AskEvent.Captured, AskEvent.Error(id1, "login"))
        assertTrue(r.last(AskEvent.Dismiss).isEmpty())
        assertEquals(AskState.Idle, r.state)
        assertTrue(r.paused) // Lumi is still in front: the video plays again only when BACK leaves it
        assertEquals(listOf(ResumePlayback), r.last(AskEvent.Back))
        assertFalse(r.paused)
        // Anywhere else it is nothing.
        val listening = Run().on(voicePress)
        assertTrue(listening.last(AskEvent.Dismiss).isEmpty())
        assertEquals(AskState.Listening(id1, null), listening.state)
    }

    @Test fun backOnTheKeyboardLeavesItWithoutQuittingLumi() {
        // the answer underneath is untouched (nothing is consumed here) and the paused video waits for a real BACK
        val r = Run().on(AskEvent.Press(voice = false, playing = true))
        assertEquals(AskState.Typing(id1, ""), r.state)
        assertTrue(r.last(AskEvent.Dismiss).isEmpty())
        assertEquals(AskState.Idle, r.state)
        assertTrue(r.paused)
        assertEquals(listOf(ResumePlayback), r.last(AskEvent.Back))
    }

    @Test fun replyThenBackResumes() {
        val r = Run().on(AskEvent.Press(voice = true, playing = true), AskEvent.Captured, AskEvent.Reply(id1, "Which mood?"))
        assertEquals(AskState.Reply(id1, "Which mood?"), r.state)
        assertEquals(listOf(ResumePlayback), r.last(AskEvent.Back))
    }

    @Test fun postFailedIsOffline() {
        val r = Run().on(AskEvent.Press(voice = false, playing = true), AskEvent.Submit("x"), AskEvent.PostFailed)
        assertEquals(AskState.Failed(id1, "offline"), r.state)
        assertEquals(listOf(ResumePlayback), r.last(AskEvent.Back))
    }

    @Test fun timeoutAfter55sFailsAndCancels() {
        val r = Run().on(voicePress, AskEvent.Captured, AskEvent.Heard(id1, "q"))
        assertEquals(listOf(SendCancel(id1)), r.last(AskEvent.Timeout(id1)))
        assertEquals(AskState.Failed(id1, "timeout"), r.state)
        // Once answered, a late timer is harmless.
        val done = Run().on(voicePress, AskEvent.Captured, AskEvent.ShowArrived(id1))
        assertTrue(done.last(AskEvent.Timeout(id1)).isEmpty())
        assertEquals(AskState.Answered(id1), done.state)
    }

    @Test fun nothingHeardIsDidntCatchThat() {
        val r = Run().on(voicePress, AskEvent.NothingHeard)
        assertEquals(AskState.Failed(id1, "not_heard"), r.state)
    }

    @Test fun aCaptureFailureSaysWhatIsWrong() {
        // the reasons the captures name (RecognizerCapture, RemoteMicCapture, TvVoiceCapture) -> the line's code
        mapOf(
            "permission" to "mic_permission",
            "recognizer" to "no_recognizer", "unavailable" to "no_recognizer", "network" to "no_recognizer",
            "mic" to "not_heard", "not on screen" to "not_heard", "no capture" to "not_heard",
        ).forEach { (reason, code) ->
            assertEquals(reason, code, captureFailureCode(reason))
            assertEquals(reason, AskState.Failed(id1, code), Run().on(voicePress, AskEvent.CaptureFailed(reason)).state)
        }
        // only while listening
        val sending = Run().on(voicePress, AskEvent.Captured)
        assertTrue(sending.last(AskEvent.CaptureFailed("permission")).isEmpty())
        assertEquals(AskState.Sending(id1), sending.state)
    }

    @Test fun secondPressCancelsFirst() {
        val r = Run().on(voicePress, AskEvent.Captured, AskEvent.Heard(id1, "q"))
        val fx = r.last(AskEvent.Press(voice = true, playing = false))
        assertEquals(AskState.Listening(id2, null), r.state)
        assertEquals(listOf(SendCancel(id1), StartCapture(id2)), fx)
        // The first ask's late answer is ignored.
        assertTrue(r.last(AskEvent.ShowArrived(id1)).isEmpty())
        assertEquals(AskState.Listening(id2, null), r.state)
    }

    @Test fun pressWhileListeningRestartsCapture() {
        val r = Run().on(voicePress)
        assertEquals(listOf(StopCapture, StartCapture(id2)), r.last(voicePress))
    }

    @Test fun typeInsteadStartsTyping() {
        val fromFailed = Run().on(voicePress, AskEvent.NothingHeard)
        assertTrue(fromFailed.last(AskEvent.TypeInstead).isEmpty())
        assertEquals(AskState.Typing(id2, ""), fromFailed.state)
        val fromListening = Run().on(voicePress)
        assertEquals(listOf(StopCapture), fromListening.last(AskEvent.TypeInstead))
        assertEquals(AskState.Typing(id2, ""), fromListening.state)
        val fromReply = Run().on(voicePress, AskEvent.Captured, AskEvent.Reply(id1, "Which mood?"))
        fromReply.on(AskEvent.TypeInstead)
        assertEquals(AskState.Typing(id2, ""), fromReply.state)
    }

    @Test fun retryListensAgainWithANewId() {
        val r = Run().on(AskEvent.Press(voice = true, playing = true), AskEvent.NothingHeard)
        assertEquals(listOf(StartCapture(id2)), r.last(AskEvent.Retry))
        assertEquals(AskState.Listening(id2, null), r.state)
        assertTrue(r.paused) // the video stays paused across the retry
    }

    @Test fun pauseTargetsPicksOnlyPlayingOrBufferingSessions() {
        val sessions = listOf(
            "com.google.android.youtube.tv" to PlaybackState.STATE_PLAYING,
            "com.netflix.ninja" to PlaybackState.STATE_PAUSED,
            "com.amazon.amazonvideo.livingroom" to PlaybackState.STATE_BUFFERING,
            "com.spotify.tv.android" to null,
            "nl.rtl.videoland.v2" to PlaybackState.STATE_STOPPED,
        )
        assertEquals(listOf("com.google.android.youtube.tv", "com.amazon.amazonvideo.livingroom"), pauseTargets(sessions))
        assertEquals(emptyList<String>(), pauseTargets(emptyList<Pair<String, Int?>>()))
    }

    @Test fun timingsForAVoiceAsk() {
        val t = AskTimings("k1")
        t.pressAt = 1_000
        t.listeningDrawnAt = 1_400
        t.speechEndAt = 5_000
        t.lookingDrawnAt = 6_200
        val o = JSONObject(t.message(postersDrawnAt = 14_000))
        assertEquals(400L, o.getLong("listen_ms"))
        assertEquals(1_200L, o.getLong("heard_ms"))
        assertEquals(9_000L, o.getLong("posters_ms"))
        assertFalse(o.has("typed_posters_ms"))
    }

    @Test fun timingsForATypedAsk() {
        val t = AskTimings("k1")
        t.pressAt = 1_000
        t.submitAt = 20_000
        val o = JSONObject(t.message(postersDrawnAt = 27_500))
        assertEquals(7_500L, o.getLong("typed_posters_ms"))
        assertFalse(o.has("posters_ms"))
        assertFalse(o.has("heard_ms"))
        assertFalse(o.has("listen_ms")) // typing has no listening screen
    }

    @Test fun askIdOfEachState() {
        assertNull(AskState.Idle.askId)
        assertEquals("k", AskState.Answered("k").askId)
    }

    @Test fun resumeTargetsOnlyTheSessionsThePressPaused() {
        // Session tokens stand in as strings: "yt1" was paused by the press; "yt2" (same app) was paused before it.
        val now = listOf(
            "yt1" to PlaybackState.STATE_PAUSED,
            "yt2" to PlaybackState.STATE_PAUSED,
            "nf1" to PlaybackState.STATE_PLAYING,
        )
        assertEquals(listOf("yt1"), resumeTargets(now, paused = listOf("yt1", "nf1")))
        // One the user already resumed, stopped or closed is left alone.
        assertEquals(emptyList<String>(), resumeTargets(listOf("yt1" to PlaybackState.STATE_STOPPED), paused = listOf("yt1")))
        assertEquals(emptyList<String>(), resumeTargets(emptyList<Pair<String, Int?>>(), paused = listOf("yt1")))
    }

    private fun show(askId: String?) = ServerMsg.Show("ans", "q", "h", "", emptyList(), askId = askId)

    @Test fun lateShowAfterTimeoutIsDropped() {
        assertFalse(wantsAskMessage(AskState.Failed(id1, "timeout"), show(id1)))
        assertFalse(wantsAskMessage(AskState.Failed(id1, "failed"), show(id1)))
        assertFalse(wantsAskMessage(AskState.Failed(id1, "timeout"), ServerMsg.Looking("t", "q", askId = id1)))
        assertFalse(wantsAskMessage(AskState.Idle, show(id1))) // left with BACK or HOME
        assertFalse(wantsAskMessage(AskState.Answered(id1), show(id1)))
    }

    @Test fun showForTheWaitingAskIsWanted() {
        assertTrue(wantsAskMessage(AskState.Sending(id1), show(id1)))
        assertTrue(wantsAskMessage(AskState.Looking(id1, "q"), show(id1)))
        assertTrue(wantsAskMessage(AskState.Sending(id1), ServerMsg.Looking("t", "q", askId = id1)))
        assertTrue(wantsAskMessage(AskState.Listening(id1, null), ServerMsg.AskError(id1, "not_heard")))
        assertTrue(wantsAskMessage(AskState.Listening(id1, null), ServerMsg.Heard(id1, "x")))
        assertFalse(wantsAskMessage(AskState.Listening(id1, null), show(id1)))
    }

    @Test fun staleAskMessagesAreNotWanted() {
        assertFalse(wantsAskMessage(AskState.Sending(id2), show(id1)))
        assertFalse(wantsAskMessage(AskState.Looking(id2, "q"), ServerMsg.AskReply(id1, "x")))
        assertFalse(wantsAskMessage(AskState.Looking(id2, "q"), ServerMsg.Heard(id1, "x")))
    }

    @Test fun desktopMessagesAlwaysReachTheScreen() {
        listOf(AskState.Idle, AskState.Failed(id1, "timeout"), AskState.Typing(id1, ""), AskState.Sending(id1)).forEach { st ->
            assertTrue(wantsAskMessage(st, show(null)))
            assertTrue(wantsAskMessage(st, ServerMsg.Looking("t", "desktop")))
            assertTrue(wantsAskMessage(st, ServerMsg.Open(emptyList())))
        }
    }

    @Test fun askIdOfMessages() {
        assertEquals(id1, askIdOf(show(id1)))
        assertNull(askIdOf(show(null)))
        assertEquals(id1, askIdOf(ServerMsg.AskError(id1, "x")))
        assertNull(askIdOf(ServerMsg.Open(emptyList())))
    }

    @Test fun captureEventsMapToAskEvents() {
        assertEquals(AskEvent.CaptureStarted, askEventFor(CaptureEvent.Started))
        assertEquals(AskEvent.Partial("some"), askEventFor(CaptureEvent.Partial("some")))
        assertEquals(AskEvent.NothingHeard, askEventFor(CaptureEvent.NothingHeard))
        assertEquals(AskEvent.CaptureFailed("no mic"), askEventFor(CaptureEvent.Failed("no mic")))
        // Text and Audio end the capture and are sent; the controller handles them itself.
        assertNull(askEventFor(CaptureEvent.Text("something funny", 10)))
    }

    @Test fun noCaptureIsUnavailable() = assertFalse(NoCapture.available)

    @Test fun moreFromAnAnswerSendsATypedAskRightAway() {
        val r = Run(state = AskState.Answered(id1))
        assertEquals(
            listOf(PostTyped(id1, "Show me more like these"), ArmTimeout(id1, AskLimits.DEFAULT.typedTimeoutMs)),
            r.last(AskEvent.More("Show me more like these")),
        )
        assertEquals(AskState.Sending(id1), r.state)
    }

    @Test fun moreAlsoWorksOnAnAnswerFromClaudeDesktop() {
        val r = Run().on(AskEvent.More("Show me more like these"))
        assertEquals(AskState.Sending(id1), r.state)
    }

    @Test fun moreIsIgnoredWhileAnAskIsUnderway() {
        val r = Run().on(voicePress)
        val before = r.state
        assertTrue(r.last(AskEvent.More("Show me more like these")).isEmpty())
        assertEquals(before, r.state)
    }

    @Test fun aRateLimitedAskSaysLimit() {
        assertEquals(AskEvent.Error("a", "limit"), postOutcome("a", PostResult.Rejected(429), audio = false))
        assertEquals(AskEvent.Error("a", "limit"), postOutcome("a", PostResult.Rejected(429), audio = true))
        assertEquals(AskEvent.Error("a", "no_speech"), postOutcome("a", PostResult.Rejected(503), audio = true))
    }
}
