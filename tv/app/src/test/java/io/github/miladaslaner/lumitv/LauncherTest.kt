package io.github.miladaslaner.lumitv

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner

@RunWith(RobolectricTestRunner::class)
class LauncherTest {
    private val videoland = Launch("nl.rtl.videoland.v2", "android.intent.action.VIEW", "videoland://title/1", mapOf("k" to "v"), vpn = "on")
    private val netflix = Launch("com.netflix.ninja", "android.intent.action.VIEW", "https://www.netflix.com/title/1", vpn = "off")
    private val prime = Launch("com.amazon.amazonvideo.livingroom", "android.intent.action.VIEW", "https://www.primevideo.com/detail/1", vpn = "any")

    private val strings = io.github.miladaslaner.lumitv.ui.UiStrings.Default

    @Test fun videolandWarnsWithoutVpnButStillLaunches() {
        val d = decideLaunch(videoland, vpnUp = false, serviceLabel = "Videoland", strings = strings)
        assertEquals("Videoland needs the VPN on", d.vpnWarning)
        assertEquals("nl.rtl.videoland.v2", d.intent.`package`)
        assertEquals(null, decideLaunch(videoland, vpnUp = true, serviceLabel = "Videoland", strings = strings).vpnWarning)
    }

    @Test fun netflixWarnsWithVpnOnButStillLaunches() {
        val d = decideLaunch(netflix, vpnUp = true, serviceLabel = "Netflix", strings = strings)
        assertEquals("Netflix may not work while the VPN is on", d.vpnWarning)
        assertEquals("https://www.netflix.com/title/1", d.intent.dataString)
        assertEquals(null, decideLaunch(netflix, vpnUp = false, serviceLabel = "Netflix", strings = strings).vpnWarning)
    }

    @Test fun primeNeverWarns() {
        assertEquals(null, decideLaunch(prime, vpnUp = true, serviceLabel = "Prime Video").vpnWarning)
        assertEquals(null, decideLaunch(prime, vpnUp = false, serviceLabel = "Prime Video").vpnWarning)
    }

    @Test fun pillCombinesHintAndWarning() {
        assertEquals("Search for “X” · Netflix may not work while the VPN is on", pillText("Search for “X”", "Netflix may not work while the VPN is on"))
        assertEquals("Search for “X”", pillText("Search for “X”", null))
        assertEquals("warn", pillText(null, "warn"))
        assertEquals(null, pillText(null, null))
    }

    @Test fun buildIntentSetsPackageActionDataExtras() {
        val i = buildIntent(videoland)
        assertEquals("nl.rtl.videoland.v2", i.`package`)
        assertEquals("android.intent.action.VIEW", i.action)
        assertEquals("videoland://title/1", i.dataString)
        assertEquals("v", i.getStringExtra("k"))
    }

    @Test fun buildIntentWithoutDataIsAppLaunch() {
        val i = buildIntent(Launch("com.example.app", "android.intent.action.MAIN"))
        assertEquals(null, i.data)
        assertTrue(i.categories.contains("android.intent.category.LEANBACK_LAUNCHER"))
    }

    @Test fun searchIntentIsExplicitToThePackageWithQueryExtra() {
        val i = buildIntent(Launch("nl.rtl.videoland.v2", "android.intent.action.SEARCH", extras = mapOf("query" to "Bluey")))
        assertEquals("android.intent.action.SEARCH", i.action)
        assertEquals("nl.rtl.videoland.v2", i.`package`)
        assertEquals("Bluey", i.getStringExtra("query"))
        assertEquals(null, i.data)
        assertTrue(i.flags and android.content.Intent.FLAG_ACTIVITY_NEW_TASK != 0)
    }

    @Test fun viewIntentCarriesUriPackageAndNewTask() {
        val i = buildIntent(Launch("com.netflix.ninja", "android.intent.action.VIEW", "https://www.netflix.com/search?q=Tom%20%26%20Jerry"))
        assertEquals("https://www.netflix.com/search?q=Tom%20%26%20Jerry", i.dataString)
        assertEquals("com.netflix.ninja", i.`package`)
        assertTrue(i.flags and android.content.Intent.FLAG_ACTIVITY_NEW_TASK != 0)
    }

    @Test fun appLaunchPrefersLeanbackThenPlainLaunchIntent() {
        val pm = org.robolectric.Shadows.shadowOf(app.packageManager)
        val ai = android.content.pm.ActivityInfo().apply { packageName = "x.app"; name = "x.app.Main" }
        fun install(category: String) {
            pm.addOrUpdateActivity(ai.apply { applicationInfo = android.content.pm.ApplicationInfo().apply { packageName = "x.app" } })
            pm.addIntentFilterForActivity(
                android.content.ComponentName("x.app", "x.app.Main"),
                android.content.IntentFilter(android.content.Intent.ACTION_MAIN).apply { addCategory(category) },
            )
        }
        val main = Launch("x.app", "android.intent.action.MAIN")
        assertEquals(null, buildIntent(main, app.packageManager).component)  // not installed: plain MAIN intent
        install(android.content.Intent.CATEGORY_LAUNCHER)
        assertEquals("x.app.Main", buildIntent(main, app.packageManager).component?.className)
        install(android.content.Intent.CATEGORY_LEANBACK_LAUNCHER)
        val i = buildIntent(main, app.packageManager)
        assertEquals("x.app.Main", i.component?.className)
        assertTrue(i.categories.contains(android.content.Intent.CATEGORY_LEANBACK_LAUNCHER))
    }

