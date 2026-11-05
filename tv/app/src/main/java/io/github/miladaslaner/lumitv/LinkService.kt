package io.github.miladaslaner.lumitv

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.BroadcastReceiver
import android.content.Context
import android.content.IntentFilter
import android.content.Intent
import android.content.pm.ServiceInfo
import android.net.ConnectivityManager
import android.net.Network
import android.net.NetworkCapabilities
import android.os.Build
import android.os.IBinder
import android.os.PowerManager
import android.provider.Settings
import android.util.Log
import io.github.miladaslaner.lumitv.ui.UiStrings
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.FlowPreview
import kotlinx.coroutines.flow.combine
import kotlinx.coroutines.flow.debounce
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.delay
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.launch
import kotlinx.coroutines.withTimeoutOrNull
import okhttp3.HttpUrl.Companion.toHttpUrl
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener

const val TAG = "LumiTV"
const val PREFS = "lumi"
const val PREF_URL = "url"
/** How long after opening a pick the TV watches for playback, and how often it looks. */
const val PLAY_CHECK_MS = 90_000L
const val PLAY_CHECK_STEP_MS = 10_000L
const val PREF_TOKEN = "token"

/**
 * Foreground service that keeps the WebSocket to the Lumi TV server up (so the process stays
 * warm) and publishes server messages on [messages].
 */
