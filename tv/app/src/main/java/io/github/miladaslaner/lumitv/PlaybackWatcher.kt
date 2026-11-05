package io.github.miladaslaner.lumitv

import android.content.ComponentName
import android.content.Context
import android.media.session.MediaController
import android.media.session.MediaSession
import android.media.session.MediaSessionManager
import android.media.session.PlaybackState
import android.os.Handler
import android.os.Looper
import android.os.SystemClock
import android.service.notification.NotificationListenerService
import android.util.Log
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow

/** How an answer reaches the user: the full screen, or a small notice over what is playing ([AnswerAction] acks it). */
enum class Mode { SCREEN, NOTICE }

/**
 * [playing] (busy, see [isBusy]) or unknown ([known] false: the notification listener is not bound, so a video
 * may be playing) sends the answer to the notice. Exception: Lumi's own answer screen is on top
 * ([answerOnTop]), so no video is visible and the answer simply replaces what Lumi shows.
 */
fun route(playing: Boolean, known: Boolean, answerOnTop: Boolean = false): Mode =
    if (!answerOnTop && (playing || !known)) Mode.NOTICE else Mode.SCREEN

/**
 * How long a paused session keeps the TV "busy" (someone paused to grab snacks). Android cannot tell us which app
 * is in the foreground without another permission (usage access), so recency stands in for it: a session that has
 * been paused this long is most likely a backgrounded app's leftover and no longer blocks the full-screen answer.
 */
const val PAUSED_BUSY_MS = 20 * 60 * 1000L

/** A media session's playback state and, while paused, since when (elapsedRealtime ms). */
data class SessionSnapshot(val state: Int?, val pausedSinceMs: Long? = null, val packageName: String? = null)

private fun SessionSnapshot.pausedRecently(nowMs: Long, windowMs: Long) =
    state == PlaybackState.STATE_PAUSED && pausedSinceMs != null && nowMs - pausedSinceMs < windowMs

/**
 * Busy = someone is watching: a session is playing, buffering (a stall mid-video must not let an answer cover
 * it), or paused less than [windowMs] ago. null = no playback state.
 */
fun isBusy(sessions: List<SessionSnapshot>, nowMs: Long, windowMs: Long = PAUSED_BUSY_MS): Boolean = sessions.any {
    it.state == PlaybackState.STATE_PLAYING || it.state == PlaybackState.STATE_BUFFERING || it.pausedRecently(nowMs, windowMs)
}

/** Something is playing (or buffering) right now; a recent pause doesn't count. */
fun isPlayingNow(sessions: List<SessionSnapshot>): Boolean =
    sessions.any { it.state == PlaybackState.STATE_PLAYING || it.state == PlaybackState.STATE_BUFFERING }

private fun isPlayingState(state: Int?) = state == PlaybackState.STATE_PLAYING || state == PlaybackState.STATE_BUFFERING

/** The sessions an ask pauses: of (session, playback state) pairs, those playing or buffering, in order. */
fun <T> pauseTargets(sessions: List<Pair<T, Int?>>): List<T> =
    sessions.filter { isPlayingState(it.second) }.map { it.first }

/**
 * The sessions to play again: of the current (session, playback state) pairs, those in [paused] (what the press
 * paused) that are still paused. A session that was paused before the press, or that the user since resumed,
 * stopped or closed, is left alone.
 */
fun <T> resumeTargets(sessions: List<Pair<T, Int?>>, paused: Collection<T>): List<T> =
    sessions.filter { it.first in paused && it.second == PlaybackState.STATE_PAUSED }.map { it.first }

/** When [isBusy] changes by itself (the newest-expiring recent pause ages out), or null if it can't. */
fun pausedExpiry(sessions: List<SessionSnapshot>, nowMs: Long, windowMs: Long = PAUSED_BUSY_MS): Long? =
    sessions.filter { it.pausedRecently(nowMs, windowMs) }.mapNotNull { it.pausedSinceMs?.plus(windowMs) }.maxOrNull()

/** Sends [state] unless the server already has it. Pure so the no-flood rules are testable. */
class StateDedupe {
    private var last: String? = null

    /** [force] (a new connection) always sends; a failed send is not remembered, so it is retried. */
    @Synchronized
    fun offer(state: String, force: Boolean, send: (String) -> Boolean) {
        if (force) last = null
        if (state != last && send(state)) last = state
    }
}

/**
 * A [NotificationListenerService] used only because Android requires one to read other apps' media
 * sessions. It overrides no notification callback: notification content is never read or logged.
 * Needs a one-time grant: `adb shell cmd notification allow_listener <component>` (see README).
 */
class PlaybackWatcher : NotificationListenerService() {
    private val callbacks = mutableMapOf<MediaController, MediaController.Callback>()
    private val pausedSince = mutableMapOf<MediaController, Long>()
    private val handler = Handler(Looper.getMainLooper())
    private val recheck = Runnable { publish() }
    private val sessionsListener = MediaSessionManager.OnActiveSessionsChangedListener { track(it.orEmpty()) }

