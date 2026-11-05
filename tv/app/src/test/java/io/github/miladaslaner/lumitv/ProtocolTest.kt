package io.github.miladaslaner.lumitv

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test

class ProtocolTest {
    @get:Rule val lumi = LumiStateRule()

    private val launch = """{"package":"com.netflix.ninja","action":"android.intent.action.VIEW","data":"https://x","extras":{"a":"b"},"vpn":"off","fallback":{"package":"p","action":"MAIN","vpn":"any"}}"""

    @Test fun showWithPickLackingOptionalFields() {
        val json = """{"type":"show","answer_id":"a1","question":"q","headline":"h","subline":"s",
            "picks":[{"token":"t1","title":"Dune","service":"netflix","service_label":"Netflix","launch":$launch}]}"""
        val msg = parseServerMsg(json) as ServerMsg.Show
        assertEquals("a1", msg.answerId)
        val pick = msg.picks.single()
        assertNull(pick.posterUrl)
        assertNull(pick.lengthLabel)
        assertEquals("Netflix", pick.serviceLabel)
        // the older single `launch` with its fallback chain becomes the ordered list; the hint was the first step's
        assertEquals(listOf("com.netflix.ninja", "p"), pick.launches.map { it.packageName })
        assertEquals("off", pick.launches[0].vpn)
        assertEquals("b", pick.launches[0].extras["a"])
        assertEquals(listOf(true, false), pick.launches.map { it.hint })
    }

    @Test fun showWithNullAndPresentOptionals() {
        val json = """{"type":"show","answer_id":"a1","question":"q","headline":"h","subline":"s",
            "picks":[{"token":"t","title":"T","poster_url":null,"service_label":"N","length_label":"1h 52m","launch":$launch}]}"""
        val pick = (parseServerMsg(json) as ServerMsg.Show).picks.single()
        assertNull(pick.posterUrl)
        assertEquals("1h 52m", pick.lengthLabel)
    }

    @Test fun pickCarriesEveryProviderBestFirstAndCanSwitchToOne() {
        val nf = """{"package":"com.netflix.ninja","action":"android.intent.action.MAIN","vpn":"off"}"""
        val pv = """{"package":"com.amazon","action":"android.intent.action.VIEW","data":"https://p/search","vpn":"any","hint":true}"""
        val json = """{"type":"show","answer_id":"a1","question":"q","headline":"h","subline":"s",
            "picks":[{"token":"t","title":"Suits","service":"netflix","service_label":"Netflix","launches":[$nf],
              "providers":[{"service":"netflix","service_label":"Netflix","launches":[$nf]},
                           {"service":"prime","service_label":"Prime Video","launches":[$pv],"search_hint":"Suits"}]}]}"""
        val pick = (parseServerMsg(json) as ServerMsg.Show).picks.single()
        assertEquals(listOf("Netflix", "Prime Video"), pick.providers.map { it.serviceLabel })
        assertEquals("netflix", pick.service)
        val onPrime = pick.on(pick.providers[1])
        assertEquals("prime", onPrime.service)
        assertEquals("Prime Video", onPrime.serviceLabel)
        assertEquals("com.amazon", onPrime.launches.single().packageName)
        assertEquals("Suits", onPrime.searchHint)
        assertEquals("t", onPrime.token)
    }

    @Test fun aPickWithoutProvidersHasItsOwnServiceAsTheOnlyOne() {
        val json = """{"type":"show","answer_id":"a1","question":"q","headline":"h","subline":"s",
            "picks":[{"token":"t","title":"T","service":"netflix","service_label":"N","launch":$launch}]}"""
        val pick = (parseServerMsg(json) as ServerMsg.Show).picks.single()
        assertEquals(listOf("N"), pick.providers.map { it.serviceLabel })
        assertEquals(pick.launches, pick.providers.single().launches)
    }

    @Test fun helloCarriesViewersAndKidsHoursAndViewersSetRoundTrips() {
        val hello = parseServerMsg("""{"type":"hello","assistant_name":"Lumi","theme":{},"strings":{},
            "viewers":[{"name":"Milad","age":null},{"name":"Emma","age":6},{"name":"","age":3}],
            "kids_hours":{"from":"06:00","until":"19:00"}}""") as ServerMsg.Hello
        assertEquals(listOf(Viewer("Milad"), Viewer("Emma", 6)), hello.viewers)
        assertEquals("06:00" to "19:00", hello.kidsFrom to hello.kidsUntil)
        val set = JSONObject(viewersSetMsg(listOf(Viewer("Milad"), Viewer("Emma", 6))))
        assertEquals("viewers_set", set.getString("type"))
        val echoed = parseServerMsg("""{"type":"viewers","viewers":${set.getJSONArray("viewers")}}""")
        assertEquals(ServerMsg.Viewers(listOf(Viewer("Milad"), Viewer("Emma", 6))), echoed)
        assertEquals(listOf("Emma"), JSONObject(askRequestJson("a", "t", "typed", null, null, listOf("Emma"))).getJSONArray("viewers").let { a -> (0 until a.length()).map { a.getString(it) } })
    }

