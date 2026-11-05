package io.github.miladaslaner.lumitv.ui

import androidx.compose.ui.graphics.Color
import io.github.miladaslaner.lumitv.LinkState
import org.json.JSONObject

/**
 * Design tokens from the server's `hello` message. [Default] equals the shipped
 * `content/theme.json`, so the screen looks right before `hello` arrives.
 */
data class Theme(
    val bg: Color,
    val panel: List<Color>,
    val border: Color,
    val borderStrong: Color,
    val text: Color,
    val textSecondary: Color,
    val textMuted: Color,
    val green: Color,
    val indigo: Color,
    val headingWeight: Int,
    val headingTracking: Float,
    val radiusCard: Int,
) {
    companion object {
        val Default = Theme(
            bg = Color(0xFF08090A),
            panel = listOf(Color(0xFF0F1011), Color(0xFF151617), Color(0xFF1C1D1F)),
            border = Color(1f, 1f, 1f, 0.08f),
            borderStrong = Color(1f, 1f, 1f, 0.12f),
            text = Color(0xFFF7F8F8),
            textSecondary = Color(0xFFD0D6E0),
            textMuted = Color(0xFF8A8F98),
            green = Color(0xFF4CB782),
            indigo = Color(0xFF7170FF),
            headingWeight = 560,
            headingTracking = -0.035f,
            radiusCard = 12,
        )

        /** Missing or unparsable tokens keep their default. The font is always the bundled Inter. */
        fun from(o: JSONObject?): Theme {
            if (o == null) return Default
            val d = Default
            val panel = o.optJSONArray("panel")?.let { a ->
                (0 until a.length()).map { parseCssColor(a.optString(it)) ?: d.panel.getOrElse(it) { d.panel.last() } }
            }?.takeIf { it.size == 3 } ?: d.panel
            return Theme(
                bg = o.color("bg") ?: d.bg,
                panel = panel,
                border = o.color("border") ?: d.border,
                borderStrong = o.color("border_strong") ?: d.borderStrong,
                text = o.color("text") ?: d.text,
                textSecondary = o.color("text_secondary") ?: d.textSecondary,
                textMuted = o.color("text_muted") ?: d.textMuted,
                green = o.color("green") ?: d.green,
                indigo = o.color("indigo") ?: d.indigo,
                headingWeight = o.optInt("heading_weight", d.headingWeight),
                headingTracking = o.optString("heading_tracking").removeSuffix("em").toFloatOrNull() ?: d.headingTracking,
                radiusCard = o.optInt("radius_card", d.radiusCard),
            )
        }
    }
}

private fun JSONObject.color(key: String): Color? = if (has(key) && !isNull(key)) parseCssColor(getString(key)) else null

private val rgba = Regex("""rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*(?:,\s*([\d.]+)\s*)?\)""")

/** Parses `#rrggbb` and `rgb()/rgba()`; null when unparsable. */
fun parseCssColor(s: String): Color? {
    val v = s.trim()
    if (v.startsWith("#") && v.length == 7) {
        val n = v.substring(1).toLongOrNull(16) ?: return null
        return Color(0xFF000000 or n)
    }
    val m = rgba.matchEntire(v) ?: return null
    val (r, g, b) = m.groupValues.drop(1).take(3).map { it.toInt().coerceIn(0, 255) / 255f }
    val a = m.groupValues[4].toFloatOrNull() ?: 1f
    return Color(r, g, b, a.coerceIn(0f, 1f))
}

/**
 * UI copy from `hello.strings`. The server fills `{assistant}` and `{vpn}` before sending; the defaults here equal
 * the shipped `content/strings.en.json` with "Lumi" and "the VPN" filled in (a test pins that). `{service}`,
 * `{title}` and `{names}` are filled in on the TV.
 */
