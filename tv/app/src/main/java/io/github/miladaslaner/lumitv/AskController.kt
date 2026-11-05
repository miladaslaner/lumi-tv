package io.github.miladaslaner.lumitv

import android.content.Context
import android.media.session.MediaSession
import android.util.Log
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import java.security.SecureRandom
import java.time.LocalTime

private const val ID_CHARS = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
private val random = SecureRandom()

/** A fresh ask id: 12 URL-safe characters (the server accepts `^[A-Za-z0-9_-]{8,40}$`). */
fun newAskId(): String = String(CharArray(12) { ID_CHARS[random.nextInt(ID_CHARS.length)] })

/** Where the current ask is. Every non-idle state names its ask; messages for any other ask are ignored. */
sealed interface AskState {
    data object Idle : AskState
    data class Listening(val askId: String, val partial: String?) : AskState
    /** The question is on its way to the server (POST in flight, or audio being transcribed). */
    data class Sending(val askId: String) : AskState
    data class Looking(val askId: String, val question: String) : AskState
    data class Typing(val askId: String, val text: String) : AskState
    /** [code]: a server `ask_error` code, or `offline` when the POST could not reach the server. */
    data class Failed(val askId: String, val code: String) : AskState
    data class Reply(val askId: String, val text: String) : AskState
    /** The posters for this ask are on screen. */
    data class Answered(val askId: String) : AskState
}

/** A fixed name for logs: release builds shorten class names, and the TV check script reads these. */
val AskState.logName: String
    get() = when (this) {
        AskState.Idle -> "Idle"
        is AskState.Listening -> "Listening"
        is AskState.Sending -> "Sending"
        is AskState.Looking -> "Looking"
        is AskState.Typing -> "Typing"
        is AskState.Failed -> "Failed($code)"
        is AskState.Reply -> "Reply"
        is AskState.Answered -> "Answered"
    }

val AskState.askId: String?
    get() = when (this) {
        AskState.Idle -> null
        is AskState.Listening -> askId
        is AskState.Sending -> askId
        is AskState.Looking -> askId
        is AskState.Typing -> askId
        is AskState.Failed -> askId
        is AskState.Reply -> askId
        is AskState.Answered -> askId
    }

sealed interface AskEvent {
    /** The mic was pressed. [voice]: capture can run (else typing); [playing]: a video plays right now. */
    data class Press(val voice: Boolean, val playing: Boolean) : AskEvent
    data object CaptureStarted : AskEvent
    data class Partial(val text: String) : AskEvent
    /** The capture ended with speech; the runtime sends it. */
    data object Captured : AskEvent
    data object NothingHeard : AskEvent
    /** The capture could not run or broke off; [reason] as the captures name it (see [captureFailureCode]). */
    data class CaptureFailed(val reason: String) : AskEvent
    data object Posted : AskEvent
    /** The POST of the current ask could not reach the server. */
    data object PostFailed : AskEvent
    data class Heard(val askId: String, val text: String) : AskEvent
    /** A `show` arrived; [askId] null means it came from Claude Desktop. */
    data class ShowArrived(val askId: String?) : AskEvent
    data class Error(val askId: String, val code: String) : AskEvent
    data class Reply(val askId: String, val text: String) : AskEvent
    data object TypeInstead : AskEvent
    data class Submit(val text: String) : AskEvent
    /** OK on the answer's "More like these" tile: ask [text] about what is on screen, without typing it. */
    data class More(val text: String) : AskEvent
    data object Retry : AskEvent
    /**
     * OK on an error nothing on the TV can fix, or BACK on the question keyboard: the ask is over and the screen goes
     * back to what was there before (idle, or the answer). Lumi stays in front, so a video the press paused stays
     * paused until [Back].
     */
    data object Dismiss : AskEvent
    /** BACK on Lumi's screen: the user returns to what the press paused, which plays again. */
    data object Back : AskEvent

