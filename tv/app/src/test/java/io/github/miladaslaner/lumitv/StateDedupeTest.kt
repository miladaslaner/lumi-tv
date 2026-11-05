package io.github.miladaslaner.lumitv

import org.junit.Assert.assertEquals
import org.junit.Test

class StateDedupeTest {
    private val sent = mutableListOf<String>()
    private val ok: (String) -> Boolean = { sent += it; true }

    @Test fun sameStateIsSentOnce() {
        val d = StateDedupe()
        d.offer("a", false, ok); d.offer("a", false, ok)
        assertEquals(listOf("a"), sent)
    }

    @Test fun changeIsSent() {
        val d = StateDedupe()
        d.offer("a", false, ok); d.offer("b", false, ok); d.offer("a", false, ok)
        assertEquals(listOf("a", "b", "a"), sent)
    }

    @Test fun forceResendsAfterReconnect() {
        val d = StateDedupe()
        d.offer("a", false, ok); d.offer("a", true, ok)
        assertEquals(listOf("a", "a"), sent)
    }

    @Test fun failedSendIsRetried() {
        val d = StateDedupe()
        d.offer("a", false) { false }
        d.offer("a", false, ok)
        assertEquals(listOf("a"), sent)
    }
}
