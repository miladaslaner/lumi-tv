package io.github.miladaslaner.lumitv

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Test

class AskProtocolTest {
    private val pick = """{"token":"t","title":"T","service_label":"N","launches":[{"package":"p","action":"MAIN","vpn":"any"}]}"""

    @Test fun parsesHeard() {
        assertEquals(ServerMsg.Heard("a1b2c3d4e5f6", "something funny"),
            parseServerMsg("""{"type":"heard","ask_id":"a1b2c3d4e5f6","text":"something funny"}"""))
    }

    @Test fun parsesAskError() {
        assertEquals(ServerMsg.AskError("a1b2c3d4e5f6", "login"),
            parseServerMsg("""{"type":"ask_error","ask_id":"a1b2c3d4e5f6","code":"login"}"""))
    }

    @Test fun parsesAskReply() {
        assertEquals(ServerMsg.AskReply("a1b2c3d4e5f6", "Which mood?"),
            parseServerMsg("""{"type":"ask_reply","ask_id":"a1b2c3d4e5f6","text":"Which mood?"}"""))
    }

    @Test fun askMessagesWithoutAskIdAreMalformed() {
        assertNull(parseServerMsg("""{"type":"heard","text":"x"}"""))
    }

    @Test fun showAndLookingCarryAskIdOnlyWhenSent() {
        val v1 = parseServerMsg("""{"type":"show","answer_id":"a","question":"q","headline":"h","subline":"s","picks":[$pick]}""") as ServerMsg.Show
        assertNull(v1.askId)
        val ask = parseServerMsg("""{"type":"show","answer_id":"a","question":"q","headline":"h","subline":"s","picks":[$pick],"ask_id":"k1"}""") as ServerMsg.Show
        assertEquals("k1", ask.askId)
        assertNull((parseServerMsg("""{"type":"looking","turn_id":"t","question":"q"}""") as ServerMsg.Looking).askId)
        assertEquals("k1", (parseServerMsg("""{"type":"looking","turn_id":"t","question":"q","ask_id":"k1"}""") as ServerMsg.Looking).askId)
    }

    @Test fun askTimingOmitsNullFields() {
        val o = JSONObject(askTimingMsg("k1", listenMs = 420, heardMs = null, postersMs = null, typedPostersMs = 9100))
        assertEquals("ask_timing", o.getString("type"))
        assertEquals("k1", o.getString("ask_id"))
        assertEquals(420L, o.getLong("listen_ms"))
        assertEquals(9100L, o.getLong("typed_posters_ms"))
        assertFalse(o.has("heard_ms"))
        assertFalse(o.has("posters_ms"))
    }

    @Test fun askCancel() {
        val o = JSONObject(askCancelMsg("k1"))
        assertEquals("ask_cancel", o.getString("type"))
        assertEquals("k1", o.getString("ask_id"))
    }

    @Test fun askRequestBody() {
        val full = JSONObject(askRequestJson("k1", "something funny", "typed", "a9", "t2"))
        assertEquals("k1", full.getString("ask_id"))
        assertEquals("something funny", full.getString("text"))
        assertEquals("typed", full.getString("source"))
        assertEquals("a9", full.getString("answer_id"))
        assertEquals("t2", full.getString("focused_token"))
        val bare = JSONObject(askRequestJson("k1", "x", "voice", null, null))
        assertFalse(bare.has("answer_id"))
        assertFalse(bare.has("focused_token"))
    }
}
