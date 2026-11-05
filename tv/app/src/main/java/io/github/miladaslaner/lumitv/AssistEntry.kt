package io.github.miladaslaner.lumitv

import android.app.Activity
import android.app.role.RoleManager
import android.content.Context
import android.content.Intent
import android.os.Bundle
import android.service.voice.VoiceInteractionService
import android.service.voice.VoiceInteractionSession
import android.service.voice.VoiceInteractionSessionService
import android.speech.RecognitionService
import android.speech.SpeechRecognizer
import android.util.Log
import android.view.KeyEvent

/**
 * The mic button's ways into Lumi. Which one a TV delivers depends on the device; all are declared, and none does
 * anything until the controller makes Lumi the TV's assistant over adb
 * (`cmd role add-role-holder android.app.role.ASSISTANT <pkg>`):
 * - session: [LumiVoiceService] (assistant with a voice-interaction service) -> [LumiSession.onShow];
 * - intent: [AssistActivity] (`android.intent.action.ASSIST`, an activity-only assistant);
 * - key: a mic key ([MIC_KEYS]) delivered to [AnswerActivity] while it is in front;
 * - none of them: the idle screen's "Ask Lumi" pill (OK).
 * Every one ends in [press].
 */
object AssistEntry {
    /** Keys a TV remote's mic or assistant button may send to the app in front. */
    val MIC_KEYS = setOf(KeyEvent.KEYCODE_SEARCH, KeyEvent.KEYCODE_VOICE_ASSIST, KeyEvent.KEYCODE_ASSIST)

    /**
     * A mic press: starts a new ask (pausing what plays, while it is still the app in front) and, when [showScreen],
     * brings Lumi's answer screen to the front (singleTop: an answer screen already in front just stays).
     */
    fun press(context: Context, via: String, showScreen: Boolean = true) {
        Log.i(TAG, "ask press via $via")
        AskController.dispatch(AskEvent.Press(voice = AskController.voiceAvailable, playing = PlaybackWatcher.isPlayingNow()))
        if (!showScreen) return
        try {
            context.startActivity(
                Intent(context, AnswerActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_SINGLE_TOP),
            )
        } catch (e: RuntimeException) {
            Log.e(TAG, "could not start the ask screen: ${e.javaClass.simpleName}")
        }
    }

    /** Lumi is the TV's assistant, so the mic button reaches it (the session or intent entry). */
    fun voiceEntryInstalled(context: Context): Boolean = try {
        context.getSystemService(RoleManager::class.java)?.isRoleHeld(RoleManager.ROLE_ASSISTANT) == true
    } catch (e: RuntimeException) {
        false
    }
}

/**
 * Intent entry: the system starts this (translucent, no UI) for `ASSIST` when Lumi is an activity-only assistant.
 * Without the assistant role it does nothing (logs only): an `ASSIST` that resolves here by itself, or any app
 * starting it, must not pause a video or open Lumi before the controller made Lumi the assistant.
 */
class AssistActivity : Activity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        if (AssistEntry.voiceEntryInstalled(this)) {
            AssistEntry.press(this, via = "intent")
        } else {
            Log.i(TAG, "ASSIST without the assistant role: ignored")
        }
        finish()
    }
}

/** Session entry: the voice-interaction service the assistant role binds. It does nothing until a session shows. */
class LumiVoiceService : VoiceInteractionService()

class LumiSessionService : VoiceInteractionSessionService() {
    override fun onNewSession(args: Bundle?): VoiceInteractionSession = LumiSession(this)
}

/** Shows no UI of its own and never reads the assist data: the press opens Lumi's ask screen, then the session hides. */
class LumiSession(context: Context) : VoiceInteractionSession(context) {
    override fun onShow(args: Bundle?, showFlags: Int) {
        super.onShow(args, showFlags)
        AssistEntry.press(context, via = "session")
        hide()
    }
}

/**
 * Required by the voice-interaction service declaration; Lumi recognizes speech elsewhere, so it refuses.
 * `selectableAsDefault="false"` (res/xml/lumi_recognizer.xml) keeps it from becoming the TV's recognizer by itself;
 * only the assistant role (which sets the recognizer to its voice-interaction service's) points the TV at it.
 */
class LumiRecognitionService : RecognitionService() {
    override fun onStartListening(recognizerIntent: Intent?, listener: Callback?) {
        runCatching { listener?.error(SpeechRecognizer.ERROR_CLIENT) }
    }

    override fun onCancel(listener: Callback?) = Unit
    override fun onStopListening(listener: Callback?) = Unit
}
