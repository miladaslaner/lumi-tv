package io.github.miladaslaner.lumitv

import android.view.KeyEvent
import org.junit.Assert.assertEquals
import org.junit.Test

class RouteTest {
    @Test fun playingRoutesToNotice() = assertEquals(Mode.NOTICE, route(playing = true, known = true))

    @Test fun notPlayingRoutesToScreen() = assertEquals(Mode.SCREEN, route(playing = false, known = true))

    @Test fun unknownPlaybackFailsSafeToNotice() {
        assertEquals(Mode.NOTICE, route(playing = false, known = false))
        assertEquals(Mode.NOTICE, route(playing = true, known = false))
    }

    @Test fun screenOffAcksTvOffAndShowsNothing() {
        val a = answerAction(Mode.SCREEN, interactive = false, canDrawOverlays = true, answerOnTop = false)
        assertEquals(AnswerAction.TV_OFF, a)
        assertEquals("tv_off", a.ack)
        assertEquals(AnswerAction.TV_OFF, answerAction(Mode.NOTICE, interactive = false, canDrawOverlays = true, answerOnTop = true))
    }

    @Test fun withOverlayGrantScreenAndNoticeAreShown() {
        assertEquals(AnswerAction.SCREEN, answerAction(Mode.SCREEN, interactive = true, canDrawOverlays = true, answerOnTop = false))
        assertEquals(AnswerAction.NOTICE, answerAction(Mode.NOTICE, interactive = true, canDrawOverlays = true, answerOnTop = false))
        assertEquals("screen", AnswerAction.SCREEN.ack)
        assertEquals("notice", AnswerAction.NOTICE.ack)
    }

    @Test fun withoutOverlayGrantNothingIsClaimed() {
        val a = answerAction(Mode.SCREEN, interactive = true, canDrawOverlays = false, answerOnTop = false)
        assertEquals(AnswerAction.NONE, a)
        assertEquals(null, a.ack)
        assertEquals(AnswerAction.NONE, answerAction(Mode.NOTICE, interactive = true, canDrawOverlays = false, answerOnTop = true))
    }

    @Test fun withoutOverlayGrantTheScreenStillUpdatesWhenLumiIsOnTop() =
        assertEquals(AnswerAction.SCREEN, answerAction(Mode.SCREEN, interactive = true, canDrawOverlays = false, answerOnTop = true))

    private fun s(state: Int?, pausedSince: Long? = null) = SessionSnapshot(state, pausedSince)
    private val now = 100 * 60_000L

    @Test fun anySessionPlayingCounts() {
        assertEquals(false, isBusy(emptyList(), now))
        assertEquals(false, isBusy(listOf(s(null), s(1 /* STOPPED */)), now))
        assertEquals(true, isBusy(listOf(s(1), s(3 /* STATE_PLAYING */)), now))
    }

    @Test fun recentlyPausedCountsAsWatching() {
        assertEquals(true, isBusy(listOf(s(2 /* PAUSED */, pausedSince = now - 60_000)), now))
        assertEquals(true, isBusy(listOf(s(2, pausedSince = now - PAUSED_BUSY_MS + 1)), now))
    }

    @Test fun longPausedSessionNoLongerBlocks() {
        assertEquals(false, isBusy(listOf(s(2, pausedSince = now - PAUSED_BUSY_MS)), now))
        assertEquals(false, isBusy(listOf(s(2, pausedSince = null)), now))
    }

    @Test fun pausedExpiryIsWhenTheLastRecentPauseAgesOut() {
        val sessions = listOf(s(2, now - 60_000), s(2, now - 120_000), s(2, now - PAUSED_BUSY_MS - 1), s(3))
        assertEquals(now - 60_000 + PAUSED_BUSY_MS, pausedExpiry(sessions, now))
        assertEquals(null, pausedExpiry(listOf(s(3), s(2, now - PAUSED_BUSY_MS)), now))
    }

    @Test fun lumiOnTopGetsTheScreenEvenWhenUnknownOrBusy() {
        assertEquals(Mode.SCREEN, route(playing = false, known = false, answerOnTop = true))
        assertEquals(Mode.SCREEN, route(playing = true, known = true, answerOnTop = true))
        assertEquals(Mode.NOTICE, route(playing = false, known = false, answerOnTop = false))
    }

    @Test fun okOpensOnFirstPressOnly() {
        assertEquals(NoticeKey.OPEN, noticeKey(KeyEvent.KEYCODE_DPAD_CENTER, KeyEvent.ACTION_DOWN, 0))
        assertEquals(NoticeKey.OPEN, noticeKey(KeyEvent.KEYCODE_ENTER, KeyEvent.ACTION_DOWN, 0))
        assertEquals(NoticeKey.IGNORE, noticeKey(KeyEvent.KEYCODE_DPAD_CENTER, KeyEvent.ACTION_DOWN, 1))
        assertEquals(NoticeKey.IGNORE, noticeKey(KeyEvent.KEYCODE_DPAD_CENTER, KeyEvent.ACTION_UP, 0))
    }

    @Test fun backDismisses() =
        assertEquals(NoticeKey.DISMISS, noticeKey(KeyEvent.KEYCODE_BACK, KeyEvent.ACTION_DOWN, 0))

    @Test fun navigationKeysDismiss() {
        assertEquals(NoticeKey.DISMISS, noticeKey(KeyEvent.KEYCODE_DPAD_LEFT, KeyEvent.ACTION_DOWN, 0))
        assertEquals(NoticeKey.DISMISS, noticeKey(KeyEvent.KEYCODE_DPAD_DOWN, KeyEvent.ACTION_DOWN, 0))
    }

    @Test fun bufferingCountsAsPlaying() = assertEquals(true, isBusy(listOf(s(6 /* STATE_BUFFERING */)), now))

    @Test fun mediaAndVolumeKeysAreLeftAlone() {
        assertEquals(NoticeKey.IGNORE, noticeKey(KeyEvent.KEYCODE_MEDIA_PLAY_PAUSE, KeyEvent.ACTION_DOWN, 0))
        assertEquals(NoticeKey.IGNORE, noticeKey(KeyEvent.KEYCODE_VOLUME_UP, KeyEvent.ACTION_DOWN, 0))
    }
}
