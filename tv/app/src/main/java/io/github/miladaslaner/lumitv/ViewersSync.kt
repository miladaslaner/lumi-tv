package io.github.miladaslaner.lumitv

import android.content.SharedPreferences

/** The prefs key holding a `viewers_set` the server has not yet answered with `viewers`. */
const val PREF_PENDING_VIEWERS = "pending_viewers"

/**
 * Keeps a household edit until the server confirms it. The edit shows at once ([Viewers.setPeople]); its
 * `viewers_set` is stored in the prefs and sent now if the link is up, else on the next connect (also after a process
 * death), and a `hello` that arrives while it is pending does not overwrite it with the server's older copy. The
 * server answers every `viewers_set` with `viewers`; once every send on the current connection has been answered the
 * pending edit is done, and the server's copy (its answer) is what the TV shows.
 */
class ViewersSync(private val prefs: SharedPreferences) {
    private var unanswered = 0

    /** The `viewers_set` still waiting for the server's answer, or null. */
    val pending: String? get() = prefs.getString(PREF_PENDING_VIEWERS, null)

    /** The household as edited on the TV: shown now, kept until confirmed, sent through [send] when that succeeds. */
    @Synchronized
    fun save(people: List<Viewer>, send: (String) -> Boolean) {
        val msg = viewersSetMsg(people)
        prefs.edit().putString(PREF_PENDING_VIEWERS, msg).apply()
        Viewers.setPeople(people)
        if (send(msg)) unanswered++
    }

    /** A new connection: nothing sent on the old one will be answered any more; a pending edit goes out again. */
    @Synchronized
    fun onOpen(send: (String) -> Boolean) {
        unanswered = 0
        pending?.let { if (send(it)) unanswered = 1 }
    }

    /** The server's `hello`: its copy of the household, unless an edit from this TV is still unconfirmed. */
    @Synchronized
    fun onHello(viewers: List<Viewer>) {
        if (pending == null) Viewers.setPeople(viewers)
    }

    /** The server's `viewers`, its answer to a `viewers_set`: the pending edit is done once every send is answered. */
    @Synchronized
    fun onEcho(viewers: List<Viewer>) {
        if (unanswered > 0) unanswered--
        if (unanswered == 0) prefs.edit().remove(PREF_PENDING_VIEWERS).apply()
        Viewers.setPeople(viewers)
    }
}