    /**
     * Lumi's screen went away some other way (HOME, another app, the screensaver, the screen turning off): the ask
     * ends, but a video the press paused stays paused. Resuming it would play sound behind the launcher or the
     * screensaver, which the user did not ask for.
     */
    data object Left : AskEvent
    /** A pick (or a server `open`) launched a title. */
    data object Opened : AskEvent
    data class Timeout(val askId: String) : AskEvent
}

sealed interface AskEffect {
    data class StartCapture(val askId: String) : AskEffect
    /** Stop recording and drop what was captured (the ask was left or replaced). */
    data object StopCapture : AskEffect
    data class PostTyped(val askId: String, val text: String) : AskEffect
    data class SendCancel(val askId: String) : AskEffect
    data object PausePlayback : AskEffect
    data object ResumePlayback : AskEffect
    data class ArmTimeout(val askId: String, val ms: Long) : AskEffect
}

/**
 * The ask error code for a capture that failed for [reason]: the mic's permission and a recognizer that is missing,
 * refused or offline get their own line (typing is the way out); silence and a mic that stopped delivering read as
 * "didn't catch that" (worth a retry).
 */
fun captureFailureCode(reason: String): String = when (reason) {
    "permission" -> "mic_permission"
    "recognizer", "unavailable", "network" -> "no_recognizer"
    else -> "not_heard"
}

/** The server may still be working on this ask: leaving it sends `ask_cancel`. */
private fun AskState.serverWorking() = this is AskState.Sending || this is AskState.Looking

/** What leaving [state] for another ask (or none) must undo. */
private fun leaving(state: AskState): List<AskEffect> = when {
    state is AskState.Listening -> listOf(AskEffect.StopCapture)
    state.serverWorking() -> listOf(AskEffect.SendCancel(state.askId!!))
    else -> emptyList()
}

/**
 * The ask state machine. Pure: returns the next state, whether a video is paused by this ask ([pausedByUs]), and the
 * effects to run. [newId] makes ask ids (tests pass a predictable one); [limits] are the server's budgets, which set
 * how long the TV waits for an answer ([AskEffect.ArmTimeout]).
 *
 * Playback: a press while a video plays pauses it; it stays paused while Lumi's screen covers it and is resumed
 * only when the user goes back to it with BACK ([AskEvent.Back] from any ask state). Leaving any other way
 * ([AskEvent.Left]: HOME, another app, screensaver, screen off) or opening a pick ([AskEvent.Opened]) ends the ask and
 * leaves the video paused. To resume as soon as the answer appears instead, emit [AskEffect.ResumePlayback] on
 * [AskEvent.ShowArrived] / [AskEvent.Reply] and clear the flag there.
 */
