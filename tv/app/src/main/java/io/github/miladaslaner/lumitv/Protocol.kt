package io.github.miladaslaner.lumitv

import org.json.JSONArray
import org.json.JSONException
import org.json.JSONObject

/** Wire protocol with the Lumi TV server; see lumi-tv/protocol.md. */

/** One way to open a title; a pick carries an ordered list of them (`launches`), tried first to last. */
data class Launch(
    val packageName: String,
    val action: String,
    val data: String? = null,
    val extras: Map<String, String> = emptyMap(),
    val vpn: String = "any",
    /** "package/.Activity": the intent goes to exactly that activity. */
    val component: String? = null,
    /** This step lands on the app's empty search screen: show the search hint after it opens. */
    val hint: Boolean = false,
    /** Cold app start: send this step again after these seconds (see [Relauncher]). */
    val relaunchAfterS: List<Double> = emptyList(),
)

/** A poster on the Looking screen: a likely pick, not yet one (nothing to open). */
data class PosterUi(val title: String, val posterUrl: String?) {
    fun asPick(): PickUi = PickUi(token = "", title = title, posterUrl = posterUrl, serviceLabel = "", lengthLabel = null, launches = emptyList())
}

/** One service a pick can open in, with its own launch steps. */
data class Provider(
    val service: String,
    val serviceLabel: String,
    /** Ordered: the first one that starts wins. Never empty. */
    val launches: List<Launch>,
    val searchHint: String? = null,
)

data class PickUi(
    val token: String,
    val title: String,
    val posterUrl: String?,
    val serviceLabel: String,
    val lengthLabel: String?,
    /** Ordered: the first one that starts wins. Never empty. */
    val launches: List<Launch>,
    /** The title to type, when the launch lands on an app's empty search screen. */
    val searchHint: String? = null,
    /** Why Lumi picked it, one short line under the poster. */
    val reason: String? = null,
    /** The service [launches] opens; "" from servers that don't name it. */
    val service: String = "",
    /** Every service the title is on, best first; the first is the one above. Never empty. */
    val providers: List<Provider> = listOf(Provider(service, serviceLabel, launches, searchHint)),
) {
    /** The same pick, opening in [p] instead. */
    fun on(p: Provider): PickUi = copy(service = p.service, serviceLabel = p.serviceLabel, launches = p.launches, searchHint = p.searchHint)
}

/**
 * The server's ask budgets from `hello.limits` (`[ask] timeout_s`, `[ask] max_question_chars`, `[speech] max_audio_s`,
 * `[speech] timeout_s`); a server that sends none gets [DEFAULT]. The TV's deadlines are the server's plus slack, so
 * the TV never gives up on an ask the server would still have answered, and its caps are the server's, so a question
 * the TV lets through is never refused for its length.
 */
data class AskLimits(
    val askTimeoutS: Double = 45.0,
    val maxQuestionChars: Int = 200,
    val maxAudioS: Double = 15.0,
    val speechTimeoutS: Double = 10.0,
) {
    /** The TV gives up on a typed ask this long after sending it: the server's Claude run plus 10 s. */
    val typedTimeoutMs: Long get() = ((askTimeoutS + SLACK_S) * 1000).toLong()

    /** The same for a spoken ask, from the end of speech: the upload and speech-to-text come before the Claude run. */
    val voiceTimeoutMs: Long get() = ((askTimeoutS + speechTimeoutS + SLACK_S) * 1000).toLong()

    /** Where the keyboard stops. */
    val questionChars: Int get() = maxQuestionChars.coerceIn(1, 1000)

    /** Where the remote mic stops recording. */
    val audioMs: Int get() = (maxAudioS * 1000).toInt()

    companion object {
        private const val SLACK_S = 10.0
        val DEFAULT = AskLimits()

        /** A missing or senseless value (absent, zero, negative) keeps its default. */
        fun from(o: JSONObject?): AskLimits {
            val d = DEFAULT
            if (o == null) return d
            fun seconds(key: String, fallback: Double) = o.optDouble(key, fallback).takeIf { it.isFinite() && it > 0 } ?: fallback
            return AskLimits(
                askTimeoutS = seconds("ask_timeout_s", d.askTimeoutS),
                maxQuestionChars = o.optInt("max_question_chars", d.maxQuestionChars).takeIf { it > 0 } ?: d.maxQuestionChars,
                maxAudioS = seconds("max_audio_s", d.maxAudioS),
                speechTimeoutS = seconds("speech_timeout_s", d.speechTimeoutS),
            )
        }
    }
}

