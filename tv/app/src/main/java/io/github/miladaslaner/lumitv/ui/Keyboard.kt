package io.github.miladaslaner.lumitv.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import io.github.miladaslaner.lumitv.AskLimits

/** The character rows; below them the last row is [LAST_ROW]. */
val KEY_ROWS = listOf("1234567890", "qwertyuiop", "asdfghjkl'", "zxcvbnm,.?")

enum class SpecialKey { SPACE, DELETE, DONE }

val LAST_ROW = listOf(SpecialKey.SPACE, SpecialKey.DELETE, SpecialKey.DONE)

private val LAST = KEY_ROWS.size
private val WIDTH = KEY_ROWS[0].length

/** Lumi's own D-pad keyboard (no system IME): what is typed and which key has focus ([row] == 4 is [LAST_ROW]). */
data class KeyboardModel(val text: String = "", val row: Int = 0, val col: Int = 0)

/**
 * Moves the focus, clamped at the edges. Moving onto the last row maps the column proportionally (the 10 columns
 * onto its 3 keys) and moving off it lands in the middle of that key's span.
 */
fun KeyboardModel.move(dx: Int, dy: Int): KeyboardModel {
    val r = (row + dy).coerceIn(0, LAST)
    val c = when {
        r == LAST && row != LAST -> col * LAST_ROW.size / WIDTH
        r != LAST && row == LAST -> (2 * col + 1) * WIDTH / (2 * LAST_ROW.size)
        else -> col
    }
    val maxCol = if (r == LAST) LAST_ROW.lastIndex else WIDTH - 1
    return copy(row = r, col = (c + dx).coerceIn(0, maxCol))
}

/**
 * OK on the focused key. The second value is the question to send: on DONE, trimmed, when not blank. Typing stops at
 * [maxChars], the server's limit on a question ([AskLimits.questionChars]).
 */
fun KeyboardModel.press(maxChars: Int = AskLimits.DEFAULT.questionChars): Pair<KeyboardModel, String?> {
    if (row < LAST) return type(KEY_ROWS[row][col], maxChars) to null
    return when (LAST_ROW[col]) {
        SpecialKey.SPACE -> type(' ', maxChars) to null
        SpecialKey.DELETE -> delete() to null
        SpecialKey.DONE -> this to text.trim().takeIf { it.isNotEmpty() }
    }
}

fun KeyboardModel.delete(): KeyboardModel = copy(text = text.dropLast(1))

private fun KeyboardModel.type(c: Char, maxChars: Int): KeyboardModel = if (text.length >= maxChars) this else copy(text = text + c)

/** "170/200" once the typed [length] passes 80 % of [maxChars] (typing stops there without this warning); else null. */
fun charCounter(length: Int, maxChars: Int): String? = if (length > maxChars * 4 / 5) "$length/$maxChars" else null

private const val KEY_W = 112f
private const val KEY_H = 86f
private const val GAP = 12f

/**
 * The typed line (or the hint) above the key grid; the focused key is inverted so it reads from the couch. Near
 * [maxChars] a muted "170/200" says why typing is about to stop.
 */
@Composable
internal fun KeyboardPanel(
    sc: Scale,
    theme: Theme,
    strings: UiStrings,
    model: KeyboardModel,
    hint: String = strings.typingHint,
    doneLabel: String = strings.ask,
    maxChars: Int = AskLimits.DEFAULT.questionChars,
) {
    val fieldWidth = sc.dp(WIDTH * KEY_W + (WIDTH - 1) * GAP)
    Column(Modifier.padding(start = sc.dp(86f), top = sc.dp(170f))) {
        Row(
            Modifier.width(fieldWidth).height(sc.dp(96f))
                .clip(RoundedCornerShape(sc.dp(theme.radiusCard.toFloat())))
                .background(theme.panel[1])
                .border(sc.dp(1.5f), theme.borderStrong, RoundedCornerShape(sc.dp(theme.radiusCard.toFloat())))
                .padding(horizontal = sc.dp(28f)),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            if (model.text.isEmpty()) {
                Text(sc, theme, hint, 44f, theme.textMuted, maxLines = 1)
            } else {
                // The end of a long question stays in view.
                Text(sc, theme, model.text.takeLast(48), 44f, theme.text, weight = FontWeight.Medium, maxLines = 1)
            }
            Box(Modifier.padding(start = sc.dp(4f)).width(sc.dp(3f)).height(sc.dp(50f)).background(theme.indigo))
        }
        // The counter's line is always there (empty until the text passes 80 % of the cap), so the keys never move.
        Text(
            sc, theme, charCounter(model.text.length, maxChars).orEmpty(), 24f, theme.textMuted, maxLines = 1, align = TextAlign.End,
            modifier = Modifier.width(fieldWidth).height(sc.dp(36f)),
        )
        Column(verticalArrangement = Arrangement.spacedBy(sc.dp(GAP))) {
            KEY_ROWS.forEachIndexed { r, keys ->
                Row(horizontalArrangement = Arrangement.spacedBy(sc.dp(GAP))) {
                    keys.forEachIndexed { c, ch ->
                        Key(sc, theme, ch.toString(), span = 1, focused = model.row == r && model.col == c)
                    }
                }
            }
            Row(horizontalArrangement = Arrangement.spacedBy(sc.dp(GAP))) {
                LAST_ROW.forEachIndexed { c, key ->
                    val focused = model.row == LAST && model.col == c
                    when (key) {
                        SpecialKey.SPACE -> Key(sc, theme, "␣", span = 4, focused = focused)
                        SpecialKey.DELETE -> Key(sc, theme, "⌫", span = 3, focused = focused)
                        SpecialKey.DONE -> Key(sc, theme, doneLabel, span = 3, focused = focused, accent = true)
                    }
                }
            }
        }
    }
}

@Composable
private fun Key(sc: Scale, theme: Theme, label: String, span: Int, focused: Boolean, accent: Boolean = false) {
    val shape = RoundedCornerShape(sc.dp(10f))
    val bg = when {
        focused -> theme.text
        accent -> theme.indigo.copy(alpha = 0.35f)
        else -> theme.panel[1]
    }
    Box(
        Modifier.width(sc.dp(span * KEY_W + (span - 1) * GAP)).height(sc.dp(KEY_H))
            .clip(shape).background(bg)
            .border(sc.dp(if (focused) 3f else 1f), if (focused) theme.text else theme.border, shape),
        contentAlignment = Alignment.Center,
    ) {
        Text(sc, theme, label, 40f, if (focused) theme.bg else theme.text, weight = FontWeight.Medium, maxLines = 1)
    }
}