    private val netflixApp = Launch("com.netflix.ninja", "android.intent.action.MAIN", vpn = "off")

    @Test fun launchFallsBackToTheAppWhenDeepLinkHasNoActivity() {
        val ctx = FailingContext(app) { if (it.action == "android.intent.action.VIEW") android.content.ActivityNotFoundException() else null }
        assertEquals(netflixApp, launchTitle(ctx, listOf(netflix, netflixApp)))
        assertEquals("android.intent.action.MAIN", ctx.started.single().action)
    }

    private class FailingContext(base: android.content.Context, private val failFor: (android.content.Intent) -> Throwable?) :
        android.content.ContextWrapper(base) {
        val started = mutableListOf<android.content.Intent>()
        override fun startActivity(intent: android.content.Intent) {
            failFor(intent)?.let { throw it }
            started += intent
        }
    }

    private val app = org.robolectric.RuntimeEnvironment.getApplication()

    private val googleTv = Launch("com.google.android.apps.tv.launcherx", "android.intent.action.VIEW", "https://tv.google.com/asset/%2Fg%2F1", vpn = "any")
    private val primeSearch = Launch("com.amazon.amazonvideo.livingroom", "android.intent.action.VIEW", "https://app.primevideo.com/search?phrase=x", hint = true)
    private val primeApp = Launch("com.amazon.amazonvideo.livingroom", "android.intent.action.MAIN")
    private val steps = listOf(googleTv, primeSearch, primeApp)

    @Test fun stepsAreTriedInOrderAndTheFirstThatStartsWins() {
        val ctx = FailingContext(app) { null }
        assertEquals(googleTv, launchTitle(ctx, steps))
        assertEquals(listOf("com.google.android.apps.tv.launcherx"), ctx.started.map { it.`package` })
        val noGoogle = FailingContext(app) { if (it.`package` == googleTv.packageName) android.content.ActivityNotFoundException() else null }
        assertEquals(primeSearch, launchTitle(noGoogle, steps))
        assertEquals("https://app.primevideo.com/search?phrase=x", noGoogle.started.single().dataString)
        val refused = FailingContext(app) { if (it.`package` == googleTv.packageName) SecurityException("no") else null }
        assertEquals(primeSearch, launchTitle(refused, steps))  // one app refusing doesn't block the next step
        val onlyApp = FailingContext(app) { if (it.action == "android.intent.action.VIEW") android.content.ActivityNotFoundException() else null }
        assertEquals(primeApp, launchTitle(onlyApp, steps))
    }

    @Test fun lastResortIsTheLastStepsApp() {
        val ctx = FailingContext(app) { if (it.action == "android.intent.action.VIEW") android.content.ActivityNotFoundException() else null }
        assertEquals("android.intent.action.MAIN", launchTitle(ctx, listOf(googleTv, primeSearch))?.action)
        assertEquals("com.amazon.amazonvideo.livingroom", ctx.started.single().`package`)
        assertEquals(null, launchTitle(FailingContext(app) { SecurityException("no") }, steps))
        assertEquals(null, launchTitle(FailingContext(app) { null }, emptyList()))
    }

    @Test fun searchHintOnlyAfterTheSearchStep() {
        val strings = io.github.miladaslaner.lumitv.ui.UiStrings.Default
        assertEquals("Search for “Heat”", searchHintAfter(primeSearch, "Heat", strings))
        assertEquals(null, searchHintAfter(googleTv, "Heat", strings))  // Google TV's page: nothing to type
        assertEquals(null, searchHintAfter(primeApp, "Heat", strings))
        assertEquals(null, searchHintAfter(null, "Heat", strings))
        assertEquals(null, searchHintAfter(primeSearch, null, strings))
    }

    @Test fun componentMakesTheIntentExplicit() {
        val link = Launch("com.netflix.ninja", "android.intent.action.VIEW", "https://www.netflix.com/title/70195800",
            mapOf("source" to "30"), vpn = "off", component = "com.netflix.ninja/.MainActivity")
        val i = buildIntent(link)
        assertEquals("com.netflix.ninja.MainActivity", i.component?.className)
        assertEquals("com.netflix.ninja", i.component?.packageName)
        assertEquals("30", i.getStringExtra("source"))
        assertTrue(i.flags and android.content.Intent.FLAG_ACTIVITY_NEW_TASK != 0)
    }

    private val netflixLink = Launch("com.netflix.ninja", "android.intent.action.VIEW", "https://www.netflix.com/title/1",
        vpn = "off", relaunchAfterS = listOf(8.0, 16.0))