sealed interface ServerMsg {
    /** [speech]: the server transcribes spoken questions (`POST /ask/audio`); absent (older servers) is false. */
    data class Hello(
        val assistantName: String,
        val theme: JSONObject,
        val strings: JSONObject,
        val speech: Boolean = false,
        /** Who watches this TV (set up on the TV); kids hours for the "who's watching" default. */
        val viewers: List<Viewer> = emptyList(),
        val kidsFrom: String? = null,
        val kidsUntil: String? = null,
        val limits: AskLimits = AskLimits.DEFAULT,
    ) : ServerMsg
    /** [askId]: the TV ask this turn answers; null for a turn from Claude Desktop. */
    data class Looking(val turnId: String, val question: String, val askId: String? = null) : ServerMsg
    data class Show(
        val answerId: String,
        val question: String,
        val headline: String,
        val subline: String,
        val picks: List<PickUi>,
        /** The TV ask this answers; null for an answer from Claude Desktop. */
        val askId: String? = null,
        /** Why each malformed pick was left out (the missing field), for the log; the rest are shown. */
        val droppedPicks: List<String> = emptyList(),
    ) : ServerMsg
    data class Open(
        val launches: List<Launch>,
        val serviceLabel: String = "",
        val searchHint: String? = null,
        val title: String = "",
    ) : ServerMsg

    /** What the server heard (or was typed) for ask [askId]. */
    data class Heard(val askId: String, val text: String) : ServerMsg

    /** The household's viewers as the server saved them (after `viewers_set`). */
    data class Viewers(val viewers: List<Viewer>) : ServerMsg

    /** The likely posters for ask [askId] while Claude chooses; the answer replaces them. */
    data class Preview(val askId: String, val posters: List<PosterUi>) : ServerMsg

    /**
     * Ask [askId] ended without an answer; [code] is `not_heard`, `failed`, `timeout`, `login`, `limit` or
     * `not_installed`.
     */
    data class AskError(val askId: String, val code: String) : ServerMsg

    /** Lumi answered ask [askId] with a line instead of posters. */
    data class AskReply(val askId: String, val text: String) : ServerMsg
}

/**
 * Returns null for unknown types and for malformed messages. A `show` whose picks are only partly malformed keeps
 * the good ones ([ServerMsg.Show.droppedPicks] says what went); it is null only when none survives.
 */
