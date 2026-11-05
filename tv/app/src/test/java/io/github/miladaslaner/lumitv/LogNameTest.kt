package io.github.miladaslaner.lumitv

import org.junit.Assert.assertEquals
import org.junit.Test

/** Release builds shorten class names (R8), so logs and the TV check script read these fixed names instead. */
class LogNameTest {
    @Test fun askStatesHaveFixedNames() {
        val id = "ask1"
        assertEquals(
            listOf("Idle", "Listening", "Sending", "Looking", "Typing", "Failed(timeout)", "Reply", "Answered"),
            listOf(
                AskState.Idle, AskState.Listening(id, null), AskState.Sending(id), AskState.Looking(id, "q"),
                AskState.Typing(id, ""), AskState.Failed(id, "timeout"), AskState.Reply(id, "x"), AskState.Answered(id),
            ).map { it.logName },
        )
    }

    @Test fun postResultsHaveFixedNames() {
        assertEquals(listOf("Accepted", "Rejected(429)", "Unreachable"),
            listOf(PostResult.Accepted, PostResult.Rejected(429), PostResult.Unreachable).map { it.logName })
    }
}