    private val component get() = ComponentName(this, PlaybackWatcher::class.java)

    override fun onListenerConnected() {
        val msm = getSystemService(MediaSessionManager::class.java)
        try {
            msm.addOnActiveSessionsChangedListener(sessionsListener, component)
            track(msm.getActiveSessions(component))
            _connected.value = true // only now is `playing` real
        } catch (e: SecurityException) {
            Log.w(TAG, "no media-session access: grant the notification listener (see README)")
            requestRebind(this)
        }
    }

    override fun onListenerDisconnected() {
        _connected.value = false
        runCatching { getSystemService(MediaSessionManager::class.java).removeOnActiveSessionsChangedListener(sessionsListener) }
        track(emptyList())
        requestRebind(this) // no-op when the user revoked the grant
    }

    override fun onDestroy() {
        _connected.value = false
        track(emptyList())
        handler.removeCallbacks(recheck)
        super.onDestroy()
    }

    /** Follows exactly [controllers]: new ones get a callback, vanished ones lose it. */
    private fun track(controllers: List<MediaController>) {
        callbacks.keys.filter { it !in controllers }.forEach { callbacks.remove(it)?.let(it::unregisterCallback) }
        controllers.filter { it !in callbacks }.forEach { c ->
            val cb = object : MediaController.Callback() {
                override fun onPlaybackStateChanged(state: PlaybackState?) = publish()
            }
            callbacks[c] = cb
            c.registerCallback(cb)
        }
        tracked = callbacks.keys.toList()
        publish()
    }

    /** A session already paused when first seen counts from then: busy for a while, the safe side. */
    private fun publish() {
        val now = SystemClock.elapsedRealtime()
        pausedSince.keys.retainAll(callbacks.keys)
        val sessions = callbacks.keys.map { c ->
            val state = c.playbackState?.state
            if (state == PlaybackState.STATE_PAUSED) pausedSince.getOrPut(c) { now } else pausedSince.remove(c)
            SessionSnapshot(state, pausedSince[c], c.packageName)
        }
        _playing.value = isBusy(sessions, now)
        _playingNow.value = isPlayingNow(sessions)
        handler.removeCallbacks(recheck)
        pausedExpiry(sessions, now)?.let { handler.postDelayed(recheck, it - now) }
    }

    companion object {
        /** The sessions followed right now; a snapshot, so [pausePlaying] may run off the main thread. */
        @Volatile private var tracked: List<MediaController> = emptyList()

        /**
         * Pauses every playing or buffering session ([pauseTargets]) and returns their tokens, for [resume]. Empty
         * while the listener is not connected.
         */
        fun pausePlaying(): List<MediaSession.Token> {
            val targets = pauseTargets(tracked.map { it to it.playbackState?.state })
            targets.forEach { c ->
                runCatching { c.transportControls.pause() }.onFailure { Log.w(TAG, "pause failed: ${it.javaClass.simpleName}") }
            }
            return targets.map { it.sessionToken }
        }

        /** Plays again exactly the sessions [pausePlaying] paused, if still active and still paused ([resumeTargets]). */
        fun resume(sessions: Collection<MediaSession.Token>) {
            if (sessions.isEmpty()) return
            val byToken = tracked.associateBy { it.sessionToken }
            resumeTargets(byToken.map { (token, c) -> token to c.playbackState?.state }, sessions).forEach { token ->
                runCatching { byToken.getValue(token).transportControls.play() }
                    .onFailure { Log.w(TAG, "resume failed: ${it.javaClass.simpleName}") }
            }
        }

        /** Apps with a session playing (or buffering) right now; empty while the listener is not connected. */
        fun playingPackages(): Set<String> =
            tracked.filter { isPlayingState(it.playbackState?.state) }.map { it.packageName }.toSet()

        private val _playing = MutableStateFlow(false)

        /** Whether someone is watching ([isBusy]); false while the listener is not granted or connected. */
        val playing: StateFlow<Boolean> = _playing.asStateFlow()

        fun isPlaying(): Boolean = _playing.value

        private val _playingNow = MutableStateFlow(false)

        /** [isPlayingNow] of the current sessions; false while the listener is not connected (check [connected]). */
        fun isPlayingNow(): Boolean = _playingNow.value

        private val _connected = MutableStateFlow(false)

        /** Whether the listener is bound with media-session access; while false, [playing] means nothing. */
        val listenerConnected: StateFlow<Boolean> = _connected.asStateFlow()

        val connected: Boolean get() = _connected.value

        /**
         * After `adb install -r` an allowed listener can stay unbound until toggled; ask the system to
         * bind it. Harmless if the grant is missing.
         */
        fun requestRebind(context: Context) {
            try {
                requestRebind(ComponentName(context, PlaybackWatcher::class.java))
            } catch (e: RuntimeException) {
                Log.w(TAG, "listener rebind request failed: ${e.javaClass.simpleName}")
            }
        }
    }
}