fun reduce(
    state: AskState,
    pausedByUs: Boolean,
    event: AskEvent,
    newId: () -> String = ::newAskId,
    limits: AskLimits = AskLimits.DEFAULT,
): Triple<AskState, Boolean, List<AskEffect>> {
    val unchanged = Triple(state, pausedByUs, emptyList<AskEffect>())
    fun go(next: AskState, vararg effects: AskEffect) = Triple(next, pausedByUs, effects.toList())
    fun current(askId: String) = askId == state.askId
    val waiting = state.serverWorking()

    return when (event) {
        is AskEvent.Press -> {
            val pause = event.playing && !pausedByUs
            val id = newId()
            val effects = leaving(state) + listOfNotNull(AskEffect.PausePlayback.takeIf { pause }) +
                listOfNotNull(AskEffect.StartCapture(id).takeIf { event.voice })
            val next = if (event.voice) AskState.Listening(id, null) else AskState.Typing(id, "")
            Triple(next, pausedByUs || pause, effects)
        }
        AskEvent.CaptureStarted, AskEvent.Posted -> unchanged
        is AskEvent.Partial -> if (state is AskState.Listening) go(state.copy(partial = event.text)) else unchanged
        AskEvent.Captured ->
            if (state is AskState.Listening) {
                go(AskState.Sending(state.askId), AskEffect.ArmTimeout(state.askId, limits.voiceTimeoutMs))
            } else unchanged
        AskEvent.NothingHeard -> if (state is AskState.Listening) go(AskState.Failed(state.askId, "not_heard")) else unchanged
        is AskEvent.CaptureFailed ->
            if (state is AskState.Listening) go(AskState.Failed(state.askId, captureFailureCode(event.reason))) else unchanged
        AskEvent.PostFailed -> if (state is AskState.Sending) go(AskState.Failed(state.askId, "offline")) else unchanged
        is AskEvent.Heard ->
            if (current(event.askId) && (waiting || state is AskState.Listening)) go(AskState.Looking(event.askId, event.text))
            else unchanged
        is AskEvent.ShowArrived ->
            if (event.askId != null && current(event.askId) && waiting) go(AskState.Answered(event.askId)) else unchanged
        is AskEvent.Error ->
            if (current(event.askId) && (waiting || state is AskState.Listening)) go(AskState.Failed(event.askId, event.code))
            else unchanged
        is AskEvent.Reply -> if (current(event.askId) && waiting) go(AskState.Reply(event.askId, event.text)) else unchanged
        AskEvent.TypeInstead ->
            if (state is AskState.Failed || state is AskState.Listening || state is AskState.Reply) {
                Triple(AskState.Typing(newId(), ""), pausedByUs, leaving(state))
            } else unchanged
        is AskEvent.Submit ->
            if (state is AskState.Typing && event.text.isNotBlank()) {
                go(
                    AskState.Sending(state.askId),
                    AskEffect.PostTyped(state.askId, event.text.trim()),
                    AskEffect.ArmTimeout(state.askId, limits.typedTimeoutMs),
                )
            } else unchanged
        is AskEvent.More ->
            // Only from a finished answer (posters on screen, from this TV or from Claude Desktop), never mid-ask.
            if ((state == AskState.Idle || state is AskState.Answered) && event.text.isNotBlank()) {
                val id = newId()
                go(AskState.Sending(id), AskEffect.PostTyped(id, event.text.trim()), AskEffect.ArmTimeout(id, limits.typedTimeoutMs))
            } else unchanged
        AskEvent.Retry ->
            if (state is AskState.Failed || state is AskState.Listening) {
                val id = newId()
                Triple(AskState.Listening(id, null), pausedByUs, leaving(state) + AskEffect.StartCapture(id))
            } else unchanged
        AskEvent.Dismiss -> if (state is AskState.Failed || state is AskState.Typing) go(AskState.Idle) else unchanged
        AskEvent.Back ->
            if (state == AskState.Idle && !pausedByUs) unchanged
            else Triple(AskState.Idle, false, leaving(state) + listOfNotNull(AskEffect.ResumePlayback.takeIf { pausedByUs }))
        // The opened title takes over the screen, or Lumi was left for something else: the paused video stays paused.
        AskEvent.Opened, AskEvent.Left ->
            if (state == AskState.Idle && !pausedByUs) unchanged else Triple(AskState.Idle, false, leaving(state))
        is AskEvent.Timeout ->
            if (current(event.askId) && waiting) go(AskState.Failed(event.askId, "timeout"), AskEffect.SendCancel(event.askId))
            else unchanged
    }
}

/**
 * The ask event for how the server took ask [askId]'s POST. A 503 to spoken audio means the server has no
 * speech-to-text (`no_speech`: type instead), not that Lumi couldn't answer; a 429 means the ask cap (`limit`).
 */
fun postOutcome(askId: String, result: PostResult, audio: Boolean): AskEvent = when (result) {
    PostResult.Accepted -> AskEvent.Posted
    is PostResult.Rejected -> AskEvent.Error(
        askId,
        when {
            result.code == 429 -> "limit" // the server's per-minute or per-day ask cap
            audio && result.code == 503 -> "no_speech"
            else -> "failed"
        },
    )
    PostResult.Unreachable -> AskEvent.PostFailed
}

