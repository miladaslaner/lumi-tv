package io.github.miladaslaner.lumitv.ui

import androidx.compose.animation.core.RepeatMode
import androidx.compose.animation.core.animateFloat
import androidx.compose.animation.core.infiniteRepeatable
import androidx.compose.animation.core.rememberInfiniteTransition
import androidx.compose.animation.core.tween
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.focusable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.BoxWithConstraints
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ExperimentalLayoutApi
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.aspectRatio
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.BasicText
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateListOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.runtime.snapshotFlow
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.focus.FocusRequester
import androidx.compose.ui.focus.focusRequester
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.input.key.Key
import androidx.compose.ui.input.key.KeyEventType
import androidx.compose.ui.input.key.key
import androidx.compose.ui.input.key.onPreviewKeyEvent
import androidx.compose.ui.input.key.type
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.Font
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.TextUnit
import androidx.compose.ui.unit.dp
import coil.compose.AsyncImage
import coil.compose.AsyncImagePainter
import io.github.miladaslaner.lumitv.AskLimits
import io.github.miladaslaner.lumitv.AskState
import io.github.miladaslaner.lumitv.LinkState
import io.github.miladaslaner.lumitv.PickUi
import io.github.miladaslaner.lumitv.R
import io.github.miladaslaner.lumitv.ServerMsg
import io.github.miladaslaner.lumitv.ViewersModel
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.withTimeoutOrNull

val Inter = FontFamily(
    Font(R.font.inter_regular, FontWeight.Normal),
    Font(R.font.inter_medium, FontWeight.Medium),
    Font(R.font.inter_semibold, FontWeight.SemiBold),
)

/** What the screen shows. */
sealed interface Screen {
    data object Idle : Screen
    /** [posters]: the likely picks, once the server has them (shown without focus; the answer replaces them). */
    data class Looking(val question: String, val posters: List<PickUi>? = null) : Screen
    data class Answer(val show: ServerMsg.Show) : Screen
    /** The mic is open; [partial] is what was heard so far. */
    data class Listening(val partial: String?) : Screen
    /** The ask ended without an answer: [message] for its [code] (see [actions] for what it offers). */
    data class AskFailed(val message: String, val code: String) : Screen
    /** Lumi answered with a line instead of posters. */
    data class Reply(val text: String) : Screen
    data class Typing(val model: KeyboardModel) : Screen
    /** "Who's watching" (and setting up who watches this TV). */
    data class Viewers(val model: ViewersModel) : Screen
}

/** A focusable pill on the ask screens. [OK] acknowledges an error nothing on the TV can fix. */
enum class AskAction { ASK, TRY_AGAIN, TYPE_INSTEAD, OK, WATCHING, SETUP_VIEWERS }

/**
 * The pills [this] screen offers, left to right (◀ ▶ move between them, OK runs one). [hasViewers]: the household
 * has people set up; until then Lumi's idle screen offers setting them up first. A failure offers only what can
 * work: no retry when the server is logged out, not installed or out of turns; typing when the mic is the problem.
 */
fun Screen.actions(hasViewers: Boolean = true): List<AskAction> = when (this) {
    Screen.Idle -> if (hasViewers) listOf(AskAction.ASK, AskAction.WATCHING) else listOf(AskAction.SETUP_VIEWERS, AskAction.ASK)
    is Screen.AskFailed -> when (code) {
        "login", "not_installed", "limit" -> listOf(AskAction.OK)
        "no_speech", "mic_permission", "no_recognizer" -> listOf(AskAction.TYPE_INSTEAD)
        else -> listOf(AskAction.TRY_AGAIN, AskAction.TYPE_INSTEAD) // not_heard, failed, timeout, offline
    }
    is Screen.Reply -> listOf(AskAction.TYPE_INSTEAD)
    is Screen.Answer, is Screen.Looking, is Screen.Listening, is Screen.Typing, is Screen.Viewers -> emptyList()
}

/**
 * What to show for the TV's own [ask], the latest [show] and a Claude Desktop [looking]. The ask wins over Claude Desktop's
 * `looking`; with no ask (or once it is answered) the screen is Claude Desktop's. [sentText] is what was just typed for the ask
 * being sent, shown as "Looking for …" until the server says what it heard.
 */
