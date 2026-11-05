package io.github.miladaslaner.lumitv

import io.github.miladaslaner.lumitv.ui.KEY_ROWS
import io.github.miladaslaner.lumitv.ui.KeyboardModel
import io.github.miladaslaner.lumitv.ui.LAST_ROW
import io.github.miladaslaner.lumitv.ui.SpecialKey
import io.github.miladaslaner.lumitv.ui.UiStrings
import io.github.miladaslaner.lumitv.ui.label
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test

class ViewersTest {
    @get:Rule val lumi = LumiStateRule()

    private val milad = Viewer("Milad")
    private val emma = Viewer("Emma", 6)
    private val noah = Viewer("Noah", 9)

    @Test fun aTrailingNumberIsAChildsAge() {
        assertEquals(Viewer("Emma", 6), parseViewer("  Emma 6 "))
        assertEquals(Viewer("Milad"), parseViewer("Milad"))
        assertEquals(Viewer("Anna Lena", 4), parseViewer("Anna Lena 4"))
        assertNull(parseViewer("   "))
        assertNull(parseViewer("7"))
        assertNull(parseViewer("Emma 300"))
        assertTrue(Viewer("Emma", 6).isChild)
        assertFalse(Viewer("Sara", 40).isChild)
    }

    @Test fun typedNamesAreTidiedAndTheAgeMayBeInBracketsOrAfterAComma() {
        // the keyboard has no shift and the family types the way they would on paper
        assertEquals(Viewer("Emma", 6), parseViewer("Emma (6)"))
        assertEquals(Viewer("Emma", 6), parseViewer("emma, 6"))
        assertEquals(Viewer("Mary Ann", 4), parseViewer("mary ann 4"))
        assertEquals(Viewer("Milad"), parseViewer("milad."))
        assertEquals(Viewer("McKay"), parseViewer("McKay")) // capitals inside a word stay
        assertEquals(Viewer("O'brien"), parseViewer("o'brien"))
        assertNull(parseViewer("(6)"))
        // a comma inside a name is refused: names travel comma-joined in /ask/audio?viewers= (protocol.md)
        assertNull(parseViewer("anna,lena 4"))
        assertNull(parseViewer("smith,jr"))
    }

    @Test fun peopleShowTheirAgeOrGrownUpAndRemovalUsesAGlyphTheFontHas() {
        val strings = UiStrings.Default
        val m = ViewersModel(listOf(milad, emma), watching = setOf("Emma"))
        assertEquals("Milad · grown-up", m.label(ViewerItem.Person(milad), strings))
        assertEquals("✓ Emma · 6", m.label(ViewerItem.Person(emma), strings))
        assertEquals("× Emma · 6", m.copy(removing = true).label(ViewerItem.Person(emma), strings))
    }

    @Test fun eveningsDefaultToTheGrownUpsDaytimeToEveryone() {
        val people = listOf(milad, emma, noah)
        assertEquals(people, defaultWatching(people, daytime = true))
        assertEquals(listOf(milad), defaultWatching(people, daytime = false))
        assertEquals(listOf(emma), defaultWatching(listOf(emma), daytime = false)) // no grown-up set up: everyone
    }

    @Test fun kidsHoursIncludeTheStartAndNotTheEnd() {
        assertTrue(inKidsHours(6 * 60, "06:00", "19:00"))
        assertFalse(inKidsHours(19 * 60, "06:00", "19:00"))
        assertTrue(inKidsHours(23 * 60, "22:00", "02:00"))
        assertFalse(inKidsHours(12 * 60, null, "19:00"))
    }

    @Test fun aChoiceHoldsFourHoursThenTheDefaultReturns() {
        Viewers.kidsFrom = "06:00"; Viewers.kidsUntil = "19:00"
        Viewers.setPeople(listOf(milad, emma))
        Viewers.choose(setOf("Emma"), nowMs = 0)
        assertEquals(listOf(emma), Viewers.watching(nowMs = 1000, minuteOfDay = 21 * 60))
        assertEquals(listOf(milad), Viewers.watching(nowMs = SELECTION_HOLD_MS, minuteOfDay = 21 * 60))
        assertEquals(listOf(milad, emma), Viewers.watching(nowMs = SELECTION_HOLD_MS, minuteOfDay = 10 * 60))
    }

    @Test fun labelSaysEveryoneOrTheNames() {
        assertEquals("Everyone", watchingLabel(listOf(milad, emma), listOf(milad, emma), "Everyone"))
        assertEquals("Milad", watchingLabel(listOf(milad, emma), listOf(milad), "Everyone"))
    }

    @Test fun okTogglesPeopleAndEveryoneTicksAll() {
        var m = ViewersModel(listOf(milad, emma), watching = setOf("Milad"))
        assertEquals(ViewerItem.Everyone, m.items[0])
        m = m.copy(focus = 2).ok().model // Emma
        assertEquals(setOf("Milad", "Emma"), m.watching)
        m = m.ok().model
        assertEquals(setOf("Milad"), m.watching)
        assertEquals(setOf("Milad", "Emma"), m.copy(focus = 0).ok().model.watching)
        assertTrue(m.copy(focus = m.items.lastIndex).ok().close)
    }

    @Test fun addingAPersonTypesThemAndSaves() {
        var m = ViewersModel(emptyList(), watching = emptySet())
        // An empty household has nobody for Everyone to tick: the first OK is Add (it has the focus).
        assertEquals(listOf(ViewerItem.Add, ViewerItem.Done), m.items)
        assertEquals(ViewerItem.Add, m.items[m.focus])
        m = m.ok().model
        var kb = m.typing!!
        // type "noah 9" on the keyboard model, then DONE
        for (ch in "noah 9") kb = typeOn(kb, ch)
        m = m.copy(typing = kb.copy(row = KEY_ROWS.size, col = LAST_ROW.indexOf(SpecialKey.DONE)))
        val step = m.ok()
        assertEquals(listOf(Viewer("Noah", 9)), step.save)
        assertNull(step.model.typing)
        assertEquals(setOf("Noah"), step.model.watching)
        // the new person is under the focus; Everyone appears now that there is someone
        assertEquals(ViewerItem.Person(Viewer("Noah", 9)), step.model.items[step.model.focus])
        assertEquals(ViewerItem.Everyone, step.model.items[0])
    }

    @Test fun removeModeRemovesOnOkAndSaves() {
        var m = ViewersModel(listOf(milad, emma), watching = setOf("Milad", "Emma"))
        m = m.copy(focus = m.items.indexOf(ViewerItem.Remove)).ok().model
        assertTrue(m.removing)
        assertEquals(ViewerItem.Person(milad), m.items[0]) // no Everyone / Add while removing
        val step = m.copy(focus = 1).ok()
        assertEquals(listOf(milad), step.save)
        assertEquals(setOf("Milad"), step.model.watching)
    }

    @Test fun theKeyThatAddsAPersonSaysAddNotAsk() {
        assertEquals("Add", UiStrings.Default.addKey)
        assertEquals("Toevoegen", UiStrings.from(JSONObject("""{"add_key":"Toevoegen"}""")).addKey)
    }

    private fun typeOn(kb: KeyboardModel, ch: Char): KeyboardModel =
        if (ch == ' ') kb.copy(text = kb.text + " ") else kb.copy(text = kb.text + ch)
}