/**
 * Whether a server message reaches the screen. Claude Desktop's messages (no `ask_id`) always do. An ask's message counts only for
 * the newest ask, and a `show` / `looking` / `ask_reply` only while that ask still waits for the server: a late answer
 * to an ask that already timed out, failed or was left must not open the posters (or wipe the answer on screen).
 */
fun wantsAskMessage(state: AskState, msg: ServerMsg): Boolean {
    val id = askIdOf(msg) ?: return true
    if (id != state.askId) return false
    val waiting = state.serverWorking()
    return when (msg) {
        is ServerMsg.Heard, is ServerMsg.AskError -> waiting || state is AskState.Listening
        else -> waiting
    }
}

/** Monotonic stage times of one ask ([MessageBus.nowMs] clock), turned into `ask_timing` when its posters are drawn. */
class AskTimings(val askId: String) {
    var pressAt: Long? = null
    var listeningDrawnAt: Long? = null
    var speechEndAt: Long? = null
    var lookingDrawnAt: Long? = null
    var submitAt: Long? = null

    fun message(postersDrawnAt: Long): String = askTimingMsg(
        askId,
        listenMs = span(pressAt, listeningDrawnAt),
        heardMs = span(speechEndAt, lookingDrawnAt),
        postersMs = span(speechEndAt, postersDrawnAt),
        typedPostersMs = span(submitAt, postersDrawnAt),
    )

    private fun span(from: Long?, to: Long?): Long? = if (from == null || to == null) null else (to - from).coerceAtLeast(0)
}

/** What the controller's effects act on; [LiveAskRuntime] on the TV. */
interface AskRuntime {
    /** Voice capture can run; without it a "Try again" goes to typing. */
    val voiceAvailable: Boolean
    fun startCapture(askId: String)
    fun stopCapture()
    suspend fun post(askId: String, text: String, source: String, answerId: String?, focusedToken: String?): PostResult
    suspend fun postAudio(askId: String, wav: ByteArray, answerId: String?, focusedToken: String?): PostResult
    /** A TV->server message over the link; false when not connected. */
    fun send(json: String): Boolean
    /** Pauses what plays; returns the sessions it paused, for [resumePlayback] later. */
    fun pausePlayback(): List<MediaSession.Token>
    fun resumePlayback(sessions: List<MediaSession.Token>)
}

/** The TV's runtime: [AskClient] with the link's url/token, [capture], [PlaybackWatcher], [LinkService.send]. */
class LiveAskRuntime(context: Context, private val capture: VoiceCapture = NoCapture) : AskRuntime {
    private val app = context.applicationContext
    private val client = AskClient(httpClient) {
        val prefs = app.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
        val url = prefs.getString(PREF_URL, null)
        val token = prefs.getString(PREF_TOKEN, null)
        if (url.isNullOrBlank() || token.isNullOrBlank()) null else url to token
    }

    override val voiceAvailable get() = capture.available

    override fun startCapture(askId: String) {
        // Without a capture, say so at once instead of listening forever.
        if (!capture.available) return AskController.dispatch(AskEvent.CaptureFailed("unavailable"))
        capture.start(askId) { AskController.onCapture(askId, it) }
    }

    override fun stopCapture() = capture.cancel()

    override suspend fun post(askId: String, text: String, source: String, answerId: String?, focusedToken: String?) =
        client.postText(askId, text, source, answerId, focusedToken, watchingNow())

    override suspend fun postAudio(askId: String, wav: ByteArray, answerId: String?, focusedToken: String?) =
        client.postAudio(askId, wav, answerId, focusedToken, watchingNow())

    /** Who is watching right now (names), for the ask: the server's kids limit and taste follow it. */
    private fun watchingNow(): List<String> {
        val now = LocalTime.now()
        return Viewers.watching(System.currentTimeMillis(), now.hour * 60 + now.minute).map { it.name }
    }