fun screenFor(
    ask: AskState,
    show: ServerMsg.Show?,
    looking: ServerMsg.Looking?,
    strings: UiStrings,
    sentText: String? = null,
    preview: ServerMsg.Preview? = null,
): Screen = when (ask) {
    AskState.Idle, is AskState.Answered -> when {
        show != null -> Screen.Answer(show)
        // An ask's own `looking` belongs to that ask; once it is over it must not come back as "Looking for …".
        looking != null && looking.askId == null -> Screen.Looking(looking.question)
        else -> Screen.Idle
    }
    is AskState.Listening -> Screen.Listening(ask.partial)
    is AskState.Sending -> Screen.Looking(
        looking?.takeIf { it.askId == ask.askId }?.question ?: sentText.orEmpty(), posters(preview, ask.askId),
    )
    is AskState.Looking -> Screen.Looking(ask.question, posters(preview, ask.askId))
    is AskState.Typing -> Screen.Typing(KeyboardModel(ask.text))
    is AskState.Failed -> Screen.AskFailed(strings.askError(ask.code), ask.code)
    is AskState.Reply -> Screen.Reply(ask.text)
}

private fun posters(preview: ServerMsg.Preview?, askId: String): List<PickUi>? =
    preview?.takeIf { it.askId == askId && it.posters.isNotEmpty() }?.posters?.map { it.asPick() }

/**
 * A footer hint "key: label" split into its boxed key and its label: "OK: open" -> ("OK", "open"). The strings file
 * writes every hint in that form; one without it is a label alone (no key box).
 */
fun hintParts(hint: String): Pair<String, String> {
    val at = hint.indexOf(": ")
    return if (at < 0) "" to hint else hint.substring(0, at) to hint.substring(at + 2)
}

/** Layout scale: every size below is written in pixels of the 1920x1080 mockup and scaled to the real width. */
internal class Scale(val unit: Dp, private val density: androidx.compose.ui.unit.Density) {
    fun dp(px: Float): Dp = unit * px
    fun sp(px: Float): TextUnit = with(density) { (unit * px).toSp() }
}

private const val RENDER_TIMEOUT_MS = 1_500L

/**
 * The answer screen: headline left, three posters right. [onRendered] fires once per answer, after
 * the first frame with all posters loaded (or after 1.5 s, with placeholders for what is missing).
 * The ask screens use the same layout: the line where the headline goes, pills or the keyboard under it.
 * [voiceEntry]: the mic button asks Lumi (the idle line and the answer's hints say so). [onDrawn] fires after the
 * first frame of each screen; [onFocusedPick] with the focused poster's token on the answer screen (null on the
 * "More like these" tile, [answerSlots]). [onMore]: OK on that tile.
 */
