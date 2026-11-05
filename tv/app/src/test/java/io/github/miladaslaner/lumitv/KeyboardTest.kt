package io.github.miladaslaner.lumitv

import io.github.miladaslaner.lumitv.ui.KEY_ROWS
import io.github.miladaslaner.lumitv.ui.KeyboardModel
import io.github.miladaslaner.lumitv.ui.delete
import io.github.miladaslaner.lumitv.ui.move
import io.github.miladaslaner.lumitv.ui.press
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class KeyboardTest {
    /** Moves from where [m] is to [ch] with D-pad steps only, then presses OK. */
    private fun typeChar(m: KeyboardModel, ch: Char): KeyboardModel {
        val row = KEY_ROWS.indexOfFirst { ch in it }
        val col = KEY_ROWS[row].indexOf(ch)
        var k = m
        repeat(Math.abs(row - k.row)) { k = k.move(0, if (row > k.row) 1 else -1) }
        repeat(Math.abs(col - k.col)) { k = k.move(if (col > k.col) 1 else -1, 0) }
        val (next, submitted) = k.press()
        assertNull(submitted)
        return next
    }

    private fun typeAll(text: String) = text.fold(KeyboardModel()) { m, c -> typeChar(m, c) }

    /** Moves down to the last row and onto [col] (0 SPACE, 1 DELETE, 2 DONE). */
    private fun lastRow(m: KeyboardModel, col: Int): KeyboardModel {
        var k = m.move(0, 99)
        k = k.move(-99, 0)
        repeat(col) { k = k.move(1, 0) }
        return k
    }

    @Test fun typesHello() {
        val m = typeAll("hello")
        assertEquals("hello", m.text)
        val (_, submitted) = lastRow(m, 2).press()
        assertEquals("hello", submitted)
    }

    @Test fun spaceAndPunctuation() {
        var m = typeAll("hi")
        m = lastRow(m, 0).press().first
        m = typeChar(m, 'x')
        assertEquals("hi x", m.text)
        assertEquals("ok?", typeAll("ok?").text)
    }

    @Test fun deleteRemovesLast() {
        val m = typeAll("abc")
        assertEquals("ab", lastRow(m, 1).press().first.text)
        assertEquals("ab", m.delete().text)
        assertEquals("", KeyboardModel().delete().text)
    }

    @Test fun doneOnBlankSubmitsNothing() {
        assertNull(lastRow(KeyboardModel(), 2).press().second)
        val spaces = lastRow(KeyboardModel(), 0).press().first.press().first
        assertEquals("  ", spaces.text)
        assertNull(spaces.move(1, 0).move(1, 0).press().second)
    }

    @Test fun doneTrimsTheText() {
        val m = lastRow(KeyboardModel(text = " funny "), 2)
        assertEquals("funny", m.press().second)
    }

    @Test fun capsAtTheServersLimit() {
        var m = KeyboardModel()
        repeat(250) { m = m.press().first }
        assertEquals(AskLimits.DEFAULT.questionChars, m.text.length)
        assertEquals(200, AskLimits.DEFAULT.questionChars)
        // a server with a lower cap stops the keyboard there, so no typed question is refused as too long
        var short = KeyboardModel()
        repeat(20) { short = short.press(maxChars = 5).first }
        assertEquals(5, short.text.length)
    }

    @Test fun counterAppearsOnlyNearTheCap() {
        assertNull(io.github.miladaslaner.lumitv.ui.charCounter(0, 200))
        assertNull(io.github.miladaslaner.lumitv.ui.charCounter(160, 200))
        assertEquals("161/200", io.github.miladaslaner.lumitv.ui.charCounter(161, 200))
        assertEquals("200/200", io.github.miladaslaner.lumitv.ui.charCounter(200, 200))
        assertEquals("81/100", io.github.miladaslaner.lumitv.ui.charCounter(81, 100)) // follows the configured cap
    }

    @Test fun moveClampsAtEdges() {
        assertEquals(KeyboardModel(row = 0, col = 0), KeyboardModel().move(-1, -1))
        assertEquals(KeyboardModel(row = 0, col = 9), KeyboardModel(col = 9).move(1, 0))
        val bottom = KeyboardModel().move(0, 99)
        assertEquals(4, bottom.row)
        assertEquals(2, bottom.move(99, 0).col)
        assertEquals(4, bottom.move(0, 1).row)
    }

    @Test fun lastRowMapsColumnsProportionally() {
        // q..r -> SPACE, t..u -> DELETE, i..p -> DONE, and back up to the middle of each span
        assertEquals(0, KeyboardModel(row = 3, col = 3).move(0, 1).col)
        assertEquals(1, KeyboardModel(row = 3, col = 4).move(0, 1).col)
        assertEquals(1, KeyboardModel(row = 3, col = 6).move(0, 1).col)
        assertEquals(2, KeyboardModel(row = 3, col = 9).move(0, 1).col)
        assertEquals(1, KeyboardModel(row = 4, col = 0).move(0, -1).col)
        assertEquals(5, KeyboardModel(row = 4, col = 1).move(0, -1).col)
        assertEquals(8, KeyboardModel(row = 4, col = 2).move(0, -1).col)
    }
}
