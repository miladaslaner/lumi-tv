package io.github.miladaslaner.lumitv

import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Rule
import org.junit.Test

class AnswerAgeTest {
    private fun show() = parseServerMsg(
        """{"type":"show","answer_id":"a1","question":"q","headline":"h","subline":"s","picks":[]}""",
    ) as ServerMsg.Show

    @get:Rule val lumi = LumiStateRule()

    @Test fun freshAnswerIsKept() {
        MessageBus.publish(show())
        MessageBus.consumeIfStale(now = MessageBus.showReceivedMs + 1_000)
        assertNotNull(MessageBus.lastShow.value)
    }

    @Test fun oldAnswerIsDropped() {
        MessageBus.publish(show())
        MessageBus.consumeIfStale(now = MessageBus.showReceivedMs + MessageBus.ANSWER_MAX_AGE_MS + 1)
        assertNull(MessageBus.lastShow.value)
    }
}