@Composable
fun EditorialScreen(
    screen: Screen,
    assistantName: String,
    theme: Theme,
    strings: UiStrings,
    notice: String?,
    onOpen: (PickUi) -> Unit,
    onNavigate: () -> Unit,
    onRendered: (answerId: String) -> Unit,
    voiceEntry: Boolean = false,
    onAction: (AskAction) -> Unit = {},
    onSubmit: (String) -> Unit = {},
    /** BACK on the question keyboard: leave it for the screen underneath (the answer stays). */
    onDismiss: () -> Unit = {},
    /** Where the keyboard stops: the server's limit on a question. */
    maxChars: Int = AskLimits.DEFAULT.questionChars,
    onDrawn: (Screen) -> Unit = {},
    onFocusedPick: (String?) -> Unit = {},
    onMore: () -> Unit = {},
    link: LinkState = LinkState.CONNECTED,
    watching: String? = null,
    onViewersKey: (Key, Boolean) -> Boolean = { _, _ -> false },
) {
    BoxWithConstraints(Modifier.fillMaxSize().background(theme.bg)) {
        val sc = Scale(maxWidth / 1920f, LocalDensity.current)
        val glow = Brush.radialGradient(
            colors = listOf(Color.White.copy(alpha = 0.06f), Color.Transparent),
            center = androidx.compose.ui.geometry.Offset(0.3f * constraints.maxWidth, 0f),
            radius = 0.6f * constraints.maxWidth,
        )
        Box(Modifier.fillMaxSize().background(glow))

        val picks = (screen as? Screen.Answer)?.show?.picks.orEmpty()
        val slots = answerSlots(picks.size)
        var focused by remember(screen) { mutableIntStateOf(0) }
        val onMoreTile = picks.isNotEmpty() && focused == picks.size
        // ▼ on a poster that is on several services: the "Open in" row, one pill per service (best first)
        var choosing by remember(screen) { mutableStateOf(false) }
        var choice by remember(screen) { mutableIntStateOf(0) }
        val focusedProviders = picks.getOrNull(focused)?.providers.orEmpty()
        val actions = screen.actions(hasViewers = watching != null)
        var action by remember(screen) { mutableIntStateOf(0) }
        var keyboard by remember(screen) { mutableStateOf((screen as? Screen.Typing)?.model ?: KeyboardModel()) }
        val focusRequester = remember { FocusRequester() }
        LaunchedEffect(Unit) { focusRequester.requestFocus() }
        LaunchedEffect(screen) {
            androidx.compose.runtime.withFrameNanos { }
            onDrawn(screen)
        }
        if (screen is Screen.Answer) LaunchedEffect(screen, focused) { onFocusedPick(picks.getOrNull(focused)?.token) }

        Box(
            Modifier.fillMaxSize()
                .focusRequester(focusRequester)
                .focusable()
                .onPreviewKeyEvent { e ->
                    if (e.type != KeyEventType.KeyDown) return@onPreviewKeyEvent false
                    val firstPress = e.nativeKeyEvent.repeatCount == 0
                    if (screen is Screen.Viewers) return@onPreviewKeyEvent onViewersKey(e.key, firstPress)
                    if (screen is Screen.Typing) {
                        when (e.key) {
                            Key.DirectionLeft -> keyboard = keyboard.move(-1, 0)
                            Key.DirectionRight -> keyboard = keyboard.move(1, 0)
                            Key.DirectionUp -> keyboard = keyboard.move(0, -1)
                            Key.DirectionDown -> keyboard = keyboard.move(0, 1)
                            Key.Backspace, Key.Delete -> keyboard = keyboard.delete()
                            Key.DirectionCenter, Key.Enter, Key.NumPadEnter -> {
                                val (next, question) = keyboard.press(maxChars)
                                keyboard = next
                                // A held OK repeats letters, but sends only once.
                                if (question != null && firstPress) onSubmit(question)
                            }
                            // BACK closes the keyboard like any TV keyboard; it never quits Lumi from here.
                            Key.Back -> onDismiss()
                            else -> return@onPreviewKeyEvent false
                        }
                        return@onPreviewKeyEvent true
                    }
                    if (actions.isNotEmpty()) {
                        return@onPreviewKeyEvent when (e.key) {
                            Key.DirectionLeft -> { action = (action - 1).coerceAtLeast(0); true }
                            Key.DirectionRight -> { action = (action + 1).coerceAtMost(actions.lastIndex); true }
                            Key.DirectionCenter, Key.Enter, Key.NumPadEnter -> { if (firstPress) onAction(actions[action]); true }
                            else -> false
                        }
                    }
                    if (choosing) {
                        when (e.key) {
                            Key.DirectionLeft -> choice = (choice - 1).coerceAtLeast(0)
                            Key.DirectionRight -> choice = (choice + 1).coerceAtMost(focusedProviders.lastIndex)
                            Key.DirectionCenter, Key.Enter, Key.NumPadEnter -> if (firstPress) {
                                picks.getOrNull(focused)?.let { p -> focusedProviders.getOrNull(choice)?.let { onOpen(p.on(it)) } }
                            }
                            Key.DirectionUp, Key.Back -> choosing = false
                            else -> Unit
                        }
                        return@onPreviewKeyEvent true // the row has the remote until it closes
                    }
                    when (e.key) {
                        Key.DirectionDown -> {
                            if (focusedProviders.size > 1) { choosing = true; choice = 0 }
                            focusedProviders.size > 1
                        }
                        Key.DirectionLeft -> { if (picks.isNotEmpty()) focused = (focused - 1).coerceAtLeast(0); onNavigate(); true }
                        Key.DirectionRight -> { if (picks.isNotEmpty()) focused = (focused + 1).coerceAtMost(slots - 1); onNavigate(); true }
                        Key.DirectionCenter, Key.Enter, Key.NumPadEnter -> {
                            // Held OK auto-repeats; only the first press opens.
                            if (e.nativeKeyEvent.repeatCount == 0) {
                                if (onMoreTile) onMore() else picks.getOrNull(focused)?.let(onOpen)
                            }
                            picks.isNotEmpty()
                        }
                        else -> false
                    }
                },
        ) {
            TopBar(sc, theme, assistantName, question = (screen as? Screen.Answer)?.show?.question.orEmpty(), connected = link == LinkState.CONNECTED)
            val pills = actions.map { a ->
                when (a) {
                    AskAction.ASK -> strings.ask
                    AskAction.TRY_AGAIN -> strings.tryAgain
                    AskAction.TYPE_INSTEAD -> strings.typeInstead
                    AskAction.OK -> strings.ok
                    AskAction.WATCHING -> strings.watching(watching.orEmpty())
                    AskAction.SETUP_VIEWERS -> strings.setupViewers
                }
            }
            when (screen) {
                is Screen.Answer -> {
                    // Narrower than the other screens: the "More like these" tile takes the room on the right.
                    Headline(sc, theme, screen.show.headline, screen.show.subline, widthPx = 680f)
                    Posters(sc, theme, screen.show.picks, focused, screen.show.answerId, onRendered, more = strings.more)
                }
                is Screen.Looking -> {
                    Headline(sc, theme, strings.looking, screen.question, subMuted = true)
                    Posters(sc, theme, screen.posters, -1, null, onRendered) // no focus: nothing to open yet
                }
                Screen.Idle -> {
                    // connected: the question and how to answer it; otherwise the assistant's name over what is wrong
                    val linkLine = strings.linkLine(link)
                    Headline(
                        sc, theme,
                        headline = if (linkLine == null) strings.idleTitle else assistantName,
                        sub = linkLine ?: if (voiceEntry) strings.idle else strings.idleNoMic,
                        pills = pills, pillFocus = action,
                    )
                }
                is Screen.Listening -> Headline(sc, theme, strings.listening, screen.partial.orEmpty(), subMuted = true, live = true)
                is Screen.AskFailed -> Headline(sc, theme, screen.message, "", pills = pills, pillFocus = action)
                // No posters beside a reply: the line gets the full width.
                is Screen.Reply -> Headline(sc, theme, screen.text, "", pills = pills, pillFocus = action, widthPx = 1600f, maxLines = 3)
                is Screen.Typing -> KeyboardPanel(sc, theme, strings, keyboard, maxChars = maxChars)
                is Screen.Viewers -> ViewersPanel(sc, theme, strings, screen.model)
            }
            // While the "Open in" row is open the hints say how to leave it; otherwise how to ask and open.
            val hints = if (choosing) strings.hints + strings.closeRow
            else (if (voiceEntry) strings.hints + strings.askAgain else strings.hints) +
                listOfNotNull(strings.otherServices.takeIf { focusedProviders.size > 1 })
            Footer(
                sc, theme, hints, strings, notice, service = picks.getOrNull(focused)?.serviceLabel?.takeIf { it.isNotBlank() },
                showHints = picks.isNotEmpty(),
                choices = if (choosing) focusedProviders.map { strings.open(it.serviceLabel) } else null, choiceFocus = choice,
            )
        }
    }
}

