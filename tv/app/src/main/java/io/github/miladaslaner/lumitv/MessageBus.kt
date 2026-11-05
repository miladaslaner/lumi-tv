package io.github.miladaslaner.lumitv

import java.util.concurrent.ConcurrentHashMap
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharedFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asSharedFlow
import kotlinx.coroutines.flow.asStateFlow

/**
 * Where the server link stands, for the idle screen's line and status dot. A plain [DOWN] is retried by
 * [LinkService]; [NOT_CONFIGURED] and [REJECTED] (the server refused the token) wait for a CONFIGURE broadcast.
 */
enum class LinkState {
    CONNECTED, DOWN, NOT_CONFIGURED, REJECTED;

    /** Whether the connect loop keeps trying after a close that left the link here. */
    val reconnects: Boolean get() = this == DOWN
}

/**
 * Where server messages land. Late subscribers (an activity started by a `show`) read the
 * latest `hello` / `show` from the state flows instead of missing them.
 */
object MessageBus {
    private val _messages = MutableSharedFlow<ServerMsg>(extraBufferCapacity = 16)
    private val _hello = MutableStateFlow<ServerMsg.Hello?>(null)
    private val _lastShow = MutableStateFlow<ServerMsg.Show?>(null)
    private val _looking = MutableStateFlow<ServerMsg.Looking?>(null)
    private val _notice = MutableStateFlow<String?>(null)
    private val _linkState = MutableStateFlow(LinkState.DOWN)
    private val _preview = MutableStateFlow<ServerMsg.Preview?>(null)

    /** The latest ask's likely posters (until its answer, or the next ask, replaces them). */
    val preview: StateFlow<ServerMsg.Preview?> = _preview.asStateFlow()

    /** The server link right now (set by [LinkService]); [LinkState.DOWN] until it first connects. */
    val linkState: StateFlow<LinkState> = _linkState.asStateFlow()

    fun setLinkState(state: LinkState) {
        _linkState.value = state
    }

    /** Every parsed server message, in arrival order (no replay). */
    val messages: SharedFlow<ServerMsg> = _messages.asSharedFlow()

    /** Latest `hello` (theme and strings). */
    val hello: StateFlow<ServerMsg.Hello?> = _hello.asStateFlow()

    /** The connected server's ask budgets; the defaults until a `hello` says otherwise. */
    val limits: AskLimits get() = _hello.value?.limits ?: AskLimits.DEFAULT

    /** Latest `show`; set before the activity is started so the first answer is never lost. */
    val lastShow: StateFlow<ServerMsg.Show?> = _lastShow.asStateFlow()

    /** The in-flight `looking` (cleared by the `show` that answers it). */
    val looking: StateFlow<ServerMsg.Looking?> = _looking.asStateFlow()

    /** The TV's own ask (mic or typing), for the screen; see [AskController]. */
    val ask: StateFlow<AskState> get() = AskController.state

    /** A message for the screen (VPN needed), or null. */
    val notice: StateFlow<String?> = _notice.asStateFlow()

    /** Monotonic ms at which [lastShow] was received; the start of the `rendered` timing. */
    @Volatile var showReceivedMs: Long = 0L
        private set

    /** Ms after [showReceivedMs] at which each [RenderPhase] of the current answer happened (first time only). */
    private val phases = ConcurrentHashMap<String, Long>()

    fun markRender(phase: String) {
        phases.putIfAbsent(phase, (nowMs() - showReceivedMs).coerceAtLeast(0))
    }

    fun renderPhases(): Map<String, Long> = phases.toMap()

    /** An answer nobody opened for this long is forgotten rather than replayed from the launcher. */
    const val ANSWER_MAX_AGE_MS = 10 * 60 * 1000L

    /**
     * Restarts the `rendered` clock. Used when the user opens a waiting answer from the notice: the
     * time spent waiting for OK is not render time.
     */
    fun restartRenderClock() {
        showReceivedMs = nowMs()
        phases.clear()
    }

    fun nowMs(): Long = System.nanoTime() / 1_000_000

    fun publish(msg: ServerMsg) {
        when (msg) {
            is ServerMsg.Hello -> _hello.value = msg
            is ServerMsg.Show -> {
                showReceivedMs = nowMs()
                phases.clear()
                _preview.value = null
                _looking.value = null
                _notice.value = null
                _lastShow.value = msg
            }
            // A new turn supersedes whatever answer was on screen.
            is ServerMsg.Preview -> _preview.value = msg
            is ServerMsg.Looking -> {
                _lastShow.value = null
                _notice.value = null
                _looking.value = msg
            }
            else -> Unit
        }
        _messages.tryEmit(msg)
    }

    fun setNotice(text: String?) {
        _notice.value = text
    }

    /** Drops a `looking` that no `show` ever answered. */
    fun expireLooking(turnId: String) {
        if (_looking.value?.turnId == turnId) _looking.value = null
    }

    /** Drops the current answer if it is older than [maxAgeMs]; a fresh one is untouched. */
    fun consumeIfStale(now: Long = nowMs(), maxAgeMs: Long = ANSWER_MAX_AGE_MS) {
        if (_lastShow.value != null && now - showReceivedMs > maxAgeMs) consume()
    }

    /** The user dismissed the screen: forget the answer so the launcher does not replay it. */
    fun consume() {
        _preview.value = null
        _lastShow.value = null
        _looking.value = null
        _notice.value = null
    }

    /** Test hook. */
    fun reset() {
        _hello.value = null
        _linkState.value = LinkState.DOWN
        consume()
    }
}

/**
 * The steps between receiving a `show` and its posters being drawn, sent in `rendered` to find where a slow first
 * answer spends its time: the answer screen was asked to start, it was created, its first frame was drawn.
 */
object RenderPhase {
    const val START = "start"
    const val CREATED = "created"
    const val FRAME = "frame"
    val ALL = listOf(START, CREATED, FRAME)
}
