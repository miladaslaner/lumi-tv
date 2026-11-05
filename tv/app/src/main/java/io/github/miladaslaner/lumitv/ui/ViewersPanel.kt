package io.github.miladaslaner.lumitv.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.ExperimentalLayoutApi
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.text.font.FontWeight
import io.github.miladaslaner.lumitv.ViewerItem
import io.github.miladaslaner.lumitv.ViewersModel

/**
 * The pill text for one item. A person always shows what Lumi took them for ("Emma · 6", "Milad · grown-up"), so a
 * child added without an age is visibly a grown-up; ticked people get a check, people about to be removed a cross.
 */
fun ViewersModel.label(item: ViewerItem, strings: UiStrings): String = when (item) {
    ViewerItem.Everyone -> strings.everyone
    is ViewerItem.Person -> {
        val who = "${item.viewer.name} · ${item.viewer.age ?: strings.grownUp}"
        when {
            removing -> "× $who"
            item.viewer.name in watching -> "✓ $who"
            else -> who
        }
    }
    ViewerItem.Add -> strings.addViewer
    ViewerItem.Remove -> if (removing) strings.doneRemoving else strings.removeViewers
    ViewerItem.Done -> strings.done
}

/** "Who's watching": the title, one line of help, the items as pills (wrapping), or the keyboard while typing. */
@OptIn(ExperimentalLayoutApi::class)
@Composable
internal fun ViewersPanel(sc: Scale, theme: Theme, strings: UiStrings, model: ViewersModel) {
    model.typing?.let {
        KeyboardPanel(sc, theme, strings, it, hint = strings.viewerHint, doneLabel = strings.addKey)
        return
    }
    Column(Modifier.padding(start = sc.dp(86f), top = sc.dp(190f)).width(sc.dp(1700f))) {
        Text(sc, theme, strings.viewersTitle, 72f, theme.text, weight = FontWeight.SemiBold, maxLines = 1)
        val line = when {
            model.removing -> strings.viewersRemoveLine
            model.people.isEmpty() -> strings.viewersSetupLine
            else -> strings.viewersLine
        }
        Text(sc, theme, line, 32f, theme.textMuted, maxLines = 2, modifier = Modifier.padding(top = sc.dp(24f)))
        FlowRow(
            Modifier.padding(top = sc.dp(56f)),
            horizontalArrangement = Arrangement.spacedBy(sc.dp(20f)),
            verticalArrangement = Arrangement.spacedBy(sc.dp(20f)),
        ) {
            model.items.forEachIndexed { i, item ->
                val focused = i == model.focus
                val ticked = item is ViewerItem.Person && item.viewer.name in model.watching && !model.removing
                Row(
                    Modifier.clip(CircleShape)
                        .background(if (focused) theme.text else if (ticked) theme.panel[2] else theme.panel[1])
                        .border(sc.dp(if (focused) 3f else 1.5f), if (focused) theme.text else theme.borderStrong, CircleShape)
                        .padding(horizontal = sc.dp(36f), vertical = sc.dp(16f)),
                ) {
                    Text(sc, theme, model.label(item, strings), 32f, if (focused) theme.bg else theme.textSecondary, weight = FontWeight.Medium, maxLines = 1)
                }
            }
        }
    }
}
