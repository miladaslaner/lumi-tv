package io.github.miladaslaner.lumitv

import android.content.Context
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.RuntimeEnvironment

/** A household edit made on the TV reaches the server even when the link is down at the time (LIFE-1). */
@RunWith(RobolectricTestRunner::class)
class ViewersSyncTest {
    @get:Rule val lumi = LumiStateRule()

    private val prefs = RuntimeEnvironment.getApplication().getSharedPreferences(PREFS, Context.MODE_PRIVATE)
    private val milad = Viewer("Milad")
    private val emma = Viewer("Emma", 6)
    private val sent = mutableListOf<String>()
    private val down: (String) -> Boolean = { false }
    private val up: (String) -> Boolean = { sent += it; true }

    private fun names(json: String) = JSONObject(json).getJSONArray("viewers").let { a -> (0 until a.length()).map { a.getJSONObject(it).getString("name") } }

    @Test fun anEditWhileTheLinkIsDownIsShownKeptAndSentOnTheNextConnect() {
        val sync = ViewersSync(prefs)
        sync.save(listOf(milad, emma), down)
        assertEquals(listOf(milad, emma), Viewers.people.value) // shown at once
        assertNotNull(sync.pending)
        // the next connection: the server's hello still has the old household, which must not win
        sync.onOpen(up)
        assertEquals(listOf(listOf("Milad", "Emma")), sent.map(::names))
        sync.onHello(listOf(milad))
        assertEquals(listOf(milad, emma), Viewers.people.value)
        // the server's answer confirms it: nothing is pending any more, the server's copy is shown
        sync.onEcho(listOf(milad, emma))
        assertNull(sync.pending)
        assertEquals(listOf(milad, emma), Viewers.people.value)
    }

    @Test fun anEditWhileConnectedIsDoneOnceTheServerAnswers() {
        val sync = ViewersSync(prefs)
        sync.save(listOf(milad), up)
        assertEquals(1, sent.size)
        assertNotNull(sync.pending)
        sync.onEcho(listOf(milad))
        assertNull(sync.pending)
        // with nothing pending, a hello is the server's truth
        sync.onHello(listOf(milad, emma))
        assertEquals(listOf(milad, emma), Viewers.people.value)
    }

    @Test fun theAnswerToAnOlderEditDoesNotRetireANewerOne() {
        val sync = ViewersSync(prefs)
        sync.save(listOf(milad), up)
        sync.save(listOf(milad, emma), up)
        sync.onEcho(listOf(milad)) // the first send's answer
        assertNotNull(sync.pending)
        sync.onEcho(listOf(milad, emma))
        assertNull(sync.pending)
    }

    @Test fun aPendingEditSurvivesTheProcessAndADroppedConnection() {
        ViewersSync(prefs).save(listOf(emma), up) // sent, but the answer never came (the link dropped)
        val afterRestart = ViewersSync(prefs)
        assertEquals(listOf("Emma"), names(afterRestart.pending!!))
        afterRestart.onOpen(up)
        assertEquals(2, sent.size) // sent again on the new connection
        afterRestart.onEcho(listOf(emma))
        assertNull(afterRestart.pending)
    }
}