    @Test fun openOutcomeMessages() {
        val failed = JSONObject(openFailedMsg("t", "prime"))
        assertEquals(listOf("open_failed", "t", "prime"), listOf(failed.getString("type"), failed.getString("token"), failed.getString("service")))
        val played = JSONObject(playCheckMsg("t", "netflix", false))
        assertEquals("play_check", played.getString("type"))
        assertFalse(played.getBoolean("played"))
    }

    @Test fun openedNamesTheServiceItOpenedOn() {
        assertEquals("prime", org.json.JSONObject(openedMsg("t", "prime")).getString("service"))
        assertFalse(org.json.JSONObject(openedMsg("t", null)).has("service"))
    }

    @Test fun previewCarriesPosterTitlesAndUrls() {
        val msg = parseServerMsg("""{"type":"preview","ask_id":"a1","posters":[{"title":"Heat","poster_url":"http://p"},{"title":"X","poster_url":null}]}""")
        assertEquals(ServerMsg.Preview("a1", listOf(PosterUi("Heat", "http://p"), PosterUi("X", null))), msg)
        assertEquals("a1", askIdOf(msg as ServerMsg))
    }

    @Test fun pickReasonIsOptional() {
        val json = """{"type":"show","answer_id":"a1","question":"q","headline":"h","subline":"s",
            "picks":[{"token":"t","title":"T","service_label":"N","reason":"Trek itself","launch":$launch},
                     {"token":"u","title":"U","service_label":"N","launch":$launch}]}"""
        val (t, u) = (parseServerMsg(json) as ServerMsg.Show).picks
        assertEquals("Trek itself", t.reason)
        assertNull(u.reason)
    }

    private val launches = """[
        {"package":"com.google.android.apps.tv.launcherx","action":"android.intent.action.VIEW","data":"https://tv.google.com/asset/%2Fg%2F1","vpn":"any"},
        {"package":"com.netflix.ninja","action":"android.intent.action.VIEW","data":"https://www.netflix.com/title/70195800",
         "component":"com.netflix.ninja/.MainActivity","extras":{"source":"30"},"vpn":"off","hint":false,"relaunch_after_s":[8,16]},
        {"package":"com.amazon","action":"android.intent.action.VIEW","data":"https://p/search","vpn":"any","hint":true},
        {"package":"com.netflix.ninja","action":"android.intent.action.MAIN","vpn":"off"}]"""

    @Test fun launchesListIsPreferredAndKeepsItsOrderAndFields() {
        val json = """{"type":"show","answer_id":"a","question":"q","headline":"h","subline":"s",
            "picks":[{"token":"t","title":"Suits","service_label":"Netflix","launches":$launches,"launch":$launch}]}"""
        val steps = (parseServerMsg(json) as ServerMsg.Show).picks.single().launches
        assertEquals(
            listOf("com.google.android.apps.tv.launcherx", "com.netflix.ninja", "com.amazon", "com.netflix.ninja"),
            steps.map { it.packageName },
        )
        assertEquals("com.netflix.ninja/.MainActivity", steps[1].component)
        assertEquals(mapOf("source" to "30"), steps[1].extras)
        assertEquals(listOf(8.0, 16.0), steps[1].relaunchAfterS)
        assertEquals(listOf(false, false, true, false), steps.map { it.hint })
        assertEquals(emptyList<Double>(), steps[3].relaunchAfterS)
        val open = parseServerMsg("""{"type":"open","title":"Suits","service_label":"Netflix","launches":$launches}""") as ServerMsg.Open
        assertEquals(4, open.launches.size)
        assertEquals("Suits", open.title)
    }

