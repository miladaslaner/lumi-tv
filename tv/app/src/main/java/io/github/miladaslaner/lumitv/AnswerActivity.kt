package io.github.miladaslaner.lumitv

import android.annotation.SuppressLint
import android.os.Bundle
import android.util.Log
import android.view.KeyEvent
import androidx.activity.ComponentActivity
import androidx.activity.OnBackPressedCallback
import androidx.activity.compose.setContent
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import io.github.miladaslaner.lumitv.ui.move
import io.github.miladaslaner.lumitv.ui.delete
import java.time.LocalTime
import androidx.compose.ui.input.key.Key
import kotlinx.coroutines.delay
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import io.github.miladaslaner.lumitv.ui.AskAction
import io.github.miladaslaner.lumitv.ui.EditorialScreen
import io.github.miladaslaner.lumitv.ui.Screen
import io.github.miladaslaner.lumitv.ui.Theme
import io.github.miladaslaner.lumitv.ui.UiStrings
import io.github.miladaslaner.lumitv.ui.screenFor

/** How long the server link may be down before Lumi's idle screen says so. */
const val OFFLINE_AFTER_MS = 10_000L

/**
 * The full-screen answer: Looking, the three posters, or an idle line when opened from the launcher
 * with no current answer. A `show` is rendered only if it is newer (by answer_id) than the last one
 * this process handled, and BACK consumes it so reopening Lumi never replays a stale answer.
 *
 * Also the TV's own ask ([AskController]): listening, typing, "Looking for …", errors and replies. BACK ends the ask and
 * plays again what the press paused; leaving any other way (HOME, another app, the screensaver, the screen turning
 * off) or opening a pick ends it and leaves that paused. BACK on the question keyboard only closes the keyboard.
 * A mic press arrives through [AssistEntry] (which brings this screen to the front, singleTop) or, while this screen
 * is in front, as a mic key.
 */
class AnswerActivity : ComponentActivity() {
    private var ownShowId: String? = null
    private var voiceEntry by mutableStateOf(false)

