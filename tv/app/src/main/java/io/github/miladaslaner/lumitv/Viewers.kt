package io.github.miladaslaner.lumitv

import io.github.miladaslaner.lumitv.ui.KeyboardModel
import io.github.miladaslaner.lumitv.ui.press
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow

/** Someone who watches this TV; [age] is set for children (the youngest one watching sets the kids limit). */
data class Viewer(val name: String, val age: Int? = null) {
    val isChild: Boolean get() = age != null && age < ADULT_AGE
}

const val ADULT_AGE = 18
const val MAX_VIEWERS = 12
const val MAX_VIEWER_NAME = 30

/** A choice of who's watching holds this long; then the default for the time of day comes back. */
const val SELECTION_HOLD_MS = 4 * 60 * 60 * 1000L

/**
 * One typed entry: a name, and for a child their age at the end ("Emma 6"; "Emma (6)" and "emma, 6" too). Null when
 * there is no name, the age is not a plausible one (0..120), or a comma sits inside the name (names travel
 * comma-joined in `/ask/audio?viewers=`, see protocol.md). The keyboard has no shift, so every word is title-cased
 * (capitals typed inside a word, "McKay", stay); punctuation around a word is dropped; the name is cut at
 * [MAX_VIEWER_NAME].
 */
fun parseViewer(text: String): Viewer? {
    val words = text.split(Regex("\\s+")).map { it.trim(',', '.', '(', ')') }.filter { it.isNotEmpty() }
    if (words.isEmpty()) return null
    val age = words.last().toIntOrNull()
    val nameWords = if (age != null) words.dropLast(1) else words
    if (nameWords.isEmpty() || (age != null && age !in 0..120) || nameWords.any { ',' in it }) return null
    val name = nameWords.joinToString(" ") { it.replaceFirstChar(Char::titlecase) }.take(MAX_VIEWER_NAME).trim()
    return Viewer(name, age)
}

/**
 * Who is watching when nobody chose lately: during the household's daytime hours everyone (children may be
 * around), in the evening the grown-ups only (or everyone, when the household has no grown-up set up).
 */
fun defaultWatching(people: List<Viewer>, daytime: Boolean): List<Viewer> =
    if (daytime) people else people.filterNot { it.isChild }.ifEmpty { people }

/** "HH:MM" -> minutes since midnight; null for anything else. */
fun minutesOf(hhmm: String?): Int? {
    val m = Regex("^([01]\\d|2[0-3]):([0-5]\\d)$").find(hhmm ?: return null) ?: return null
    return m.groupValues[1].toInt() * 60 + m.groupValues[2].toInt()
}

/** Whether [minute] (since midnight) is inside the household's kids hours [from, until); none set: never. */
fun inKidsHours(minute: Int, from: String?, until: String?): Boolean {
    val start = minutesOf(from) ?: return false
    val end = minutesOf(until) ?: return false
    return if (start <= end) minute in start until end else minute >= start || minute < end
}

/** The pill's text: the names watching, or [everyone] when that is all of them. */
fun watchingLabel(people: List<Viewer>, watching: List<Viewer>, everyone: String): String =
    if (watching.size == people.size) everyone else watching.joinToString(", ") { it.name }

/** One focusable item on the "Who's watching" screen, left to right. */
sealed interface ViewerItem {
    data object Everyone : ViewerItem
    data class Person(val viewer: Viewer) : ViewerItem
    data object Add : ViewerItem
    data object Remove : ViewerItem
    data object Done : ViewerItem
}

/**
 * The "Who's watching" screen: [people] is the household, [watching] who is ticked, [removing] turns OK on a person
 * into removing them, [typing] is the keyboard while a new person is typed.
 */