/** The status pill: its dot is green only while the server link is up. */
@Composable
private fun TopBar(sc: Scale, theme: Theme, name: String, question: String, connected: Boolean) {
    Row(
        Modifier.fillMaxWidth().padding(start = sc.dp(86f), end = sc.dp(86f), top = sc.dp(60f)),
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.SpaceBetween,
    ) {
        Pill(sc, theme) {
            Box(Modifier.size(sc.dp(12f)).clip(CircleShape).background(if (connected) theme.green else theme.textMuted))
            Text(sc, theme, name, 26f, theme.textSecondary)
        }
        if (question.isNotBlank()) {
            Box(Modifier.padding(start = sc.dp(48f)).weight(1f)) {
                Text(sc, theme, "“$question”", 28f, theme.textMuted, maxLines = 1, align = TextAlign.End, modifier = Modifier.fillMaxWidth())
            }
        }
    }
}

/**
 * The headline column. [pills] are focusable (the one at [pillFocus] is drawn inverted, so focus reads from the
 * couch); [live] adds a pulsing dot (the mic is open).
 */
@Composable
private fun Headline(
    sc: Scale, theme: Theme, headline: String, sub: String, subMuted: Boolean = false,
    pills: List<String> = emptyList(), pillFocus: Int = 0, live: Boolean = false,
    widthPx: Float = 800f, maxLines: Int = 2,
) {
    Column(Modifier.padding(start = sc.dp(86f), top = sc.dp(190f)).width(sc.dp(widthPx))) {
        if (live) LiveDot(sc, theme)
        FitHeadline(sc, theme, headline, if (subMuted) theme.textMuted else theme.text, maxLines)
        if (sub.isNotBlank()) {
            Text(
                sc, theme, sub, 32f, theme.textMuted, maxLines = 4,
                modifier = Modifier.padding(top = sc.dp(30f)),
            )
        }
        if (pills.isNotEmpty()) {
            Row(Modifier.padding(top = sc.dp(56f)), horizontalArrangement = Arrangement.spacedBy(sc.dp(20f))) {
                pills.forEachIndexed { i, label -> ActionPill(sc, theme, label, focused = i == pillFocus) }
            }
        }
    }
}

