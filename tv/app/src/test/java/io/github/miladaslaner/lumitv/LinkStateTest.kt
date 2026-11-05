package io.github.miladaslaner.lumitv

import io.github.miladaslaner.lumitv.ui.UiStrings
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test

/** The link's states: what a close code means, which states retry, and the idle line each one gets. */
class LinkStateTest {
    @get:Rule val lumi = LumiStateRule()

    @Test fun aRefusedTokenIsRejectedAndNeverRetried() {
        // the server's own 4401, and an HTTP 401/403 from a proxy in front of it
        listOf(LinkService.CLOSE_BAD_TOKEN, 401, 403).forEach { assertEquals("$it", LinkState.REJECTED, linkStateAfterClose(it)) }
        assertFalse(LinkState.REJECTED.reconnects)
        assertFalse(LinkState.NOT_CONFIGURED.reconnects)
    }

    @Test fun anyOtherCloseIsADropThatRetries() {
        listOf(1000, 1006, LinkService.CLOSE_REPLACED, -1, 500, 502).forEach { assertEquals("$it", LinkState.DOWN, linkStateAfterClose(it)) }
        assertTrue(LinkState.DOWN.reconnects)
    }

    @Test fun theIdleLineNamesWhatIsWrong() {
        val s = UiStrings.Default
        assertNull(s.linkLine(LinkState.CONNECTED))
        assertEquals(s.offline, s.linkLine(LinkState.DOWN))
        assertEquals(s.notSetUp, s.linkLine(LinkState.NOT_CONFIGURED))
        assertEquals(s.linkRejected, s.linkLine(LinkState.REJECTED))
        // three different lines: none sends the family waiting for a repair that cannot happen
        assertEquals(3, setOf(s.offline, s.notSetUp, s.linkRejected).size)
    }

    @Test fun theLinkStartsDownUntilItConnects() {
        assertEquals(LinkState.DOWN, MessageBus.linkState.value)
        MessageBus.setLinkState(LinkState.CONNECTED)
        assertEquals(LinkState.CONNECTED, MessageBus.linkState.value)
    }
}