data class ViewersModel(
    val people: List<Viewer>,
    val watching: Set<String>,
    val focus: Int = 0,
    val removing: Boolean = false,
    val typing: KeyboardModel? = null,
) {
    val items: List<ViewerItem>
        get() = buildList {
            // On an empty household Everyone would tick nobody: the first thing to do (and to focus) is Add.
            if (!removing && people.isNotEmpty()) add(ViewerItem.Everyone)
            people.forEach { add(ViewerItem.Person(it)) }
            if (!removing && people.size < MAX_VIEWERS) add(ViewerItem.Add)
            if (people.isNotEmpty()) add(ViewerItem.Remove)
            add(ViewerItem.Done)
        }

    fun move(dx: Int): ViewersModel = copy(focus = (focus + dx).coerceIn(0, items.lastIndex))
}

/** What OK on the "Who's watching" screen asks the app to do besides showing [model]. */
data class ViewersStep(val model: ViewersModel, val save: List<Viewer>? = null, val close: Boolean = false)

/** OK on the focused item (or on the keyboard while typing a new person). */
fun ViewersModel.ok(): ViewersStep {
    typing?.let { kb ->
        val (next, done) = kb.press()
        val added = done?.let(::parseViewer)
        return when {
            done == null -> ViewersStep(copy(typing = next))
            added == null || people.any { it.name.equals(added.name, ignoreCase = true) } -> ViewersStep(copy(typing = next))
            else -> {
                val people2 = people + added
                val next = copy(people = people2, watching = watching + added.name, typing = null)
                // The new person is ticked and under the focus, so what was just typed is what is looked at.
                ViewersStep(next.copy(focus = next.items.indexOf(ViewerItem.Person(added))), save = people2)
            }
        }
    }
    return when (val item = items.getOrNull(focus)) {
        ViewerItem.Everyone -> ViewersStep(copy(watching = people.map { it.name }.toSet()))
        is ViewerItem.Person ->
            if (removing) {
                val people2 = people - item.viewer
                val m = copy(people = people2, watching = watching - item.viewer.name, removing = people2.isNotEmpty())
                ViewersStep(m.copy(focus = focus.coerceAtMost(m.items.lastIndex)), save = people2)
            } else {
                val n = item.viewer.name
                ViewersStep(copy(watching = if (n in watching) watching - n else watching + n))
            }
        ViewerItem.Add -> ViewersStep(copy(typing = KeyboardModel()))
        ViewerItem.Remove -> ViewersStep(copy(removing = !removing, focus = 0))
        ViewerItem.Done, null -> ViewersStep(this, close = true)
    }
}

/**
 * The household's viewers (from the server's `hello` / `viewers`) and who is watching now. A choice holds for
 * [SELECTION_HOLD_MS]; before any (or after that) the default for the time of day applies.
 */
object Viewers {
    private val _people = MutableStateFlow<List<Viewer>>(emptyList())
    val people: StateFlow<List<Viewer>> = _people.asStateFlow()
    private var chosen: Set<String>? = null
    private var chosenAtMs = 0L
    @Volatile var kidsFrom: String? = null
    @Volatile var kidsUntil: String? = null

    fun setPeople(list: List<Viewer>) {
        _people.value = list
        chosen = chosen?.filter { n -> list.any { it.name == n } }?.toSet()
    }

    /** Test hook. */
    fun reset() {
        _people.value = emptyList()
        chosen = null
        chosenAtMs = 0L
        kidsFrom = null
        kidsUntil = null
    }

    fun choose(names: Set<String>, nowMs: Long) {
        chosen = names
        chosenAtMs = nowMs
    }

    /** Who is watching now ([minuteOfDay] for the time-of-day default). */
    fun watching(nowMs: Long, minuteOfDay: Int): List<Viewer> {
        val people = _people.value
        val held = chosen?.takeIf { nowMs - chosenAtMs < SELECTION_HOLD_MS }
        return if (held != null) people.filter { it.name in held }.ifEmpty { people }
        else defaultWatching(people, inKidsHours(minuteOfDay, kidsFrom, kidsUntil))
    }
}