    @Test fun oneBadPickKeepsTheOthers() {
        val good = """{"token":"t1","title":"Dune","service_label":"N","launches":[{"package":"p","action":"MAIN"}]}"""
        val noToken = """{"title":"X","service_label":"N","launches":[{"package":"p","action":"MAIN"}]}"""
        val noLaunch = """{"token":"t3","title":"Y","service_label":"N"}"""
        val show = parseServerMsg("""{"type":"show","answer_id":"a1","question":"q","headline":"h","subline":"s",
            "picks":[$noToken,$good,$noLaunch]}""") as ServerMsg.Show
        assertEquals(listOf("t1"), show.picks.map { it.token })
        // what went, naming the field, so the log says which pick the server built wrong
        assertEquals(2, show.droppedPicks.size)
        assertTrue(show.droppedPicks[0], "token" in show.droppedPicks[0])
        assertTrue(show.droppedPicks[1], "launch" in show.droppedPicks[1])
        // only when no pick survives is the whole answer dropped
        assertNull(parseServerMsg("""{"type":"show","answer_id":"a1","picks":[$noToken]}"""))
        // no picks at all is a valid answer (nothing found), not a malformed one
        assertEquals(emptyList<String>(), (parseServerMsg("""{"type":"show","answer_id":"a1","picks":[]}""") as ServerMsg.Show).droppedPicks)
    }

    @Test fun unknownTypeIsNull() = assertNull(parseServerMsg("""{"type":"nope"}"""))

    @Test fun malformedIsNull() {
        assertNull(parseServerMsg("not json"))
        assertNull(parseServerMsg("""{"type":"show"}"""))
    }

    @Test fun lookingParses() {
        assertEquals(ServerMsg.Looking("t9", "something funny"), parseServerMsg("""{"type":"looking","turn_id":"t9","question":"something funny"}"""))
    }

    @Test fun openParses() {
        val msg = parseServerMsg("""{"type":"open","launch":$launch}""") as ServerMsg.Open
        assertEquals("com.netflix.ninja", msg.launches.first().packageName)
        assertEquals("", msg.title)
        assertEquals("", msg.serviceLabel)
    }

    @Test fun openCarriesServiceLabel() {
        val msg = parseServerMsg("""{"type":"open","service_label":"Netflix","launch":$launch}""") as ServerMsg.Open
        assertEquals("Netflix", msg.serviceLabel)
    }

    @Test fun helloKeepsThemeAndStrings() {
        val msg = parseServerMsg("""{"type":"hello","assistant_name":"Lumi","theme":{"bg":"#08090a"},"strings":{"looking":"Looking..."}}""") as ServerMsg.Hello
        assertEquals("Lumi", msg.assistantName)
        assertEquals("#08090a", msg.theme.getString("bg"))
        assertEquals("Looking...", msg.strings.getString("looking"))
        assertFalse(msg.speech) // an older server says nothing about speech: the TV uses its own recognizer
    }

    @Test fun helloCarriesTheServersLimitsAndOlderServersGetTheDefaults() {
        val older = parseServerMsg("""{"type":"hello","assistant_name":"Lumi","theme":{},"strings":{}}""") as ServerMsg.Hello
        assertEquals(AskLimits.DEFAULT, older.limits)
        assertEquals(AskLimits(45.0, 200, 15.0, 10.0), AskLimits.DEFAULT) // today's constants: 55 s / 65 s / 200 / 15 s
        assertEquals(55_000L to 65_000L, AskLimits.DEFAULT.typedTimeoutMs to AskLimits.DEFAULT.voiceTimeoutMs)
        assertEquals(15_000, AskLimits.DEFAULT.audioMs)
        val tuned = parseServerMsg("""{"type":"hello","assistant_name":"Lumi","theme":{},"strings":{},
            "limits":{"ask_timeout_s":90,"max_question_chars":120,"max_audio_s":20.5,"speech_timeout_s":15}}""") as ServerMsg.Hello
        assertEquals(AskLimits(90.0, 120, 20.5, 15.0), tuned.limits)
        assertEquals(20_500, tuned.limits.audioMs)
        // a partial or senseless `limits` keeps the defaults for what it does not say
        val partial = parseServerMsg("""{"type":"hello","assistant_name":"Lumi","theme":{},"strings":{},
            "limits":{"ask_timeout_s":0,"max_question_chars":-5,"max_audio_s":"nope"}}""") as ServerMsg.Hello
        assertEquals(AskLimits.DEFAULT, partial.limits)
        assertEquals(1000, AskLimits(maxQuestionChars = 5000).questionChars) // the keyboard's own ceiling
    }

    @Test fun helloSaysWhetherTheServerHearsSpeech() {
        val yes = parseServerMsg("""{"type":"hello","assistant_name":"Lumi","theme":{},"strings":{},"speech":true}""") as ServerMsg.Hello
        assertTrue(yes.speech)
        val no = parseServerMsg("""{"type":"hello","assistant_name":"Lumi","theme":{},"strings":{},"speech":false}""") as ServerMsg.Hello
        assertFalse(no.speech)
    }