    private val nf = "com.netflix.ninja"
    private val lumi = "io.github.miladaslaner.lumitv"
    private fun ev(pkg: String, type: AppEvent.Type, t: Long) = AppEvent(pkg, type, t)

    @Test fun coldMeansTheTargetAppItselfWasNotUsedRecently() {
        val now = 10 * 3600_000L
        // Lumi being the last app in front says nothing: Netflix was used 10 minutes ago, so it is warm
        val warm = listOf(ev(nf, AppEvent.Type.STOPPED, now - 10 * 60_000L), ev(lumi, AppEvent.Type.RESUMED, now - 60_000L))
        assertTrue(targetRecentlyUsed(nf, warm, now))
        assertEquals(false, targetRecentlyUsed(nf, listOf(ev(nf, AppEvent.Type.STOPPED, now - 2 * 3600_000L)), now))
        assertEquals(false, targetRecentlyUsed(nf, listOf(ev(lumi, AppEvent.Type.RESUMED, now - 1_000L)), now))
    }

    @Test fun relaunchPlanResendsOnlyOnAColdStart() {
        assertEquals(RelaunchPlan.RESEND, relaunchPlan(netflixLink, usageAccess = true, targetRecentlyUsed = false))
        assertEquals(RelaunchPlan.NONE, relaunchPlan(netflixLink, usageAccess = true, targetRecentlyUsed = true))
        assertEquals(RelaunchPlan.ASK_RETRY, relaunchPlan(netflixLink, usageAccess = false, targetRecentlyUsed = false))
        assertEquals(RelaunchPlan.NONE, relaunchPlan(netflixApp, usageAccess = false, targetRecentlyUsed = false))
    }

    @Test fun atMostOneResendAtTheFirstDelay() {
        // a second re-send could pull the user out of a video they started after the first one landed
        assertEquals(listOf(8_000L), relaunchDelaysMs(netflixLink))
        assertEquals(emptyList<Long>(), relaunchDelaysMs(netflixApp))
    }

    @Test fun stopOnceTheUserLeavesTheTargetApp() {
        val t = AppEvent.Type.RESUMED
        assertTrue(keepRelaunching(nf, lumi, emptyList()))  // still starting
        // profile picker -> main activity inside Netflix: its last event is a resume
        assertTrue(keepRelaunching(nf, lumi, listOf(ev(nf, t, 1), ev(nf, AppEvent.Type.PAUSED, 2), ev(nf, t, 3))))
        // Home (the launcher's own events may be invisible): Netflix paused and stopped, nothing after
        assertEquals(false, keepRelaunching(nf, lumi, listOf(ev(nf, t, 1), ev(nf, AppEvent.Type.PAUSED, 2), ev(nf, AppEvent.Type.STOPPED, 3))))
        assertEquals(false, keepRelaunching(nf, lumi, listOf(ev(nf, t, 1), ev(nf, AppEvent.Type.PAUSED, 2))))
        assertEquals(false, keepRelaunching(nf, lumi, listOf(ev(nf, t, 1), ev(lumi, t, 2), ev(nf, t, 3))))  // Lumi came back
        assertEquals(false, keepRelaunching(nf, lumi, null))  // lost usage access
    }

    @Test fun playingNowIgnoresARecentPause() {
        assertTrue(isPlayingNow(listOf(SessionSnapshot(android.media.session.PlaybackState.STATE_PLAYING))))
        assertTrue(isPlayingNow(listOf(SessionSnapshot(android.media.session.PlaybackState.STATE_BUFFERING))))
        assertEquals(false, isPlayingNow(listOf(SessionSnapshot(android.media.session.PlaybackState.STATE_PAUSED, pausedSinceMs = 1))))
        assertEquals(false, isPlayingNow(emptyList()))
    }

    @Test fun profilePillOutlastsTheLastResend() {
        assertEquals(20_000L, Relauncher.pillTimeoutMs(netflixLink))
        assertEquals(NoticeOverlay.HINT_TIMEOUT_MS, Relauncher.pillTimeoutMs(netflix))
        assertEquals("Pick your profile — Lumi will open “Suits”",
            io.github.miladaslaner.lumitv.ui.UiStrings.Default.profileRelaunch("Suits"))
        assertEquals("Pick your profile, then open Lumi and press OK on the title again", io.github.miladaslaner.lumitv.ui.UiStrings.Default.profileRetry)
    }

    @Test fun launchReturnsNothingWhenSystemRefuses() {
        val ctx = FailingContext(app) { SecurityException("no") }
        assertEquals(null, launchTitle(ctx, listOf(netflix)))
    }

    @Test fun startCommandRestartsOnlyWhenReconfiguredOrNotRunning() {
        assertEquals(false, shouldRestart(null, loopRunning = true))
        assertEquals(true, shouldRestart(null, loopRunning = false))
        assertEquals(true, shouldRestart(ACTION_RECONFIGURE, loopRunning = true))
    }
}
