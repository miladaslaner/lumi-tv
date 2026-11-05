package io.github.miladaslaner.lumitv

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test

class RenderClockTest {
    @get:Rule val lumi = LumiStateRule()

    private fun show(id: String) = parseServerMsg(
        """{"type":"show","answer_id":"$id","question":"q","headline":"h","subline":"s","picks":[]}""",
    ) as ServerMsg.Show

    @Test fun restartMovesTheStartToNow() {
        val before = MessageBus.nowMs()
        MessageBus.restartRenderClock()
        assertTrue(MessageBus.showReceivedMs >= before)
        assertTrue(MessageBus.nowMs() - MessageBus.showReceivedMs < 1_000)
    }

    @Test fun eachPhaseIsMarkedOncePerAnswerAndANewShowClearsThem() {
        MessageBus.publish(show("a1"))
        MessageBus.markRender(RenderPhase.START)
        MessageBus.markRender(RenderPhase.CREATED)
        Thread.sleep(20)
        MessageBus.markRender(RenderPhase.CREATED) // a second activity for the same answer keeps the first time
        val first = MessageBus.renderPhases()
        assertEquals(setOf(RenderPhase.START, RenderPhase.CREATED), first.keys)
        assertTrue(first.getValue(RenderPhase.CREATED) < 20)
        MessageBus.publish(show("a2"))
        assertTrue(MessageBus.renderPhases().isEmpty())
    }

    @Test fun restartingTheClockClearsThePhases() {
        MessageBus.publish(show("a1"))
        MessageBus.markRender(RenderPhase.FRAME)
        MessageBus.restartRenderClock()
        assertFalse(RenderPhase.FRAME in MessageBus.renderPhases())
    }

    @Test fun renderedCarriesThePhasesWhenThereAreAny() {
        val r = JSONObject(renderedMsg("a1", 900, mapOf(RenderPhase.START to 5L, RenderPhase.CREATED to 400L, RenderPhase.FRAME to 700L)))
        val p = r.getJSONObject("phases")
        assertEquals(5, p.getInt("start")); assertEquals(400, p.getInt("created")); assertEquals(700, p.getInt("frame"))
        assertFalse(JSONObject(renderedMsg("a1", 900)).has("phases"))
    }

    @Test fun renderLineNamesThePhases() {
        assertEquals("rendered a1 in 900ms (start 5, created 400, frame 700)",
            renderLine("a1", 900, linkedMapOf(RenderPhase.START to 5L, RenderPhase.CREATED to 400L, RenderPhase.FRAME to 700L)))
        assertEquals("rendered a1 in 120ms", renderLine("a1", 120, emptyMap()))
    }
}
