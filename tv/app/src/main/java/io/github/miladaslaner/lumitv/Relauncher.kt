package io.github.miladaslaner.lumitv

import android.app.AppOpsManager
import android.app.usage.UsageEvents
import android.app.usage.UsageStatsManager
import android.content.Context
import android.os.Handler
import android.os.Looper
import android.os.Process
import android.util.Log
import io.github.miladaslaner.lumitv.ui.UiStrings

/** An activity of [packageName] was resumed, paused or stopped at [timeMs] (wall clock). */
data class AppEvent(val packageName: String, val type: Type, val timeMs: Long) {
    enum class Type { RESUMED, PAUSED, STOPPED }
}

/**
 * Activity events from usage stats, read only for the apps a launch targets and for Lumi itself (both visible to
 * Lumi: the manifest's `<queries>` covers every TV app). Needs usage access, granted once over adb
 * (`appops set io.github.miladaslaner.lumitv GET_USAGE_STATS allow`); without it nothing is re-sent.
 */
object UsageEventsReader {
    /** How far back [targetRecentlyUsed] looks. */
    const val LOOKBACK_MS = 60 * 60_000L

    fun hasAccess(context: Context): Boolean {
        val ops = context.getSystemService(AppOpsManager::class.java)
        return ops.unsafeCheckOpNoThrow(AppOpsManager.OPSTR_GET_USAGE_STATS, Process.myUid(), context.packageName) ==
            AppOpsManager.MODE_ALLOWED
    }

    /** Events of [packages] since [sinceMs], oldest first; null without usage access. */
    fun events(context: Context, packages: Set<String>, sinceMs: Long, nowMs: Long = System.currentTimeMillis()): List<AppEvent>? {
        if (!hasAccess(context)) return null
        val events = context.getSystemService(UsageStatsManager::class.java).queryEvents(sinceMs, nowMs)
        val out = mutableListOf<AppEvent>()
        val e = UsageEvents.Event()
        while (events.getNextEvent(e)) {
            if (e.packageName !in packages) continue
            val type = when (e.eventType) {
                UsageEvents.Event.ACTIVITY_RESUMED -> AppEvent.Type.RESUMED
                UsageEvents.Event.ACTIVITY_PAUSED -> AppEvent.Type.PAUSED
                UsageEvents.Event.ACTIVITY_STOPPED -> AppEvent.Type.STOPPED
                else -> null
            } ?: continue
            out += AppEvent(e.packageName, type, e.timeStamp)
        }
        return out
    }
}

/** A cold start is likely when the target app had no activity at all within the lookback (its process is gone). */
fun targetRecentlyUsed(target: String, events: List<AppEvent>, nowMs: Long, windowMs: Long = UsageEventsReader.LOOKBACK_MS): Boolean =
    events.any { it.packageName == target && nowMs - it.timeMs <= windowMs }

/** What to do after [launched] opened. */
enum class RelaunchPlan {
    /** Nothing: the step has no `relaunch_after_s`, or its app was in use recently, so the link landed. */
    NONE,
    /** Cold start: send the link again on schedule, with the "pick your profile" pill. */
    RESEND,
    /** Can't tell whether it was a cold start (no usage access): ask the user to press OK again. */
    ASK_RETRY,
}

fun relaunchPlan(launched: Launch, usageAccess: Boolean, targetRecentlyUsed: Boolean): RelaunchPlan = when {
    launched.relaunchAfterS.isEmpty() -> RelaunchPlan.NONE
    !usageAccess -> RelaunchPlan.ASK_RETRY
    targetRecentlyUsed -> RelaunchPlan.NONE
    else -> RelaunchPlan.RESEND
}

/**
 * One re-send at most, at the first configured delay. Playback state can't guard it (on a cold start Netflix reports
 * PLAYING on its profile picker and home preview), so a second re-send could pull the user out of a video they started.
 */
fun relaunchDelaysMs(launched: Launch): List<Long> = launched.relaunchAfterS.take(1).map { (it * 1000).toLong() }

/**
 * Re-send only while the user is still in the target app: stop on Lumi coming back, or once the target's own last
 * event since the launch is a pause/stop (the user left it: Home, Back, another app). Unknown events (null) stop it too.
 * Playback is not checked: after a cold start the target's previews report PLAYING (Netflix does), and one re-send
 * ~10 s after a cold start comes before anyone can have picked a profile and started a video.
 */
