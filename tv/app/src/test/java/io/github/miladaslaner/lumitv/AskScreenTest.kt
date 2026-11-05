package io.github.miladaslaner.lumitv

import io.github.miladaslaner.lumitv.ui.AskAction
import io.github.miladaslaner.lumitv.ui.KeyboardModel
import io.github.miladaslaner.lumitv.ui.Screen
import io.github.miladaslaner.lumitv.ui.UiStrings
import io.github.miladaslaner.lumitv.ui.actions
import io.github.miladaslaner.lumitv.ui.hintParts
import io.github.miladaslaner.lumitv.ui.screenFor
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class AskScreenTest {
    private val s = UiStrings.Default
    private val id = "ask1xxxxxxxx"
    private val show = ServerMsg.Show("ans1", "q", "Three funny ones", "", emptyList(), askId = id)
    private val desktopLooking = ServerMsg.Looking("turn1", "a desktop question")

    @Test fun idleShowsTheAnswerOrDesktopLookingOrNothing() {
        assertEquals(Screen.Idle, screenFor(AskState.Idle, null, null, s))
        assertEquals(Screen.Answer(show), screenFor(AskState.Idle, show, null, s))
        assertEquals(Screen.Looking("a desktop question"), screenFor(AskState.Idle, null, desktopLooking, s))
    }

    @Test fun anEndedAsksLookingIsNotShownAgain() {
        val ours = ServerMsg.Looking("turn2", "funny films", askId = id)
        assertEquals(Screen.Idle, screenFor(AskState.Idle, null, ours, s))
        assertEquals(Screen.Idle, screenFor(AskState.Answered(id), null, ours, s))
    }

    @Test fun listeningShowsThePartial() {
        assertEquals(Screen.Listening(null), screenFor(AskState.Listening(id, null), null, null, s))
        assertEquals(Screen.Listening("something fu"), screenFor(AskState.Listening(id, "something fu"), null, null, s))
    }

    @Test fun typingShowsTheKeyboard() =
        assertEquals(Screen.Typing(KeyboardModel()), screenFor(AskState.Typing(id, ""), show, null, s))

    @Test fun sendingShowsLookingWithWhatWasTyped() {
        assertEquals(Screen.Looking("something funny"), screenFor(AskState.Sending(id), null, null, s, sentText = "something funny"))
        assertEquals(Screen.Looking(""), screenFor(AskState.Sending(id), null, null, s))
        // The server's own `looking` for this ask names the question too.
        val ours = ServerMsg.Looking("turn2", "funny films", askId = id)
        assertEquals(Screen.Looking("funny films"), screenFor(AskState.Sending(id), null, ours, s))
    }

    @Test fun lookingShowsWhatWasHeard() =
        assertEquals(Screen.Looking("something funny"), screenFor(AskState.Looking(id, "something funny"), null, null, s))

    @Test fun staleDesktopLookingLosesToAskState() {
        assertEquals(Screen.Looking("something funny"), screenFor(AskState.Looking(id, "something funny"), null, desktopLooking, s))
        assertEquals(Screen.Typing(KeyboardModel()), screenFor(AskState.Typing(id, ""), null, desktopLooking, s))
        assertEquals(Screen.Looking(""), screenFor(AskState.Sending(id), null, desktopLooking, s))
    }

    @Test fun answeredShowsTheShow() {
        assertEquals(Screen.Answer(show), screenFor(AskState.Answered(id), show, null, s))
        assertEquals(Screen.Idle, screenFor(AskState.Answered(id), null, null, s))
    }

    @Test fun replyShowsTheLine() =
        assertEquals(Screen.Reply("Which mood are you in?"), screenFor(AskState.Reply(id, "Which mood are you in?"), null, null, s))

    @Test fun notHeardShowsDidntCatch() =
        assertEquals(Screen.AskFailed("Didn't catch that", "not_heard"), screenFor(AskState.Failed(id, "not_heard"), null, null, s))

    @Test fun everyErrorCodeHasItsLine() {
        val expected = mapOf(
            "not_heard" to "Didn't catch that",
            "failed" to "Lumi couldn't answer that. Try again?",
            "timeout" to "That took too long. Try again?",
            "login" to "Lumi is logged out. Ask whoever installed it to log it back in.",
            "limit" to "Lumi has used up its turns for now. Try again later.",
            "offline" to "Lumi can't connect right now. Try again in a minute.",
            "not_installed" to "Lumi isn't fully set up yet. Ask whoever installed it.",
            "no_speech" to "Lumi can't listen right now. Type instead?",
            "mic_permission" to "Lumi isn't allowed to use the mic. Type instead?",
            "no_recognizer" to "The TV's speech recognition isn't working. Type instead?",
            "something_new" to "Lumi couldn't answer that. Try again?",
        )
        expected.forEach { (code, line) -> assertEquals(code, Screen.AskFailed(line, code), screenFor(AskState.Failed(id, code), null, null, s)) }
    }

    @Test fun errorLinesComeFromTheStringsFile() {
        val nl = UiStrings.from(JSONObject("""{"ask_login":"Lumi moet inloggen"}"""))
        assertEquals(Screen.AskFailed("Lumi moet inloggen", "login"), screenFor(AskState.Failed(id, "login"), null, null, nl))
    }

    @Test fun failureScreensOfferOnlyWhatCanWork() {
        val retry = listOf(AskAction.TRY_AGAIN, AskAction.TYPE_INSTEAD)
        listOf("not_heard", "failed", "timeout", "offline", "something_new").forEach { assertEquals(it, retry, Screen.AskFailed("x", it).actions()) }
        // the mic is the problem: only typing helps
        listOf("no_speech", "mic_permission", "no_recognizer").forEach { assertEquals(it, listOf(AskAction.TYPE_INSTEAD), Screen.AskFailed("x", it).actions()) }
        // nothing on the TV can fix these: a retry would fail the same way
        listOf("login", "not_installed", "limit").forEach { assertEquals(it, listOf(AskAction.OK), Screen.AskFailed("x", it).actions()) }
    }

    @Test fun screensOfferTheirPills() {
        assertEquals(listOf(AskAction.ASK, AskAction.WATCHING), Screen.Idle.actions(hasViewers = true))
        assertEquals(listOf(AskAction.SETUP_VIEWERS, AskAction.ASK), Screen.Idle.actions(hasViewers = false))
        assertEquals(listOf(AskAction.TRY_AGAIN, AskAction.TYPE_INSTEAD), Screen.AskFailed("x", "failed").actions())
        assertEquals(listOf(AskAction.TYPE_INSTEAD), Screen.Reply("x").actions())
        assertEquals(emptyList<AskAction>(), Screen.Answer(show).actions())
        assertEquals(emptyList<AskAction>(), Screen.Looking("").actions())
        assertEquals(emptyList<AskAction>(), Screen.Typing(KeyboardModel()).actions())
    }

    @Test fun newStringDefaults() {
        assertEquals("Listening…", s.listening)
        assertEquals("Try again", s.tryAgain)
        assertEquals("Type instead", s.typeInstead)
        assertEquals("What do you feel like watching?", s.typingHint)
        assertEquals("Mic: ask again", s.askAgain)
        assertEquals("Ask Lumi", s.ask)
        assertEquals("What do you feel like watching?", s.idleTitle)
        assertEquals("Press the mic button and just say it.", s.idle)
        assertEquals("Press OK and type it.", s.idleNoMic)
    }

    @Test fun hintsSplitIntoKeyAndLabel() {
        assertEquals("◀ ▶" to "choose", hintParts("◀ ▶: choose"))
        assertEquals("OK" to "open", hintParts("OK: open"))
        assertEquals("Mic" to "ask again", hintParts("Mic: ask again"))
        assertEquals("▼" to "other services", hintParts("▼: other services"))
        assertEquals("" to "open it", hintParts("open it")) // without the colon form: a label alone, no key box
    }

    @Test fun everyShippedHintHasAKeyAndALabel() {
        val shipped = JSONObject(java.io.File("../../server/content/strings.en.json").readText())
        val hints = shipped.getJSONArray("hints").let { a -> (0 until a.length()).map { a.getString(it) } } +
            listOf("ask_again", "other_services", "close_row").map { shipped.getString(it) }
        hints.forEach { hint ->
            val (key, label) = hintParts(hint)
            assertTrue(hint, key.isNotBlank() && label.isNotBlank())
        }
        assertEquals(listOf("◀ ▶", "OK", "Mic", "▼", "▲"), hints.map { hintParts(it).first })
    }

    @Test fun answerScreenHasTheMoreTileAfterThePostersOnly() {
        assertEquals(4, io.github.miladaslaner.lumitv.ui.answerSlots(3))
        assertEquals(2, io.github.miladaslaner.lumitv.ui.answerSlots(1))
        assertEquals(0, io.github.miladaslaner.lumitv.ui.answerSlots(0))
    }

    @Test fun lookingShowsTheLikelyPostersOfThisAskOnly() {
        val strings = UiStrings.Default
        val preview = ServerMsg.Preview("ask1", listOf(io.github.miladaslaner.lumitv.PosterUi("Heat", "http://p")))
        val looking = screenFor(AskState.Looking("ask1", "q"), null, null, strings, preview = preview) as Screen.Looking
        assertEquals(listOf("Heat"), looking.posters?.map { it.title })
        val other = screenFor(AskState.Looking("ask2", "q"), null, null, strings, preview = preview) as Screen.Looking
        assertEquals(null, other.posters)
        val sending = screenFor(AskState.Sending("ask1"), null, null, strings, sentText = "q", preview = preview) as Screen.Looking
        assertEquals(listOf("Heat"), sending.posters?.map { it.title })
    }
}