@OptIn(FlowPreview::class)
class LinkService : Service() {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main.immediate)
    @Volatile private var socket: WebSocket? = null
    private var connectJob: Job? = null
    private val wake = Channel<Unit>(Channel.CONFLATED)
    private val stateDedupe = StateDedupe()

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        instance = this
        enterForeground()
        registerWakeTriggers()
        if (!PlaybackWatcher.connected) PlaybackWatcher.requestRebind(this)
        // Build the shared image loader (OkHttp client, caches) now, off the main thread, not on the first answer.
        scope.launch(Dispatchers.IO) { coil.Coil.imageLoader(applicationContext) }
        // `playing` flaps around seeks and ads; report only once it has settled. The listener binding
        // (playback state known or not) is part of the state too.
        scope.launch {
            combine(PlaybackWatcher.playing, PlaybackWatcher.listenerConnected) { p, c -> p to c }
                .debounce(PLAYING_DEBOUNCE_MS).collect { reportState() }
        }
    }

    /** Must run on every start command: startForegroundService requires startForeground within ~10 s. */
    private fun enterForeground() {
        val nm = getSystemService(NotificationManager::class.java)
        nm.createNotificationChannel(NotificationChannel(CHANNEL, getString(R.string.link_channel), NotificationManager.IMPORTANCE_MIN))
        val notification = Notification.Builder(this, CHANNEL)
            .setSmallIcon(android.R.drawable.ic_menu_info_details)
            .setContentTitle(getString(R.string.app_name))
            .build()
        // Android 14 requires the type here (the manifest declares specialUse); before it there is none to give.
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.UPSIDE_DOWN_CAKE) {
            startForeground(1, notification, ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE)
        } else {
            startForeground(1, notification)
        }
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        enterForeground()
        // Only (re)connect when reconfigured or not running: a restart mid-answer drops the socket
        // and the server would see the TV as offline while it waits for `shown`.
        if (shouldRestart(intent?.action, connectJob?.isActive == true)) {
            connectJob?.cancel()
            socket?.close(1000, null)
            connectJob = scope.launch { connectLoop() }
        }
        return START_STICKY
    }

    private val screenOnReceiver = object : BroadcastReceiver() {
        override fun onReceive(context: Context, intent: Intent) {
            if (intent.action == Intent.ACTION_SCREEN_ON) wakeReconnect()
            reportState()
        }
    }
    private val networkCallback = object : ConnectivityManager.NetworkCallback() {
        override fun onAvailable(network: Network) = wakeReconnect()
        // VPN up/down shows as a change of the default network's capabilities (or a new default network).
        override fun onCapabilitiesChanged(network: Network, caps: NetworkCapabilities) = reportState()
        override fun onLost(network: Network) = reportState()
    }

    /** The TV's low-power standby blocks the network while asleep, so backoff grows; retry at once on wake. */
    private fun registerWakeTriggers() {
        val screen = IntentFilter(Intent.ACTION_SCREEN_ON).apply { addAction(Intent.ACTION_SCREEN_OFF) }
        // System broadcasts only; the exported flag exists from Android 13 (and is required from 14).
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            registerReceiver(screenOnReceiver, screen, Context.RECEIVER_NOT_EXPORTED)
        } else {
            registerReceiver(screenOnReceiver, screen)
        }
        getSystemService(ConnectivityManager::class.java).registerDefaultNetworkCallback(networkCallback)
    }

    private fun wakeReconnect() {
        if (socket == null) wake.trySend(Unit)
    }

    override fun onDestroy() {
        instance = null
        MessageBus.setLinkState(LinkState.DOWN)
        NoticeOverlay.dismiss()
        runCatching { unregisterReceiver(screenOnReceiver) }.onFailure { Log.d(TAG, "receiver was not registered") }
        runCatching { getSystemService(ConnectivityManager::class.java).unregisterNetworkCallback(networkCallback) }
            .onFailure { Log.d(TAG, "network callback was not registered") }
        scope.coroutineContext[Job]?.cancel()
        socket?.close(1000, null)
        super.onDestroy()
    }

    private suspend fun connectLoop() {
        var backoffMs = 1_000L
        while (true) {
            val prefs = getSharedPreferences(PREFS, MODE_PRIVATE)
            val url = prefs.getString(PREF_URL, null)
            val token = prefs.getString(PREF_TOKEN, null)
            if (url.isNullOrBlank() || token.isNullOrBlank()) {
                MessageBus.setLinkState(LinkState.NOT_CONFIGURED)
                Log.i(TAG, "not configured; waiting for CONFIGURE broadcast")
                return
            }
            val closed = CompletableDeferredClose()
            val request = try {
                // the token goes in a header, never the URL (URLs end up in server and proxy logs)
                Request.Builder()
                    .url(toHttpScheme(url).toHttpUrl())
                    .header("Authorization", "Bearer $token")
                    .build()
            } catch (e: IllegalArgumentException) {
                MessageBus.setLinkState(LinkState.NOT_CONFIGURED)
                Log.e(TAG, "bad server url in config; waiting for CONFIGURE broadcast")
                return
            }
            socket = httpClient.newWebSocket(request, Listener(closed))
            val code = closed.await()
            socket = null
            val state = linkStateAfterClose(code)
            MessageBus.setLinkState(state)
            if (!state.reconnects) {
                // "server rejected token (4401)" verbatim: the README quotes it as the sign of a wrong token.
                Log.e(TAG, "server rejected token ($code); not retrying until reconfigured")
                return
            }
            // Code 4000 = another client replaced us; don't reset backoff or two clients fight at 1 s.
            if (closed.everOpened && code != CLOSE_REPLACED) backoffMs = 1_000L
            Log.i(TAG, "disconnected (code $code); retry in ${backoffMs}ms")
            wake.tryReceive() // drop a stale signal from before this failure
            val woken = withTimeoutOrNull(backoffMs) { wake.receive() } != null
            backoffMs = if (woken) 1_000L else nextBackoff(backoffMs)
        }
    }

    private inner class Listener(private val closed: CompletableDeferredClose) : WebSocketListener() {
        override fun onOpen(webSocket: WebSocket, response: Response) {
            Log.i(TAG, "connected")
            MessageBus.setLinkState(LinkState.CONNECTED)
            closed.everOpened = true
            sendState(webSocket, force = true)
            viewersSync(this@LinkService).onOpen { webSocket.send(it) }
        }

        override fun onMessage(webSocket: WebSocket, text: String) {
            val msg = parseServerMsg(text) ?: return Unit.also { Log.w(TAG, "ignored unknown or malformed message") }
            if (msg is ServerMsg.Show && msg.droppedPicks.isNotEmpty()) {
                Log.w(TAG, "show ${msg.answerId}: left out ${msg.droppedPicks.size} malformed pick(s): ${msg.droppedPicks.joinToString("; ")}")
            }
            scope.launch { handle(msg) }
        }

        override fun onClosed(webSocket: WebSocket, code: Int, reason: String) = closed.complete(code)

        override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
            Log.w(TAG, "socket failure: ${t.javaClass.simpleName}")
            closed.complete(response?.code ?: -1)
        }

        override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
            webSocket.close(if (code in 1000..4999 && code != 1005 && code != 1006) code else 1000, null)
            closed.complete(code)
        }
    }

    private suspend fun handle(msg: ServerMsg) {
        val askId = askIdOf(msg)
        // Only the newest ask's messages count (an older one was cancelled by BACK or a newer press), and an answer
        // that comes after the ask timed out, failed or was left is dropped too.
        if (!wantsAskMessage(AskController.state.value, msg)) {
            Log.i(TAG, "ignored a message for ask $askId (not waited for)")
            return
        }
        // A message for the TV's own ask belongs on Lumi's ask screen, which the press put in front of any video.
        val answerOnTop = AnswerActivity.onTop || askId != null
        // The server never sends a Desktop `looking` while playing (or not knowing); guard anyway so playback is never covered.
        if (msg is ServerMsg.Looking &&
            route(PlaybackWatcher.isPlaying(), PlaybackWatcher.connected, answerOnTop) == Mode.NOTICE
        ) return
        // Posters first: the fetches overlap the activity start, so AnswerActivity finds them in the
        // shared loader's memory cache instead of starting three cold downloads.
        if (msg is ServerMsg.Show) prefetchPosters(applicationContext, posterUrls(msg.picks))
        MessageBus.publish(msg)
        when (msg) {
            is ServerMsg.Show -> {
                val action = answerAction(
                    route(PlaybackWatcher.isPlaying(), PlaybackWatcher.connected, answerOnTop),
                    interactive = isInteractive(),
                    canDrawOverlays = Settings.canDrawOverlays(this),
                    answerOnTop = answerOnTop,
                )
                val shown = when (action) {
                    AnswerAction.SCREEN -> { MessageBus.markRender(RenderPhase.START); showAnswerScreen(); true }
                    // The answer stays in MessageBus; OK on the notice opens AnswerActivity with it.
                    // If the pill cannot be drawn, say nothing (see AnswerAction.NONE). We do not fall back to
                    // the full screen: that would cover the video, and it needs the same overlay permission.
                    AnswerAction.NOTICE -> NoticeOverlay.show(this)
                    // Not woken; the answer waits in MessageBus for when Lumi is opened.
                    AnswerAction.TV_OFF -> { reportState(); true }
                    AnswerAction.NONE -> {
                        Log.w(TAG, "can't draw over other apps, so no answer shown: run `adb shell appops set $packageName SYSTEM_ALERT_WINDOW allow`")
                        false
                    }
                }
                // Ack as soon as the start is issued, independent of Compose; the server waits 5 s.
                if (shown) action.ack?.let { send(shownMsg(msg.answerId, it)) }
                AskController.dispatch(AskEvent.ShowArrived(msg.askId))
            }
            is ServerMsg.Looking -> {
                showAnswerScreen()
                // No show may follow (the turn can fail server-side): don't leave "Looking" up forever.
                scope.launch {
                    delay(LOOKING_EXPIRY_MS)
                    MessageBus.expireLooking(msg.turnId)
                }
            }
            is ServerMsg.Open -> {
                val strings = UiStrings.from(MessageBus.hello.value?.strings)
                val label = msg.serviceLabel.ifBlank { appLabel(this, msg.launches.last().packageName) }
                val warning = decideLaunch(msg.launches.first(), isVpnUp(this), label, strings).vpnWarning
                NoticeOverlay.dismiss()
                val outcome = openTitle(this, msg.launches, msg.title, msg.searchHint, warning, strings)
                val opened = outcome.opened
                if (opened && !outcome.keepAnswer) {
                    // Like a pick: the answer is done, never replay it from the launcher.
                    MessageBus.consume()
                }
                // A title took over the screen: an ask's paused video stays paused.
                if (opened) AskController.dispatch(AskEvent.Opened)
                val notice = if (opened) null else pillText(strings.cantOpen(label), warning)
                if (notice != null) {
                    MessageBus.setNotice(notice)
                    showAnswerScreen()
                }
            }
            is ServerMsg.Heard -> AskController.dispatch(AskEvent.Heard(msg.askId, msg.text))
            is ServerMsg.Preview -> Unit // MessageBus holds it for the Looking screen
            is ServerMsg.Viewers -> viewersSync(this).onEcho(msg.viewers)
            is ServerMsg.AskError -> AskController.dispatch(AskEvent.Error(msg.askId, msg.code))
            is ServerMsg.AskReply -> AskController.dispatch(AskEvent.Reply(msg.askId, msg.text))
            is ServerMsg.Hello -> {
                Viewers.kidsFrom = msg.kidsFrom
                Viewers.kidsUntil = msg.kidsUntil
                viewersSync(this).onHello(msg.viewers)
            }
        }
    }

    private fun showAnswerScreen() {
        NoticeOverlay.dismiss()
        try {
            startActivity(
                Intent(this, AnswerActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_SINGLE_TOP),
            )
        } catch (e: RuntimeException) {
            Log.e(TAG, "could not start the answer screen: ${e.javaClass.simpleName}")
        }
    }

    private fun isInteractive(): Boolean = getSystemService(PowerManager::class.java).isInteractive

    private fun currentState(): String = stateMsg(
        isInteractive(),
        playing = PlaybackWatcher.isPlaying(),
        vpn = isVpnUp(this),
        playingKnown = PlaybackWatcher.connected,
    )

    /** Sends `state` when screen_on/playing/vpn differ from what the server last got. */
    private fun reportState() {
        socket?.let { sendState(it, force = false) }
    }

    private fun sendState(ws: WebSocket, force: Boolean) = stateDedupe.offer(currentState(), force) { ws.send(it) }

    companion object {
        private const val CHANNEL = "link"
        const val CLOSE_BAD_TOKEN = 4401

        @Volatile private var instance: LinkService? = null
        const val CLOSE_REPLACED = 4000

        /** Sends a TV->server message; false when not connected. */
        fun send(json: String): Boolean = instance?.socket?.send(json) ?: false

        @Volatile private var viewersSync: ViewersSync? = null

        /** The one [ViewersSync], on the app's prefs (the same ones the link reads its url and token from). */
        private fun viewersSync(context: Context): ViewersSync =
            viewersSync ?: ViewersSync(context.applicationContext.getSharedPreferences(PREFS, Context.MODE_PRIVATE)).also { viewersSync = it }

        /** The household as edited on the "Who's here" screen: shown now, sent now or on the next connect. */
        fun saveViewers(context: Context, people: List<Viewer>) = viewersSync(context).save(people, ::send)

        /**
         * After a pick opened in [pkg]: tells the server whether anything played there within [PLAY_CHECK_MS]
         * (checked every [PLAY_CHECK_STEP_MS]). Netflix's previews count as playing, so "played" only ever helps a
         * title; "didn't play" is the signal that its listing may be wrong.
         */
        fun watchPlay(token: String, service: String, pkg: String) {
            if (token.isBlank() || service.isBlank()) return
            val service0 = instance ?: return
            service0.scope.launch {
                var waited = 0L
                while (waited < PLAY_CHECK_MS) {
                    delay(PLAY_CHECK_STEP_MS)
                    waited += PLAY_CHECK_STEP_MS
                    if (pkg in PlaybackWatcher.playingPackages()) {
                        send(playCheckMsg(token, service, true))
                        return@launch
                    }
                }
                if (PlaybackWatcher.connected) send(playCheckMsg(token, service, false)) // unknown without the listener
            }
        }

        /** Starts the service, or re-delivers a start command (keeping the connection) carrying [command]. */
        fun start(context: Context, command: String? = null) {
            context.startForegroundService(Intent(context, LinkService::class.java).also { i -> command?.let(i::setAction) })
        }

        /** [start] for callers that may run where Android refuses a foreground start; logs instead of crashing. */
        fun startGuarded(context: Context, command: String? = null): Boolean = try {
            start(context, command)
            true
        } catch (e: IllegalStateException) { // ForegroundServiceStartNotAllowedException
            Log.w(TAG, "foreground service start refused: ${e.javaClass.simpleName}")
            false
        } catch (e: SecurityException) {
            Log.w(TAG, "foreground service start refused: ${e.javaClass.simpleName}")
            false
        }

        /** Starts the service only when it is not running, so an activity never causes a reconnect. */
        fun ensureRunning(context: Context) {
            if (instance == null) start(context)
        }

        /** After CONFIGURE: restart the connect loop so a new url/token takes effect; false when Android refused. */
        fun reconfigure(context: Context): Boolean = startGuarded(context, ACTION_RECONFIGURE)

        const val LOOKING_EXPIRY_MS = 30_000L
        const val PLAYING_DEBOUNCE_MS = 1_000L
    }
}