    /** What was just typed for ask (id, text): "Looking for …" shows it until the server says what it heard. */
    private var sent by mutableStateOf<Pair<String, String>?>(null)

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        MessageBus.markRender(RenderPhase.CREATED)
        LinkService.ensureRunning(this)
        onBackPressedDispatcher.addCallback(this, object : OnBackPressedCallback(true) {
            override fun handleOnBackPressed() {
                AskController.dispatch(AskEvent.Back)
                AskController.focusedToken = null
                MessageBus.consume()
                finish()
            }
        })
        setContent {
            val hello by MessageBus.hello.collectAsState()
            val theme = remember(hello) { Theme.from(hello?.theme) }
            val strings = remember(hello) { UiStrings.from(hello?.strings) }
            val show by MessageBus.lastShow.collectAsState()
            val looking by MessageBus.looking.collectAsState()
            val notice by MessageBus.notice.collectAsState()
            val ask by AskController.state.collectAsState()
            // the link drops for a moment on every wake: a drop shows only after OFFLINE_AFTER_MS, the other states at once
            val linkState by MessageBus.linkState.collectAsState()
            var link by remember { mutableStateOf(if (linkState == LinkState.DOWN) LinkState.CONNECTED else linkState) }
            LaunchedEffect(linkState) {
                if (linkState == LinkState.DOWN) delay(OFFLINE_AFTER_MS)
                link = linkState
            }

            val current = show?.takeIf { it.answerId == ownShowId || it.answerId != handledShowId }
            LaunchedEffect(current?.answerId) {
                val s = current ?: return@LaunchedEffect
                if (ownShowId != s.answerId) {
                    ownShowId = s.answerId
                    handledShowId = s.answerId
                }
            }
            val sentText = sent?.takeIf { it.first == ask.askId }?.second
            val preview by MessageBus.preview.collectAsState()
            val people by Viewers.people.collectAsState()
            val viewersOpen = viewersUi
            // "Who's watching" only over Lumi's idle or answer screen, never over an ask in progress
            val askIdle = ask == AskState.Idle || ask is AskState.Answered
            val screen = if (viewersOpen != null && askIdle) Screen.Viewers(viewersOpen)
            else screenFor(ask, current, looking, strings, sentText, preview)
            val watchingLabel = if (people.isEmpty()) null
            else watchingLabel(people, watchingNow(), strings.everyone)
            EditorialScreen(
                screen = screen,
                assistantName = hello?.assistantName ?: "Lumi",
                theme = theme,
                strings = strings,
                notice = notice,
                onOpen = { pick -> open(pick, strings) },
                onNavigate = { MessageBus.setNotice(null) },
                onRendered = { id -> reportRendered(id) },
                voiceEntry = voiceEntry,
                onAction = ::onAction,
                onSubmit = ::submit,
                onDismiss = { AskController.dispatch(AskEvent.Dismiss) },
                maxChars = (hello?.limits ?: AskLimits.DEFAULT).questionChars,
                onDrawn = ::drawn,
                onFocusedPick = { AskController.focusedToken = it },
                onMore = { more(strings.moreQuestion) },
                link = link,
                watching = watchingLabel,
                onViewersKey = ::viewersKey,
            )
        }
    }

    /** A mic key while Lumi is in front (when the TV hands the key to the app rather than to an assistant). */
    @SuppressLint("RestrictedApi") // androidx.core marks ComponentActivity.dispatchKeyEvent restricted; overriding it is the documented way
    override fun dispatchKeyEvent(event: KeyEvent): Boolean {
        if (event.keyCode in AssistEntry.MIC_KEYS) {
            if (event.action == KeyEvent.ACTION_DOWN && event.repeatCount == 0) AssistEntry.press(this, via = "key", showScreen = false)
            return true
        }
        return super.dispatchKeyEvent(event)
    }

    private fun onAction(action: AskAction) = when (action) {
        AskAction.ASK -> AssistEntry.press(this, via = "pill", showScreen = false)
        AskAction.TRY_AGAIN -> AskController.dispatch(AskEvent.Retry)
        AskAction.TYPE_INSTEAD -> AskController.dispatch(AskEvent.TypeInstead)
        AskAction.OK -> AskController.dispatch(AskEvent.Dismiss)
        AskAction.WATCHING, AskAction.SETUP_VIEWERS -> openViewers()
    }

    // ---- "Who's watching" ----------------------------------------------------------------------------------

    private var viewersUi by mutableStateOf<ViewersModel?>(null)

    private fun watchingNow(): List<Viewer> {
        val t = LocalTime.now()
        return Viewers.watching(System.currentTimeMillis(), t.hour * 60 + t.minute)
    }

    private fun openViewers() {
        viewersUi = ViewersModel(Viewers.people.value, watchingNow().map { it.name }.toSet())
    }

    /** Keys while "Who's watching" is open: ◀ ▶ (and ▲ ▼ on the keyboard) move, OK acts, BACK steps back. */
    private fun viewersKey(key: Key, firstPress: Boolean): Boolean {
        val m = viewersUi ?: return false
        val kb = m.typing
        val next: ViewersModel? = when {
            kb != null -> when (key) {
                Key.DirectionLeft -> m.copy(typing = kb.move(-1, 0))
                Key.DirectionRight -> m.copy(typing = kb.move(1, 0))
                Key.DirectionUp -> m.copy(typing = kb.move(0, -1))
                Key.DirectionDown -> m.copy(typing = kb.move(0, 1))
                Key.Backspace, Key.Delete -> m.copy(typing = kb.delete())
                Key.Back -> m.copy(typing = null)
                Key.DirectionCenter, Key.Enter, Key.NumPadEnter -> if (firstPress) apply(m.ok()) else m
                else -> return false
            }
            else -> when (key) {
                Key.DirectionLeft -> m.move(-1)
                Key.DirectionRight -> m.move(1)
                Key.DirectionCenter, Key.Enter, Key.NumPadEnter -> if (firstPress) apply(m.ok()) else m
                Key.Back -> { closeViewers(m); null }
                else -> return false
            }
        }
        if (viewersUi != null) viewersUi = next
        return true
    }

    /** One OK's result: save a changed household, close on Done. Returns the model to show (null: closed). */
    private fun apply(step: ViewersStep): ViewersModel? {
        step.save?.let { LinkService.saveViewers(this, it) } // shown at once; kept until the server confirms it

        if (step.close) {
            closeViewers(step.model)
            return null
        }
        return step.model
    }

    private fun closeViewers(m: ViewersModel) {
        Viewers.choose(m.watching, System.currentTimeMillis())
        viewersUi = null
    }

    /** The "More like these" tile: a typed ask about the answer on screen ([AskEvent.More]). */
    private fun more(question: String) {
        AskController.dispatch(AskEvent.More(question))
        AskController.state.value.askId?.let { sent = it to question }
    }

    private fun submit(text: String) {
        val id = AskController.state.value.askId ?: return
        sent = id to text
        AskController.dispatch(AskEvent.Submit(text))
    }

    /** Stage times of the TV's ask: the listening screen and the heard question are on screen. */
    private fun drawn(screen: Screen) {
        when (screen) {
            is Screen.Listening -> AskController.listeningDrawn()
            is Screen.Looking -> if (AskController.state.value is AskState.Looking) AskController.lookingDrawn()
            is Screen.Answer ->
                if (screen.show.answerId == MessageBus.lastShow.value?.answerId) MessageBus.markRender(RenderPhase.FRAME)
            else -> Unit
        }
    }

    override fun onStart() {
        super.onStart()
        started.incrementAndGet()
        _onScreen.value = true
        voiceEntry = AssistEntry.voiceEntryInstalled(this) // the controller may have changed the assistant meanwhile
        NoticeOverlay.dismiss() // the user is looking at the answer now
        // A dismissed notice leaves the answer waiting (opening Lumi later still shows it), but not forever.
        // Checked on every start: a backgrounded activity brought back hours later must not show the old answer.
        MessageBus.consumeIfStale()
    }

    override fun onStop() {
        // Lumi's screen went away without BACK (HOME, another app, the screensaver's DreamActivity, the screen turning
        // off): the ask ends, so a late answer can't pop up, but the video the press paused stays paused: playing it
        // behind the launcher or the screensaver was not asked for. BACK already ended the ask and resumed the video
        // (no-op here), as did opening a pick (which leaves it paused).
        val left = started.decrementAndGet() == 0 && !isChangingConfigurations
        if (started.get() == 0) _onScreen.value = false
        if (left) {
            AskController.dispatch(AskEvent.Left)
            AskController.focusedToken = null
        }
        super.onStop()
    }

    private fun open(pick: PickUi, strings: UiStrings) {
        val warning = decideLaunch(pick.launches.first(), isVpnUp(this), pick.serviceLabel, strings).vpnWarning
        val outcome = openTitle(this, pick.launches, pick.title, pick.searchHint, warning, strings)
        if (outcome.opened) {
            // Only a launch that happened counts as opened (it feeds "recently opened" and `seen`).
            LinkService.send(openedMsg(pick.token, pick.service))
            pick.launches.lastOrNull()?.let { LinkService.watchPlay(pick.token, pick.service, it.packageName) }
            // The title took over: the ask is done and the video it paused stays paused.
            AskController.dispatch(AskEvent.Opened)
            AskController.focusedToken = null
            // The pill asked to press OK in Lumi again (the app may drop the link after its profile picker): keep
            // this screen and its answer, so opening Lumi brings them back. Otherwise done: never replay it.
            if (!outcome.keepAnswer) {
                MessageBus.consume()
                finish()
            }
        } else {
            if (pick.service.isNotBlank()) LinkService.send(openFailedMsg(pick.token, pick.service))
            val label = pick.serviceLabel.ifBlank { appLabel(this, pick.launches.last().packageName) }
            MessageBus.setNotice(pillText(strings.cantOpen(label), warning))
        }
    }

    private fun reportRendered(answerId: String) {
        AskController.postersDrawn() // the ask's `ask_timing`, once, if these are its posters
        if (renderedShowId == answerId) return
        renderedShowId = answerId
        val ms = (MessageBus.nowMs() - MessageBus.showReceivedMs).coerceAtLeast(0)
        val phases = MessageBus.renderPhases()
        Log.i(TAG, renderLine(answerId, ms, phases))
        LinkService.send(renderedMsg(answerId, ms, phases))
    }

    companion object {
        /**
         * Started (onStart..onStop) instances. A count, not a flag: with two instances in different tasks the old
         * one's onStop can run after the new one's onStart.
         */
        private val started = java.util.concurrent.atomic.AtomicInteger(0)

        /** Lumi's answer screen is visible: a `show` lands on it without the overlay grant and covers no video. */
        val onTop: Boolean get() = started.get() > 0

        private val _onScreen = kotlinx.coroutines.flow.MutableStateFlow(false)

        /** [onTop] as a flow: the mic records only while this is true (see [TvVoiceCapture]). */
        val onScreen: kotlinx.coroutines.flow.StateFlow<Boolean> = _onScreen

        /** Last answer any instance handled; survives activity recreation within the process. */
        @Volatile private var handledShowId: String? = null
        @Volatile private var renderedShowId: String? = null

        /** Test hook: as if no instance had ever started. */
        fun reset() {
            started.set(0)
            _onScreen.value = false
            handledShowId = null
            renderedShowId = null
        }
    }
}