fun keepRelaunching(target: String, ownPackage: String, eventsSinceLaunch: List<AppEvent>?): Boolean {
    if (eventsSinceLaunch == null) return false
    if (eventsSinceLaunch.any { it.packageName == ownPackage && it.type == AppEvent.Type.RESUMED }) return false
    val last = eventsSinceLaunch.lastOrNull { it.packageName == target } ?: return true
    return last.type == AppEvent.Type.RESUMED
}

/** Re-sends a title link once after a cold app start, so it lands once the user has picked a profile. Main thread only. */
object Relauncher {
    private val handler = Handler(Looper.getMainLooper())

    fun cancel() = handler.removeCallbacksAndMessages(null)

    fun start(context: Context, launched: Launch, startedAtMs: Long) {
        cancel()
        val app = context.applicationContext
        relaunchDelaysMs(launched).forEach { d -> handler.postDelayed({ resend(app, launched, startedAtMs) }, d) }
    }

    private fun resend(app: Context, launched: Launch, startedAtMs: Long) {
        val events = UsageEventsReader.events(app, setOf(launched.packageName, app.packageName), startedAtMs)
        if (!keepRelaunching(launched.packageName, app.packageName, events)) {
            Log.i(TAG, "not re-sending to ${launched.packageName} (left it, Lumi back, or no usage events)")
            cancel()
            return
        }
        try {
            Log.i(TAG, "re-sending the link to ${launched.packageName}")
            app.startActivity(buildIntent(launched, app.packageManager))
        } catch (e: RuntimeException) {
            Log.w(TAG, "re-send to ${launched.packageName} failed", e)
            cancel()
        }
    }

    /** How long the "pick your profile" pill stays: past the last re-send. */
    fun pillTimeoutMs(launched: Launch): Long =
        maxOf(NoticeOverlay.HINT_TIMEOUT_MS, ((launched.relaunchAfterS.maxOrNull() ?: 0.0) * 1000).toLong() + 4_000L)
}

/** [opened]: anything started. [keepAnswer]: the user was asked to press OK in Lumi again, so keep the answer. */
data class OpenOutcome(val opened: Boolean, val keepAnswer: Boolean = false)

/**
 * Opens a title from its ordered [launches] and shows what applies in one pill: the search hint (only after the
 * app's search link), the profile note (re-sending, or asking to press OK again), and the VPN [warning].
 * Main thread only.
 */
fun openTitle(
    context: Context, launches: List<Launch>, title: String, searchHint: String?, warning: String?, strings: UiStrings,
): OpenOutcome {
    Relauncher.cancel() // a new open supersedes any re-send still pending from an earlier one
    val nowMs = System.currentTimeMillis()
    val targets = launches.map { it.packageName }.toSet()
    val before = UsageEventsReader.events(context, targets, nowMs - UsageEventsReader.LOOKBACK_MS, nowMs)
    val launched = launchTitle(context, launches) ?: return OpenOutcome(opened = false)
    val plan = relaunchPlan(
        launched,
        usageAccess = before != null,
        targetRecentlyUsed = before != null && targetRecentlyUsed(launched.packageName, before, nowMs),
    )
    val profile = when (plan) {
        RelaunchPlan.RESEND -> strings.profileRelaunch(title.ifBlank { "…" })
        RelaunchPlan.ASK_RETRY -> strings.profileRetry
        RelaunchPlan.NONE -> null
    }
    if (plan == RelaunchPlan.RESEND) Relauncher.start(context, launched, nowMs)
    if (plan == RelaunchPlan.ASK_RETRY) Log.i(TAG, "no usage access: ${launched.packageName} link not re-sent")
    pillText(searchHintAfter(launched, searchHint, strings), profile, warning)?.let {
        NoticeOverlay.showHint(context, it, if (profile != null) Relauncher.pillTimeoutMs(launched) else NoticeOverlay.HINT_TIMEOUT_MS)
    }
    return OpenOutcome(opened = true, keepAnswer = plan == RelaunchPlan.ASK_RETRY)
}