data class UiStrings(
    val looking: String = "Looking for…",
    val notice: String = "Answer ready · press OK",
    /** Footer hints on the answer screen, each "key: label" (see [hintParts]). */
    val hints: List<String> = listOf("◀ ▶: choose", "OK: open"),
    /** The hint beside them while the "Open in" row is open. */
    val closeRow: String = "▲: close",
    private val openTemplate: String = "Open in {service}",
    private val needsVpnOnTemplate: String = "{service} needs the VPN on",
    private val needsVpnOffTemplate: String = "{service} may not work while the VPN is on",
    /** The idle screen while connected: the question, and how to answer it with the mic entry installed or not. */
    val idleTitle: String = "What do you feel like watching?",
    val idle: String = "Press the mic button and just say it.",
    val idleNoMic: String = "Press OK and type it.",
    /** The idle line while the link is down, was never configured, or the server refused the token. */
    val offline: String = "Lumi can't connect right now. Try again in a few minutes.",
    val notSetUp: String = "Lumi isn't set up yet. Whoever installed it: see the README's TV app setup.",
    val linkRejected: String = "Lumi can't connect: its key was changed. Whoever installed it: send CONFIGURE again.",
    private val cantOpenTemplate: String = "Can't open {service}",
    private val searchHintTemplate: String = "Search for “{title}”",
    private val profileRelaunchTemplate: String = "Pick your profile — Lumi will open “{title}”",
    val profileRetry: String = "Pick your profile, then open Lumi and press OK on the title again",
    val listening: String = "Listening…",
    val tryAgain: String = "Try again",
    val typeInstead: String = "Type instead",
    /** The only pill on an error nothing on the TV can fix: back to the idle screen. */
    val ok: String = "OK",
    val typingHint: String = "What do you feel like watching?",
    val askAgain: String = "Mic: ask again",
    val ask: String = "Ask Lumi",
    /** The tile after the posters, and the question OK on it sends. */
    val more: String = "More picks",
    val moreQuestion: String = "Show me more",
    /** Footer hint on a poster that is on more than one service: ▼ opens the "Open in" row. */
    val otherServices: String = "▼: other services",
    /** The "Who's here" pill and screen. */
    private val watchingTemplate: String = "Watching: {names}",
    val everyone: String = "Everyone",
    val setupViewers: String = "Add your family",
    val viewersTitle: String = "Who's here right now?",
    val viewersSetupLine: String = "Add everyone who watches this TV. For a child, add the age so Lumi keeps picks kid-friendly.",
    val viewersLine: String = "Choose who's watching right now. With a child ticked, Lumi keeps picks kid-friendly.",
    val viewersRemoveLine: String = "Press OK on a person to remove them.",
    val addViewer: String = "+ Add person",
    /** After a person's name on the setup screen when no age was given: "Milad · grown-up". */
    val grownUp: String = "grown-up",
    val removeViewers: String = "Remove someone",
    val doneRemoving: String = "Done",
    val done: String = "Done",
    val viewerHint: String = "Name — and age for a child, like “Emma 6”",
    /** The keyboard's last key while a person is typed (it is "Ask Lumi" while a question is typed). */
    val addKey: String = "Add",
    private val askErrors: Map<String, String> = ASK_ERROR_DEFAULTS,
) {
    /**
     * The line for an ask that ended with [code]: a server `ask_error` code, `offline` (the POST could not reach the
     * server) or a TV-side capture failure ([io.github.miladaslaner.lumitv.captureFailureCode]); unknown reads as `failed`.
     */
    fun askError(code: String): String = askErrors[ASK_ERROR_KEYS[code] ?: "ask_failed"] ?: askErrors.getValue("ask_failed")

    /** The idle screen's line about the link, or null while it is connected (the ask line shows then). */
    fun linkLine(state: LinkState): String? = when (state) {
        LinkState.CONNECTED -> null
        LinkState.DOWN -> offline
        LinkState.NOT_CONFIGURED -> notSetUp
        LinkState.REJECTED -> linkRejected
    }

    fun profileRelaunch(title: String) = profileRelaunchTemplate.replace("{title}", title)
    fun watching(names: String) = watchingTemplate.replace("{names}", names)
    fun searchHint(title: String) = searchHintTemplate.replace("{title}", title)
    fun cantOpen(service: String) = cantOpenTemplate.replace("{service}", service)
    fun open(service: String) = openTemplate.replace("{service}", service)
    fun needsVpnOn(service: String) = needsVpnOnTemplate.replace("{service}", service)
    fun needsVpnOff(service: String) = needsVpnOffTemplate.replace("{service}", service)

    companion object {
        /** Ask error code -> strings key. */
        private val ASK_ERROR_KEYS = mapOf(
            "not_heard" to "didnt_catch", "failed" to "ask_failed", "timeout" to "ask_timeout",
            "login" to "ask_login", "limit" to "ask_limit", "offline" to "ask_offline",
            "not_installed" to "ask_not_installed", "no_speech" to "ask_no_speech",
            "mic_permission" to "ask_mic_permission", "no_recognizer" to "ask_no_recognizer",
        )
        private val ASK_ERROR_DEFAULTS = mapOf(
            "didnt_catch" to "Didn't catch that",
            "ask_failed" to "Lumi couldn't answer that. Try again?",
            "ask_timeout" to "That took too long. Try again?",
            "ask_offline" to "Lumi can't connect right now. Try again in a minute.",
            "ask_login" to "Lumi is logged out. Ask whoever installed it to log it back in.",
            "ask_limit" to "Lumi has used up its turns for now. Try again later.",
            "ask_not_installed" to "Lumi isn't fully set up yet. Ask whoever installed it.",
            "ask_no_speech" to "Lumi can't listen right now. Type instead?",
            "ask_mic_permission" to "Lumi isn't allowed to use the mic. Type instead?",
            "ask_no_recognizer" to "The TV's speech recognition isn't working. Type instead?",
        )

        val Default = UiStrings()

        /** A missing or null key keeps its default, so an older server's `hello` never blanks a line. */
        fun from(o: JSONObject?): UiStrings {
            if (o == null) return Default
            val d = Default
            fun str(key: String, fallback: String) = o.takeIf { it.has(key) && !it.isNull(key) }?.getString(key) ?: fallback
            val hints = o.optJSONArray("hints")?.let { a -> (0 until a.length()).map { a.getString(it) } }
                ?.takeIf { it.isNotEmpty() } ?: d.hints
            return UiStrings(
                looking = str("looking", d.looking),
                notice = str("notice", d.notice),
                hints = hints,
                closeRow = str("close_row", d.closeRow),
                openTemplate = str("open", d.openTemplate),
                needsVpnOnTemplate = str("needs_vpn_on", d.needsVpnOnTemplate),
                needsVpnOffTemplate = str("needs_vpn_off", d.needsVpnOffTemplate),
                idleTitle = str("idle_title", d.idleTitle),
                idle = str("idle", d.idle),
                idleNoMic = str("idle_no_mic", d.idleNoMic),
                offline = str("offline", d.offline),
                notSetUp = str("not_set_up", d.notSetUp),
                linkRejected = str("link_rejected", d.linkRejected),
                cantOpenTemplate = str("cant_open", d.cantOpenTemplate),
                searchHintTemplate = str("search_hint", d.searchHintTemplate),
                profileRelaunchTemplate = str("profile_relaunch", d.profileRelaunchTemplate),
                profileRetry = str("profile_retry", d.profileRetry),
                listening = str("listening", d.listening),
                tryAgain = str("try_again", d.tryAgain),
                typeInstead = str("type_instead", d.typeInstead),
                ok = str("ok", d.ok),
                typingHint = str("typing_hint", d.typingHint),
                askAgain = str("ask_again", d.askAgain),
                ask = str("ask", d.ask),
                more = str("more", d.more),
                moreQuestion = str("more_question", d.moreQuestion),
                otherServices = str("other_services", d.otherServices),
                watchingTemplate = str("watching", d.watchingTemplate),
                everyone = str("everyone", d.everyone),
                setupViewers = str("setup_viewers", d.setupViewers),
                viewersTitle = str("viewers_title", d.viewersTitle),
                viewersSetupLine = str("viewers_setup_line", d.viewersSetupLine),
                viewersLine = str("viewers_line", d.viewersLine),
                viewersRemoveLine = str("viewers_remove_line", d.viewersRemoveLine),
                addViewer = str("add_viewer", d.addViewer),
                grownUp = str("grown_up", d.grownUp),
                removeViewers = str("remove_viewers", d.removeViewers),
                doneRemoving = str("done_removing", d.doneRemoving),
                done = str("done", d.done),
                viewerHint = str("viewer_hint", d.viewerHint),
                addKey = str("add_key", d.addKey),
                askErrors = ASK_ERROR_DEFAULTS.mapValues { (key, fallback) -> str(key, fallback) },
            )
        }
    }
}
