package io.github.miladaslaner.lumitv

import android.content.ActivityNotFoundException
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.net.ConnectivityManager
import android.net.NetworkCapabilities
import android.net.Uri
import android.util.Log

/** The intent to start, and a [vpnWarning] to show beside it when the VPN state is wrong for the service. */
data class LaunchDecision(val intent: Intent, val vpnWarning: String?)

fun isAppLaunch(launch: Launch) = launch.action == Intent.ACTION_MAIN && launch.data == null

/**
 * The intent for a launch. VIEW/SEARCH (and anything else with a deep link) are explicit to the package; a bare
 * MAIN is the app itself, resolved through the package manager (leanback entry first) when [pm] is given.
 */
fun buildIntent(launch: Launch, pm: PackageManager? = null): Intent {
    if (pm != null && isAppLaunch(launch)) {
        (pm.getLeanbackLaunchIntentForPackage(launch.packageName) ?: pm.getLaunchIntentForPackage(launch.packageName))
            ?.let { return it.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK) }
    }
    val intent = Intent(launch.action).setPackage(launch.packageName).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
    launch.component?.let { ComponentName.unflattenFromString(it) }?.let { intent.component = it }
    launch.data?.let { intent.data = Uri.parse(it) }
    launch.extras.forEach { (k, v) -> intent.putExtra(k, v) }
    // A bare MAIN needs a category to resolve to the app's TV entry point.
    if (isAppLaunch(launch)) intent.addCategory(Intent.CATEGORY_LEANBACK_LAUNCHER)
    return intent
}

/** The VPN state never blocks a launch: a wrong state only adds a warning to show with it. */
fun decideLaunch(
    launch: Launch, vpnUp: Boolean, serviceLabel: String,
    strings: io.github.miladaslaner.lumitv.ui.UiStrings = io.github.miladaslaner.lumitv.ui.UiStrings.Default,
): LaunchDecision = LaunchDecision(
    buildIntent(launch),
    when {
        launch.vpn == "on" && !vpnUp -> strings.needsVpnOn(serviceLabel)
        launch.vpn == "off" && vpnUp -> strings.needsVpnOff(serviceLabel)
        else -> null
    },
)

/**
 * Starts the first of [launches] that opens (they are ordered: e.g. Google TV's title page, the app's own link,
 * the plain app). A step whose app is missing or refuses is skipped. Last resort is the plain launch of the last
 * step's app. Returns the step that started, or null when nothing could be started.
 */
fun launchTitle(context: Context, launches: List<Launch>): Launch? {
    val pm = context.packageManager
    for (step in launches) {
        try {
            context.startActivity(buildIntent(step, pm))
            return step
        } catch (e: ActivityNotFoundException) {
            Log.w(TAG, "no activity for ${step.packageName}; trying the next step")
        } catch (e: SecurityException) {
            Log.w(TAG, "system refused to open ${step.packageName}; trying the next step")
        }
    }
    val last = launches.lastOrNull() ?: return null
    if (!isAppLaunch(last)) {
        val app = Launch(last.packageName, Intent.ACTION_MAIN, vpn = last.vpn)
        try {
            context.startActivity(buildIntent(app, pm))
            return app
        } catch (e: RuntimeException) {
            Log.e(TAG, "cannot open ${last.packageName}")
        }
    }
    return null
}

/** The search-hint text to show after [launched], or null: only when the step that opened is the app's search
 * link (Google TV's page or the plain app launch land elsewhere, where "Search for …" would mislead). */
fun searchHintAfter(launched: Launch?, hint: String?, strings: io.github.miladaslaner.lumitv.ui.UiStrings): String? =
    if (launched?.hint == true && hint != null) strings.searchHint(hint) else null

fun isVpnUp(context: Context): Boolean {
    val cm = context.getSystemService(ConnectivityManager::class.java)
    return cm.getNetworkCapabilities(cm.activeNetwork)?.hasTransport(NetworkCapabilities.TRANSPORT_VPN) == true
}

/** The installed app's label, for an `open` that carries no pick. */
fun appLabel(context: Context, packageName: String): String = try {
    val pm = context.packageManager
    pm.getApplicationLabel(pm.getApplicationInfo(packageName, 0)).toString()
} catch (e: PackageManager.NameNotFoundException) {
    packageName
}

/** One pill line from whatever applies (search hint first, then the VPN warning), or null when nothing does. */
fun pillText(vararg parts: String?): String? = parts.filterNotNull().joinToString(" · ").ifEmpty { null }