const val ACTION_RECONFIGURE = "io.github.miladaslaner.lumitv.RECONFIGURE"

/** What to do with a `show`, and the `shown` mode to ack (null: no ack, so the server reports the TV unreachable). */
enum class AnswerAction(val ack: String?) { SCREEN("screen"), NOTICE("notice"), TV_OFF("tv_off"), NONE(null) }

/**
 * Only claim what the user can see. Screen off: ack `tv_off` and don't wake it. Without the overlay grant a
 * background service can neither draw the pill nor start the answer screen (unless Lumi is already on top), and
 * Android drops that start silently, so: no ack.
 */
fun answerAction(mode: Mode, interactive: Boolean, canDrawOverlays: Boolean, answerOnTop: Boolean): AnswerAction = when {
    !interactive -> AnswerAction.TV_OFF
    mode == Mode.NOTICE -> if (canDrawOverlays) AnswerAction.NOTICE else AnswerAction.NONE
    canDrawOverlays || answerOnTop -> AnswerAction.SCREEN
    else -> AnswerAction.NONE
}

/** The TV ask a server message belongs to; null for Claude Desktop's messages. */
fun askIdOf(msg: ServerMsg): String? = when (msg) {
    is ServerMsg.Show -> msg.askId
    is ServerMsg.Looking -> msg.askId
    is ServerMsg.Heard -> msg.askId
    is ServerMsg.Preview -> msg.askId
    is ServerMsg.AskError -> msg.askId
    is ServerMsg.AskReply -> msg.askId
    is ServerMsg.Hello, is ServerMsg.Open, is ServerMsg.Viewers -> null
}