    @Test fun stateRoundTrips() {
        val o = JSONObject(stateMsg(true, false, true))
        assertEquals("state", o.getString("type"))
        assertTrue(o.getBoolean("screen_on"))
        assertEquals(false, o.getBoolean("playing"))
        assertTrue(o.getBoolean("vpn"))
        assertTrue(o.getBoolean("playing_known"))
    }

    @Test fun unknownPlaybackIsReportedAsPlaying() {
        val o = JSONObject(stateMsg(true, playing = false, vpn = false, playingKnown = false))
        assertEquals(false, o.getBoolean("playing_known"))
        assertTrue(o.getBoolean("playing")) // an older server that ignores playing_known still fails safe
    }

    @Test fun searchHintParsesOnOpenAndPicks() {
        val open = parseServerMsg("""{"type":"open","service_label":"Prime Video","search_hint":"Heat","launch":$launch}""") as ServerMsg.Open
        assertEquals("Heat", open.searchHint)
        assertNull((parseServerMsg("""{"type":"open","launch":$launch}""") as ServerMsg.Open).searchHint)
        val json = """{"type":"show","answer_id":"a1","question":"q","headline":"h","subline":"s",
            "picks":[{"token":"t","title":"Heat","service_label":"P","search_hint":"Heat","launch":$launch},
                     {"token":"u","title":"Dune","service_label":"N","search_hint":null,"launch":$launch}]}"""
        val (a, b) = (parseServerMsg(json) as ServerMsg.Show).picks
        assertEquals("Heat", a.searchHint)
        assertNull(b.searchHint)
    }

    @Test fun outgoingMessages() {
        val shown = JSONObject(shownMsg("a1", "notice"))
        assertEquals("shown", shown.getString("type")); assertEquals("notice", shown.getString("mode"))
        assertEquals("t1", JSONObject(openedMsg("t1", "netflix")).getString("token"))
        val r = JSONObject(renderedMsg("a1", 321))
        assertEquals("rendered", r.getString("type")); assertEquals(321, r.getInt("ms"))
    }

    @Test fun backoffDoublesAndCapsAt30s() {
        assertEquals(2_000L, nextBackoff(1_000))
        assertEquals(30_000L, nextBackoff(20_000))
        assertEquals(30_000L, nextBackoff(30_000))
    }

    @Test fun firstShowSurvivesLateSubscriber() {
        val show = parseServerMsg("""{"type":"show","answer_id":"a1","question":"q","headline":"h","subline":"s","picks":[]}""") as ServerMsg.Show
        MessageBus.publish(show) // service publishes before the activity exists
        assertEquals(show, MessageBus.lastShow.value)
    }

    @Test fun consumeForgetsTheAnswerAndLookingSupersedesIt() {
        val show = (parseServerMsg("""{"type":"show","answer_id":"a1","picks":[]}""") as ServerMsg.Show)
        MessageBus.publish(show)
        MessageBus.publish(ServerMsg.Looking("t2", "q"))
        assertNull(MessageBus.lastShow.value)
        assertEquals("q", MessageBus.looking.value?.question)
        MessageBus.publish(show)
        assertNull(MessageBus.looking.value)
        MessageBus.consume()
        assertNull(MessageBus.lastShow.value)
    }

    @Test fun expireLookingClearsOnlyThatTurn() {
        MessageBus.publish(ServerMsg.Looking("t1", "q"))
        MessageBus.expireLooking("other")
        assertEquals("t1", MessageBus.looking.value?.turnId)
        MessageBus.expireLooking("t1")
        assertNull(MessageBus.looking.value)
    }

    @Test fun helloKeptForLateSubscriber() {
        val hello = parseServerMsg("""{"type":"hello","assistant_name":"Lumi","theme":{},"strings":{}}""") as ServerMsg.Hello
        MessageBus.publish(hello)
        assertEquals(hello, MessageBus.hello.value)
    }

    @Test fun jsonNullStringsAreNotTheWordNull() {
        val msg = parseServerMsg("""{"type":"looking","turn_id":"t","question":null}""") as ServerMsg.Looking
        assertEquals("", msg.question)
    }

    @Test fun urlSchemeHandling() {
        assertEquals("http://h:8765/tv", toHttpScheme("ws://h:8765/tv"))
        assertEquals("https://h/tv", toHttpScheme("wss://h/tv"))
        assertTrue(isWebSocketUrl("ws://h/tv"))
        assertTrue(!isWebSocketUrl("http://example.org/x"))
    }
}