@Composable
private fun LiveDot(sc: Scale, theme: Theme) {
    val alpha by rememberInfiniteTransition(label = "live").animateFloat(
        initialValue = 0.3f, targetValue = 1f,
        animationSpec = infiniteRepeatable(tween(700), RepeatMode.Reverse), label = "alpha",
    )
    Box(Modifier.padding(bottom = sc.dp(28f)).size(sc.dp(28f)).clip(CircleShape).background(theme.green.copy(alpha = alpha)))
}

@Composable
private fun ActionPill(sc: Scale, theme: Theme, label: String, focused: Boolean) {
    Row(
        Modifier.clip(CircleShape)
            .background(if (focused) theme.text else theme.panel[1])
            .border(sc.dp(if (focused) 3f else 1.5f), if (focused) theme.text else theme.borderStrong, CircleShape)
            .padding(horizontal = sc.dp(36f), vertical = sc.dp(16f)),
    ) {
        Text(sc, theme, label, 32f, if (focused) theme.bg else theme.textSecondary, weight = FontWeight.Medium, maxLines = 1)
    }
}

/** Headline, [maxLines] at most: steps the font down until it fits, ellipsizes at the smallest size. */
@Composable
private fun FitHeadline(sc: Scale, theme: Theme, text: String, color: Color, maxLines: Int = 2) {
    val sizes = listOf(84f, 72f, 62f, 54f, 46f, 40f)
    var step by remember(text) { mutableIntStateOf(0) }
    BasicText(
        text = text,
        maxLines = maxLines,
        overflow = TextOverflow.Ellipsis,
        style = TextStyle(
            fontFamily = Inter,
            fontWeight = FontWeight(theme.headingWeight),
            fontSize = sc.sp(sizes[step]),
            letterSpacing = sc.sp(sizes[step] * theme.headingTracking),
            lineHeight = sc.sp(sizes[step] * 1.04f),
            color = color,
        ),
        onTextLayout = { if (it.hasVisualOverflow && step < sizes.lastIndex) step++ },
    )
}