/** A start command restarts the connect loop only when reconfigured or when no loop is running. */
fun shouldRestart(action: String?, loopRunning: Boolean): Boolean = action == ACTION_RECONFIGURE || !loopRunning

/** Maps the configured ws/wss URL to the http/https form OkHttp's HttpUrl parser needs. */
fun toHttpScheme(url: String): String = when {
    url.startsWith("ws://") -> "http://" + url.removePrefix("ws://")
    url.startsWith("wss://") -> "https://" + url.removePrefix("wss://")
    else -> url
}

fun isWebSocketUrl(url: String): Boolean = url.startsWith("ws://") || url.startsWith("wss://")

/** Next reconnect delay: doubles from 1 s up to a 30 s cap. */
fun nextBackoff(currentMs: Long): Long = minOf(currentMs * 2, 30_000L)

/**
 * The link after the socket closed with [code]: the server's 4401, or an HTTP 401/403 on the upgrade (a proxy in
 * front of it), mean the token was refused and only CONFIGURE can help; anything else is a drop to retry.
 */
fun linkStateAfterClose(code: Int): LinkState =
    if (code == LinkService.CLOSE_BAD_TOKEN || code == 401 || code == 403) LinkState.REJECTED else LinkState.DOWN

/** Tiny one-shot latch carrying the close code. */
class CompletableDeferredClose {
    private val deferred = kotlinx.coroutines.CompletableDeferred<Int>()
    @Volatile var everOpened = false
    fun complete(code: Int) { deferred.complete(code) }
    suspend fun await(): Int = deferred.await()
}
