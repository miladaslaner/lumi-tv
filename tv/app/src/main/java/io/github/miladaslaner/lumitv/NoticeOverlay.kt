package io.github.miladaslaner.lumitv

import android.annotation.SuppressLint
import android.content.Context
import android.content.Intent
import android.graphics.Color
import android.graphics.PixelFormat
import android.graphics.Typeface
import android.graphics.drawable.GradientDrawable
import android.os.Handler
import android.os.Looper
import android.text.SpannableStringBuilder
import android.text.Spanned
import android.text.style.ForegroundColorSpan
import android.util.Log
import android.view.Gravity
import android.view.KeyEvent
import android.view.WindowManager
import android.widget.TextView
import androidx.core.content.res.ResourcesCompat
import androidx.compose.ui.graphics.toArgb
import io.github.miladaslaner.lumitv.ui.Theme
import io.github.miladaslaner.lumitv.ui.UiStrings

/** What the notice does with a key press. */
enum class NoticeKey { OPEN, DISMISS, IGNORE }

/**
 * OK opens the answer; BACK or a D-pad key dismisses. The dismissing key is consumed either way: it was
 * already delivered to the pill's window and does not reach the player, so the user's first D-pad press
 * is spent on closing the pill (the price of a focusable overlay). Media and volume keys are never
 * touched and keep reaching the system.
 */
fun noticeKey(keyCode: Int, action: Int, repeatCount: Int): NoticeKey {
    if (action != KeyEvent.ACTION_DOWN) return NoticeKey.IGNORE
    return when (keyCode) {
        KeyEvent.KEYCODE_DPAD_CENTER, KeyEvent.KEYCODE_ENTER, KeyEvent.KEYCODE_NUMPAD_ENTER ->
            if (repeatCount == 0) NoticeKey.OPEN else NoticeKey.IGNORE
        KeyEvent.KEYCODE_BACK,
        KeyEvent.KEYCODE_DPAD_UP, KeyEvent.KEYCODE_DPAD_DOWN,
        KeyEvent.KEYCODE_DPAD_LEFT, KeyEvent.KEYCODE_DPAD_RIGHT -> NoticeKey.DISMISS
        else -> NoticeKey.IGNORE
    }
}

/**
 * The "● Answer ready · press OK" pill, bottom-right, drawn as an application overlay over whatever
 * is playing. It is focusable (an overlay only receives OK if it holds key focus) but it never sends
 * a key or touches the player; removing the window hands key focus straight back to the app below.
 * The same pill without focus carries a search hint ([showHint]). Main thread only.
 */
@SuppressLint("StaticFieldLeak") // the pill is built on the application context and the field is nulled on dismiss
object NoticeOverlay {
    const val TIMEOUT_MS = 15_000L
    const val HINT_TIMEOUT_MS = 12_000L

    private val handler = Handler(Looper.getMainLooper())
    private var view: TextView? = null
    private var windowManager: WindowManager? = null
    private val timeout = Runnable { dismiss() }

    /** True when the pill is up; false when the window could not be added (permission missing). */
    fun show(context: Context): Boolean =
        addPill(context, UiStrings.from(MessageBus.hello.value?.strings).notice, focusable = true, TIMEOUT_MS)

    /**
     * [text] (e.g. `Search for "Heat"`) over the app just launched, for [HINT_TIMEOUT_MS]. It never takes key
     * focus or touches, so the remote keeps working in the app's search screen. False when it could not be drawn.
     */
    fun showHint(context: Context, text: String, timeoutMs: Long = HINT_TIMEOUT_MS): Boolean =
        addPill(context, text, focusable = false, timeoutMs)

    private fun addPill(context: Context, text: String, focusable: Boolean, timeoutMs: Long): Boolean {
        val app = context.applicationContext
        dismiss()
        val theme = Theme.from(MessageBus.hello.value?.theme)
        val density = app.resources.displayMetrics.density
        fun dp(v: Int) = (v * density).toInt()

        val pill = TextView(app).apply {
            val label = SpannableStringBuilder("● ").append(text)
            label.setSpan(ForegroundColorSpan(theme.green.toArgb()), 0, 1, Spanned.SPAN_EXCLUSIVE_EXCLUSIVE)
            this.text = label
            setTextColor(theme.text.toArgb())
            textSize = 22f
            typeface = ResourcesCompat.getFont(app, R.font.inter_medium) ?: Typeface.DEFAULT
            setPadding(dp(26), dp(14), dp(26), dp(14))
            background = GradientDrawable().apply {
                cornerRadius = dp(40).toFloat()
                setColor(theme.panel[1].toArgb())
                setStroke(dp(1), theme.borderStrong.toArgb())
            }
            isFocusable = focusable
            isFocusableInTouchMode = focusable
            if (focusable) setOnKeyListener { _, keyCode, event ->
                when (noticeKey(keyCode, event.action, event.repeatCount)) {
                    NoticeKey.OPEN -> { open(app); true }
                    NoticeKey.DISMISS -> { dismiss(); true }
                    NoticeKey.IGNORE -> false
                }
            }
            // Show focus the way the posters do: a brighter outline.
            setOnFocusChangeListener { v, focused ->
                (v.background as GradientDrawable).setStroke(dp(if (focused) 2 else 1), (if (focused) theme.text else theme.borderStrong).toArgb())
            }
        }
        val params = WindowManager.LayoutParams(
            WindowManager.LayoutParams.WRAP_CONTENT,
            WindowManager.LayoutParams.WRAP_CONTENT,
            WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY,
            if (focusable) {
                WindowManager.LayoutParams.FLAG_NOT_TOUCH_MODAL
            } else {
                WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE or WindowManager.LayoutParams.FLAG_NOT_TOUCHABLE
            },
            PixelFormat.TRANSLUCENT,
        ).apply {
            gravity = Gravity.BOTTOM or Gravity.END
            x = dp(48)
            y = dp(40)
        }
        val wm = app.getSystemService(WindowManager::class.java)
        try {
            wm.addView(pill, params)
        } catch (e: RuntimeException) { // SYSTEM_ALERT_WINDOW not granted, or the window token is gone
            Log.e(TAG, "could not show the notice: ${e.javaClass.simpleName}")
            return false
        }
        view = pill
        windowManager = wm
        if (focusable) pill.requestFocus()
        handler.postDelayed(timeout, timeoutMs)
        return true
    }

    /** Removes the pill; key focus returns to the app underneath. Never sends a key or pauses anything. */
    fun dismiss() {
        handler.removeCallbacks(timeout)
        val v = view ?: return
        view = null
        runCatching { windowManager?.removeView(v) }.onFailure { Log.d(TAG, "notice already gone: ${it.javaClass.simpleName}") }
    }

    private fun open(context: Context) {
        dismiss()
        MessageBus.restartRenderClock()
        try {
            context.startActivity(
                Intent(context, AnswerActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_SINGLE_TOP),
            )
        } catch (e: RuntimeException) {
            Log.e(TAG, "could not open the answer: ${e.javaClass.simpleName}")
        }
    }
}