@Composable
private fun Posters(
    sc: Scale,
    theme: Theme,
    picks: List<PickUi>?,
    focused: Int,
    answerId: String?,
    onRendered: (String) -> Unit,
    more: String? = null,
) {
    val count = picks?.size ?: 3
    // Which posters have finished (loaded, failed, or have no URL). `null` picks = looking screen.
    val ready = remember(answerId) { mutableStateListOf(*Array(count) { picks?.get(it)?.posterUrl == null }) }
    if (answerId != null) {
        LaunchedEffect(answerId) {
            withTimeoutOrNull(RENDER_TIMEOUT_MS) { snapshotFlow { ready.all { it } }.first { it } }
            androidx.compose.runtime.withFrameNanos { }
            onRendered(answerId)
        }
    }
    Box(Modifier.fillMaxSize().padding(end = sc.dp(74f), top = sc.dp(150f)), contentAlignment = Alignment.TopEnd) {
        Row(horizontalArrangement = Arrangement.spacedBy(sc.dp(14f))) {
            repeat(count) { i ->
                val pick = picks?.get(i)
                Column(Modifier.width(sc.dp(293f)).padding(sc.dp(12f))) {
                    val outline = if (i == focused && pick != null) theme.text else Color.Transparent
                    Box(
                        Modifier.fillMaxWidth().aspectRatio(2f / 3f)
                            .border(sc.dp(5f), outline, RoundedCornerShape(sc.dp(theme.radiusCard + 8f)))
                            .padding(sc.dp(9f)),
                    ) {
                        Poster(sc, theme, pick, onDone = { if (i < ready.size) ready[i] = true })
                    }
                    if (pick != null) {
                        // The lower half of the screen is empty, so the card text takes the room: a title that is
                        // not cut after one line, and the reason (why Lumi picked it) at a size that reads from a couch.
                        Text(sc, theme, pick.title, 32f, theme.text, weight = FontWeight.Medium, maxLines = 2, modifier = Modifier.padding(top = sc.dp(18f)))
                        // every service it is on (OK opens the first), then its length
                        val on = pick.providers.map { it.serviceLabel }.filter { it.isNotBlank() }.joinToString(" · ")
                        val meta = listOfNotNull(on.takeIf { it.isNotEmpty() }, pick.lengthLabel).joinToString(" · ")
                        if (meta.isNotEmpty()) Text(sc, theme, meta, 26f, theme.textMuted, maxLines = 2, modifier = Modifier.padding(top = sc.dp(6f)))
                        pick.reason?.let {
                            Text(sc, theme, it, 28f, theme.textSecondary, maxLines = 3, modifier = Modifier.padding(top = sc.dp(10f)))
                        }
                    }
                }
            }
            if (picks != null && picks.isNotEmpty() && more != null) MoreTile(sc, theme, more, focused = focused == picks.size)
        }
    }
}

/** Focus slots on the answer screen: the posters, then the "More like these" tile. */
fun answerSlots(picks: Int): Int = if (picks == 0) 0 else picks + 1

/** As tall as a poster, narrow: OK asks for the next picks like these. */
@Composable
private fun MoreTile(sc: Scale, theme: Theme, label: String, focused: Boolean) {
    Column(Modifier.width(sc.dp(150f)).padding(sc.dp(12f))) {
        val outer = RoundedCornerShape(sc.dp(theme.radiusCard + 8f))
        val inner = RoundedCornerShape(sc.dp(theme.radiusCard.toFloat()))
        Box(
            Modifier.width(sc.dp(126f)).height(sc.dp(404f))
                .border(sc.dp(5f), if (focused) theme.text else Color.Transparent, outer)
                .padding(sc.dp(9f)),
        ) {
            Column(
                Modifier.fillMaxSize().clip(inner).background(theme.panel[1]).border(sc.dp(1f), theme.borderStrong, inner)
                    .padding(sc.dp(14f)),
                verticalArrangement = Arrangement.Center,
                horizontalAlignment = Alignment.CenterHorizontally,
            ) {
                Text(sc, theme, "›", 72f, if (focused) theme.text else theme.textSecondary, weight = FontWeight.Medium, maxLines = 1)
                Text(
                    sc, theme, label, 24f, if (focused) theme.text else theme.textSecondary, weight = FontWeight.Medium,
                    maxLines = 4, align = TextAlign.Center, modifier = Modifier.padding(top = sc.dp(12f)),
                )
            }
        }
    }
}

@Composable
private fun Poster(sc: Scale, theme: Theme, pick: PickUi?, onDone: () -> Unit) {
    val shape = RoundedCornerShape(sc.dp(theme.radiusCard.toFloat()))
    val base = Modifier.fillMaxSize().clip(shape).border(sc.dp(1f), theme.border, shape)
    if (pick == null) {
        val alpha by rememberInfiniteTransition(label = "shimmer").animateFloat(
            initialValue = 0.35f, targetValue = 1f,
            animationSpec = infiniteRepeatable(tween(900), RepeatMode.Reverse), label = "alpha",
        )
        Box(base.background(theme.panel[1].copy(alpha = alpha)))
        return
    }
    Box(base.background(theme.panel[1])) {
        var failed by remember(pick.token) { mutableStateOf(pick.posterUrl == null) }
        if (pick.posterUrl != null) {
            AsyncImage(
                model = pick.posterUrl,
                contentDescription = pick.title,
                contentScale = ContentScale.Crop,
                modifier = Modifier.fillMaxSize(),
                onState = { s ->
                    when (s) {
                        is AsyncImagePainter.State.Success -> onDone()
                        is AsyncImagePainter.State.Error -> { failed = true; onDone() }
                        else -> Unit
                    }
                },
            )
        }
        if (failed) {
            Box(Modifier.fillMaxSize().background(Brush.verticalGradient(listOf(theme.panel[2], theme.panel[0]))))
            Text(
                sc, theme, pick.title, 30f, theme.text, weight = FontWeight.SemiBold, maxLines = 4,
                modifier = Modifier.align(Alignment.BottomStart).padding(sc.dp(22f)),
            )
        }
    }
}