    override fun send(json: String) = LinkService.send(json)

    override fun pausePlayback() = PlaybackWatcher.pausePlaying()

    override fun resumePlayback(sessions: List<MediaSession.Token>) = PlaybackWatcher.resume(sessions)
}

/**
 * Runs the ask state machine ([reduce]) on the TV: holds [state], runs effects through the installed [AskRuntime]
 * and reports the ask's stage times (`ask_timing`) once its posters are drawn. Logs ask ids and states, never text.
 * Main thread only (activities, [LinkService] and its own scope all run there); [onCapture] hops to it.
 */
object AskController {
    private val _state = MutableStateFlow<AskState>(AskState.Idle)
    val state: StateFlow<AskState> = _state.asStateFlow()

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main.immediate)
    @Volatile private var runtime: AskRuntime? = null
    private var pausedByUs = false
    /** The sessions the press paused (exactly those, never one that was already paused), for the resume. */
    private var pausedSessions: List<MediaSession.Token> = emptyList()
    private var timeoutJob: Job? = null
    private var timings: AskTimings? = null
    private var timingSent = false

    /** The focused poster's token, sent as `focused_token` with the next ask (set by the answer screen). */
    @Volatile var focusedToken: String? = null

    fun install(runtime: AskRuntime) {
        this.runtime = runtime
    }

    /** Test hook: as in a fresh process (no runtime, no ask, nothing paused). */
    fun reset() {
        synchronized(this) {
            timeoutJob?.cancel()
            timeoutJob = null
            runtime = null
            pausedByUs = false
            pausedSessions = emptyList()
            timings = null
            timingSent = false
            focusedToken = null
            _state.value = AskState.Idle
        }
    }

    /** A press can listen (else it types). */
    val voiceAvailable: Boolean get() = runtime?.voiceAvailable == true

    /** [askId] is the newest ask: only its server messages count. */
    fun isCurrent(askId: String): Boolean = askId == _state.value.askId

    fun dispatch(event: AskEvent) {
        val rt = runtime ?: return Unit.also { Log.w(TAG, "ask event before the runtime is installed") }
        var toResume: List<MediaSession.Token> = emptyList()
        val effects = synchronized(this) {
            // "Try again" without a mic is typing.
            val e = if (event == AskEvent.Retry && !rt.voiceAvailable) AskEvent.TypeInstead else event
            val before = _state.value
            val (next, paused, effects) = reduce(before, pausedByUs, e, limits = MessageBus.limits)
            pausedByUs = paused
            // Once nothing is paused by us (resumed, or a pick opened) the saved sessions are done with.
            if (!paused) toResume = pausedSessions.also { pausedSessions = emptyList() }
            _state.value = next
            if (next != before) Log.i(TAG, "ask ${next.askId ?: before.askId}: ${next.logName}")
            mark(before, next, e)
            effects
        }
        effects.forEach { run(rt, it, toResume) }
    }

    /** A [VoiceCapture] event for ask [askId], from any thread. Events of an ask that is no longer current are dropped. */
    fun onCapture(askId: String, event: CaptureEvent) {
        scope.launch {
            if (!isCurrent(askId)) {
                if (event is CaptureEvent.Audio) event.wav.fill(0)
                return@launch
            }
            when (event) {
                is CaptureEvent.Text -> {
                    captured(event.endAtMs)
                    postText(askId, event.text.take(MessageBus.limits.questionChars), "voice")
                }
                is CaptureEvent.Audio -> {
                    captured(event.endAtMs)
                    try {
                        postAudio(askId, event.wav)
                    } finally {
                        event.wav.fill(0) // the only copy of the speech: gone once it was sent
                    }
                }
                is CaptureEvent.Failed -> {
                    Log.w(TAG, "ask $askId capture failed: ${event.reason}")
                    dispatch(AskEvent.CaptureFailed(event.reason))
                }
                else -> askEventFor(event)?.let(::dispatch)
            }
        }
    }

    /** The capture ended at [endAtMs] ([MessageBus.nowMs] clock) with speech; more exact than the dispatch time. */
    fun captured(endAtMs: Long) {
        synchronized(this) { timings?.let { if (it.askId == _state.value.askId) it.speechEndAt = endAtMs } }
        dispatch(AskEvent.Captured)
    }

    /** The listening screen is drawn. */
    fun listeningDrawn() = synchronized(this) { currentTimings()?.let { it.listeningDrawnAt = it.listeningDrawnAt ?: MessageBus.nowMs() } }

    /** "Looking for …" is drawn. */
    fun lookingDrawn() = synchronized(this) { currentTimings()?.let { it.lookingDrawnAt = it.lookingDrawnAt ?: MessageBus.nowMs() } }

    /** The answer's posters are drawn: sends this ask's `ask_timing` (once). */
    fun postersDrawn() {
        val msg = synchronized(this) {
            val t = currentTimings()
            if (t == null || timingSent || _state.value !is AskState.Answered) return
            timingSent = true
            t.message(MessageBus.nowMs())
        }
        runtime?.send(msg)
    }

    /** Sends [text] for ask [askId] with what is on screen, then feeds the outcome back (if still the newest ask). */
    suspend fun postText(askId: String, text: String, source: String) =
        send(askId, audio = false) { rt, answerId, focused -> rt.post(askId, text, source, answerId, focused) }

    /** Sends the spoken question [wav] for the server's speech-to-text (Path A), like [postText]. */
    suspend fun postAudio(askId: String, wav: ByteArray) =
        send(askId, audio = true) { rt, answerId, focused -> rt.postAudio(askId, wav, answerId, focused) }

    private suspend fun send(askId: String, audio: Boolean, post: suspend (AskRuntime, String?, String?) -> PostResult) {
        val rt = runtime ?: return
        val result = post(rt, MessageBus.lastShow.value?.answerId, focusedToken)
        Log.i(TAG, "ask $askId post: ${result.logName}")
        if (!isCurrent(askId)) return
        dispatch(postOutcome(askId, result, audio))
    }

    private fun currentTimings(): AskTimings? = timings?.takeIf { it.askId == _state.value.askId }

    private fun mark(before: AskState, next: AskState, event: AskEvent) {
        val id = next.askId ?: return
        val now = MessageBus.nowMs()
        if (id != before.askId) {
            timings = AskTimings(id).also { if (event is AskEvent.Press) it.pressAt = now }
            timingSent = false
        }
        val t = timings ?: return
        when (event) {
            AskEvent.Captured -> if (t.speechEndAt == null) t.speechEndAt = now
            is AskEvent.Submit, is AskEvent.More -> if (next is AskState.Sending) t.submitAt = now
            else -> Unit
        }
    }

    private fun run(rt: AskRuntime, effect: AskEffect, toResume: List<MediaSession.Token>) {
        when (effect) {
            is AskEffect.StartCapture -> {
                rt.send(askWarmMsg(effect.askId)) // first: the warm-up overlaps the user's speech
                rt.startCapture(effect.askId)
            }
            AskEffect.StopCapture -> rt.stopCapture()
            is AskEffect.PostTyped -> scope.launch { postText(effect.askId, effect.text, "typed") }
            is AskEffect.SendCancel -> rt.send(askCancelMsg(effect.askId))
            AskEffect.PausePlayback -> pausedSessions = rt.pausePlayback()
            AskEffect.ResumePlayback -> rt.resumePlayback(toResume)
            is AskEffect.ArmTimeout -> {
                timeoutJob?.cancel()
                timeoutJob = scope.launch {
                    delay(effect.ms)
                    dispatch(AskEvent.Timeout(effect.askId))
                }
            }
        }
    }
}