fun parseServerMsg(json: String): ServerMsg? = try {
    val o = JSONObject(json)
    when (o.getString("type")) {
        "hello" -> ServerMsg.Hello(
            o.optStringOrNull("assistant_name") ?: "Lumi",
            o.optJSONObject("theme") ?: JSONObject(),
            o.optJSONObject("strings") ?: JSONObject(),
            o.optBoolean("speech", false),
            parseViewers(o),
            o.optJSONObject("kids_hours")?.optStringOrNull("from"),
            o.optJSONObject("kids_hours")?.optStringOrNull("until"),
            AskLimits.from(o.optJSONObject("limits")),
        )
        "looking" -> ServerMsg.Looking(o.getString("turn_id"), o.optStringOrNull("question").orEmpty(), o.optStringOrNull("ask_id"))
        "show" -> {
            val dropped = mutableListOf<String>()
            val picks = o.getJSONArray("picks").objects().mapNotNull { p ->
                try {
                    parsePick(p)
                } catch (e: JSONException) {
                    dropped += e.message ?: "malformed pick"
                    null
                }
            }
            if (picks.isEmpty() && dropped.isNotEmpty()) null
            else ServerMsg.Show(
                o.getString("answer_id"),
                o.optStringOrNull("question").orEmpty(),
                o.optStringOrNull("headline").orEmpty(),
                o.optStringOrNull("subline").orEmpty(),
                picks,
                o.optStringOrNull("ask_id"),
                droppedPicks = dropped,
            )
        }
        "open" -> ServerMsg.Open(
            parseLaunches(o),
            o.optStringOrNull("service_label").orEmpty(),
            o.optStringOrNull("search_hint")?.takeIf { it.isNotBlank() },
            o.optStringOrNull("title").orEmpty(),
        )
        "heard" -> ServerMsg.Heard(o.getString("ask_id"), o.optStringOrNull("text").orEmpty())
        "viewers" -> ServerMsg.Viewers(parseViewers(o))
        "preview" -> ServerMsg.Preview(
            o.getString("ask_id"),
            o.optJSONArray("posters")?.objects()?.map { PosterUi(it.getString("title"), it.optStringOrNull("poster_url")) }.orEmpty(),
        )
        "ask_error" -> ServerMsg.AskError(o.getString("ask_id"), o.getString("code"))
        "ask_reply" -> ServerMsg.AskReply(o.getString("ask_id"), o.optStringOrNull("text").orEmpty())
        else -> null
    }
} catch (e: JSONException) {
    null
}

private fun JSONArray.objects(): List<JSONObject> = (0 until length()).map { getJSONObject(it) }

private fun JSONObject.optStringOrNull(key: String): String? =
    if (isNull(key) || !has(key)) null else getString(key)

private fun parseProvider(o: JSONObject) = Provider(
    service = o.optStringOrNull("service").orEmpty(),
    serviceLabel = o.optStringOrNull("service_label").orEmpty(),
    launches = parseLaunches(o),
    searchHint = o.optStringOrNull("search_hint")?.takeIf { it.isNotBlank() },
)

private fun parsePick(o: JSONObject): PickUi {
    val own = parseProvider(o)
    val providers = o.optJSONArray("providers")?.objects()?.map(::parseProvider)?.takeIf { it.isNotEmpty() } ?: listOf(own)
    return PickUi(
        token = o.getString("token"),
        title = o.getString("title"),
        posterUrl = o.optStringOrNull("poster_url"),
        serviceLabel = own.serviceLabel,
        lengthLabel = o.optStringOrNull("length_label"),
        launches = own.launches,
        searchHint = own.searchHint,
        reason = o.optStringOrNull("reason")?.takeIf { it.isNotBlank() },
        service = own.service,
        providers = providers,
    )
}

/**
 * `launches` (the ordered list), else the older single `launch` whose `fallback` chain is the same list; there
 * the search hint belonged to the first step.
 */
private fun parseLaunches(o: JSONObject): List<Launch> {
    o.optJSONArray("launches")?.objects()?.map(::parseLaunch)?.takeIf { it.isNotEmpty() }?.let { return it }
    val chain = generateSequence(o.getJSONObject("launch")) { it.optJSONObject("fallback") }.map(::parseLaunch).toList()
    return chain.mapIndexed { i, l -> l.copy(hint = i == 0) }
}

private fun parseLaunch(o: JSONObject): Launch {
    val extras = o.optJSONObject("extras")
    val relaunch = o.optJSONArray("relaunch_after_s")
    return Launch(
        packageName = o.getString("package"),
        action = o.getString("action"),
        data = o.optStringOrNull("data"),
        extras = extras?.keys()?.asSequence()?.associateWith { extras.getString(it) } ?: emptyMap(),
        vpn = o.optStringOrNull("vpn") ?: "any",
        component = o.optStringOrNull("component"),
        hint = o.optBoolean("hint", false),
        relaunchAfterS = relaunch?.let { a -> (0 until a.length()).map { a.getDouble(it) } } ?: emptyList(),
    )
}

/** [playingKnown] false: playback state can't be read; [playing] is then reported true so older servers fail safe too. */
fun stateMsg(screenOn: Boolean, playing: Boolean, vpn: Boolean, playingKnown: Boolean = true): String =
    JSONObject().put("type", "state").put("screen_on", screenOn).put("playing", playing || !playingKnown).put("vpn", vpn)
        .put("playing_known", playingKnown).toString()