/**
 * The hints bottom-left and, bottom-right, the notice, the focused pick's "Open in …" pill, or the "Open in" row
 * ([choices], one pill per service, wrapping when there are many).
 */
@OptIn(ExperimentalLayoutApi::class)
@Composable
private fun androidx.compose.foundation.layout.BoxScope.Footer(
    sc: Scale, theme: Theme, hints: List<String>, strings: UiStrings, notice: String?, service: String?, showHints: Boolean,
    choices: List<String>? = null, choiceFocus: Int = 0,
) {
    Row(
        Modifier.align(Alignment.BottomCenter).fillMaxWidth()
            .padding(start = sc.dp(86f), end = sc.dp(86f), bottom = sc.dp(56f)),
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.SpaceBetween,
    ) {
        Row(horizontalArrangement = Arrangement.spacedBy(sc.dp(28f)), verticalAlignment = Alignment.CenterVertically) {
            if (showHints) hints.forEach { hint ->
                val (key, label) = hintParts(hint)
                Row(horizontalArrangement = Arrangement.spacedBy(sc.dp(10f)), verticalAlignment = Alignment.CenterVertically) {
                    if (key.isNotEmpty()) {
                        val shape = RoundedCornerShape(sc.dp(8f))
                        Box(Modifier.border(sc.dp(1f), theme.borderStrong, shape).padding(horizontal = sc.dp(12f), vertical = sc.dp(4f))) {
                            Text(sc, theme, key, 26f, theme.textMuted)
                        }
                    }
                    Text(sc, theme, label, 28f, theme.textMuted)
                }
            }
        }
        when {
            choices != null -> FlowRow(
                Modifier.weight(1f).padding(start = sc.dp(40f)),
                horizontalArrangement = Arrangement.spacedBy(sc.dp(20f), Alignment.End),
                verticalArrangement = Arrangement.spacedBy(sc.dp(16f)),
            ) {
                choices.forEachIndexed { i, label -> ActionPill(sc, theme, label, focused = i == choiceFocus) }
            }
            notice != null -> Pill(sc, theme, border = theme.indigo) { Text(sc, theme, notice, 28f, theme.text, weight = FontWeight.Medium) }
            service != null -> Pill(sc, theme) { Text(sc, theme, strings.open(service), 28f, theme.textSecondary) }
        }
    }
}

@Composable
private fun Pill(sc: Scale, theme: Theme, border: Color? = null, content: @Composable () -> Unit) {
    Row(
        Modifier.clip(CircleShape)
            .background(theme.panel[0].copy(alpha = 0.9f))
            .border(sc.dp(1.5f), border ?: theme.borderStrong, CircleShape)
            .padding(horizontal = sc.dp(26f), vertical = sc.dp(10f)),
        horizontalArrangement = Arrangement.spacedBy(sc.dp(12f)),
        verticalAlignment = Alignment.CenterVertically,
    ) { content() }
}

@Composable
internal fun Text(
    sc: Scale, theme: Theme, text: String, sizePx: Float, color: Color,
    modifier: Modifier = Modifier, weight: FontWeight = FontWeight.Normal, maxLines: Int = Int.MAX_VALUE,
    align: TextAlign = TextAlign.Start,
) {
    BasicText(
        text = text, modifier = modifier, maxLines = maxLines, overflow = TextOverflow.Ellipsis,
        style = TextStyle(fontFamily = Inter, fontWeight = weight, fontSize = sc.sp(sizePx), color = color, textAlign = align, lineHeight = sc.sp(sizePx * 1.4f)),
    )
}
