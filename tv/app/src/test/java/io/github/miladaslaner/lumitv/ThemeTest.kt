package io.github.miladaslaner.lumitv

import androidx.compose.ui.graphics.Color
import io.github.miladaslaner.lumitv.ui.Theme
import io.github.miladaslaner.lumitv.ui.UiStrings
import org.json.JSONObject
import java.io.File
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class ThemeTest {
    @Test fun defaultsMatchShippedTheme() {
        // every token: the defaults are what the TV draws before (or without) `hello`
        val shipped = JSONObject(File("../../server/content/theme.json").readText())
        assertEquals(Theme.Default, Theme.from(shipped))
        listOf("bg", "panel", "border", "border_strong", "text", "text_secondary", "text_muted", "green", "indigo",
            "heading_weight", "heading_tracking", "radius_card").forEach { assertTrue(it, shipped.has(it)) }
        assertEquals(Color(0xFF08090A), Theme.Default.bg)
    }

    @Test fun helloOverridesAndBadValuesFallBack() {
        val t = Theme.from(JSONObject("""{"bg":"#112233","green":"nope","border":"rgba(255,255,255,.08)","radius_card":20}"""))
        assertEquals(Color(0xFF112233), t.bg)
        assertEquals(Theme.Default.green, t.green)
        assertEquals(Color(1f, 1f, 1f, 0.08f), t.border)
        assertEquals(20, t.radiusCard)
    }

    @Test fun stringsFillServiceAndFallBack() {
        val s = UiStrings.from(JSONObject("""{"open":"Watch on {service}"}"""))
        assertEquals("Watch on Netflix", s.open("Netflix"))
        assertEquals("Netflix may not work while the VPN is on", s.needsVpnOff("Netflix"))
        assertEquals("Videoland needs the VPN on", s.needsVpnOn("Videoland"))
    }

    @Test fun vpnNameComesFromTheStringsFile() {
        val s = UiStrings.from(JSONObject("""{"needs_vpn_on":"{service} needs SomeVPN on"}"""))
        assertEquals("Videoland needs SomeVPN on", s.needsVpnOn("Videoland"))
    }

    @Test fun offlineLineMakesNoPromise() {
        // nothing restarts by itself on a Mac/Linux host, and the family cannot act on "server" anyway
        assertFalse(UiStrings.Default.offline.contains("restarts"))
        assertFalse(UiStrings.Default.offline.contains("server"))
    }

    @Test fun otherServicesHint() = assertEquals("▼: other services", UiStrings.Default.otherServices)

    @Test fun moreTileLabelAndQuestion() {
        assertEquals("More picks", UiStrings.Default.more)
        assertEquals("Show me more", UiStrings.Default.moreQuestion)
    }

    @Test fun searchHintText() {
        assertEquals("Search for “Heat”", UiStrings.Default.searchHint("Heat"))
        assertEquals("Zoek “Heat”", UiStrings.from(JSONObject("""{"search_hint":"Zoek “{title}”"}""")).searchHint("Heat"))
    }

    private val shippedFile = File("../../server/content/strings.en.json")

    /** The shipped file with its placeholders filled the way the server fills them before sending `hello`. */
    private fun shippedStrings() = JSONObject(shippedFile.readText().replace("{assistant}", "Lumi").replace("{vpn}", "the VPN"))

    @Test fun defaultsMatchTheShippedStringsFile() {
        // every key, present and future: the defaults are what the TV shows before (or without) `hello`
        assertEquals(UiStrings.Default, UiStrings.from(shippedStrings()))
    }

    @Test fun shippedStringsNameNoBrandAndNoMachinery() {
        val raw = shippedFile.readText()
        listOf("Lumi", "NordVPN", "server", "Claude", "signing in").forEach { assertFalse(it, raw.contains(it, ignoreCase = true)) }
        assertTrue(raw.contains("{assistant}") && raw.contains("{vpn}"))
    }
}