fun shownMsg(answerId: String, mode: String): String =
    JSONObject().put("type", "shown").put("answer_id", answerId).put("mode", mode).toString()

/** [service]: the one it opened in (the pick may be on several); null when unknown. */
fun openedMsg(token: String, service: String?): String =
    JSONObject().put("type", "opened").put("token", token).apply { if (!service.isNullOrBlank()) put("service", service) }.toString()

/** A pick that didn't open on [service] (no app step started): the server stops putting that service first. */
fun openFailedMsg(token: String, service: String): String =
    JSONObject().put("type", "open_failed").put("token", token).put("service", service).toString()

/** Whether something played in the opened app within [PLAY_CHECK_MS] of opening a pick. */
fun playCheckMsg(token: String, service: String, played: Boolean): String =
    JSONObject().put("type", "play_check").put("token", token).put("service", service).put("played", played).toString()

/** Saves who watches this TV (the server answers with `viewers`). */
fun viewersSetMsg(viewers: List<Viewer>): String = JSONObject().put("type", "viewers_set").put(
    "viewers",
    JSONArray().apply { viewers.forEach { v -> put(JSONObject().put("name", v.name).put("age", v.age ?: JSONObject.NULL)) } },
).toString()

private fun parseViewers(o: JSONObject): List<Viewer> =
    o.optJSONArray("viewers")?.objects()?.mapNotNull { v ->
        v.optStringOrNull("name")?.takeIf { it.isNotBlank() }?.let { Viewer(it, if (v.isNull("age")) null else v.optInt("age")) }
    }.orEmpty()

fun renderedMsg(answerId: String, ms: Long, phases: Map<String, Long> = emptyMap()): String {
    val o = JSONObject().put("type", "rendered").put("answer_id", answerId).put("ms", ms)
    if (phases.isNotEmpty()) o.put("phases", JSONObject(phases))
    return o.toString()
}

/** The logcat line for a rendered answer, with its steps when known (the TV check script reads it). */
fun renderLine(answerId: String, ms: Long, phases: Map<String, Long>): String {
    val steps = RenderPhase.ALL.mapNotNull { p -> phases[p]?.let { "$p $it" } }
    return "rendered $answerId in ${ms}ms" + if (steps.isEmpty()) "" else " (${steps.joinToString(", ")})"
}

/** The mic just opened for [askId]: the server pages its speech model in while the user talks. */
fun askWarmMsg(askId: String): String =
    JSONObject().put("type", "ask_warm").put("ask_id", askId).toString()

/** BACK or a newer press: the server stops working on [askId]. */
fun askCancelMsg(askId: String): String =
    JSONObject().put("type", "ask_cancel").put("ask_id", askId).toString()

/** Stage times of one ask, TV clock (see protocol.md); a null stage is left out. */
fun askTimingMsg(askId: String, listenMs: Long?, heardMs: Long?, postersMs: Long?, typedPostersMs: Long?): String =
    JSONObject().put("type", "ask_timing").put("ask_id", askId)
        .apply {
            listenMs?.let { put("listen_ms", it) }
            heardMs?.let { put("heard_ms", it) }
            postersMs?.let { put("posters_ms", it) }
            typedPostersMs?.let { put("typed_posters_ms", it) }
        }.toString()

/** Body of `POST /ask`. [source] is `voice` or `typed`; [answerId] / [focusedToken] name what is on screen. */
/** [viewers]: who is watching (names); empty when the household has none set up. */
fun askRequestJson(
    askId: String, text: String, source: String, answerId: String?, focusedToken: String?, viewers: List<String> = emptyList(),
): String =
    JSONObject().put("ask_id", askId).put("text", text).put("source", source)
        .apply {
            answerId?.let { put("answer_id", it) }
            focusedToken?.let { put("focused_token", it) }
            if (viewers.isNotEmpty()) put("viewers", JSONArray(viewers))
        }.toString()
