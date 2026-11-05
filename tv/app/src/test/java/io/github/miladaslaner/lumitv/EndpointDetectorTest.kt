package io.github.miladaslaner.lumitv

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.nio.ByteBuffer
import java.nio.ByteOrder

class EndpointDetectorTest {
    private val silent = ShortArray(320) // 20 ms at 16 kHz
    private val loud = ShortArray(320) { if (it % 2 == 0) 3000 else -3000 } // RMS 3000

    private fun feed(d: EndpointDetector, frame: ShortArray, times: Int): List<EndpointDetector.Verdict> =
        List(times) { d.feed(frame) }

    @Test fun noSpeechWithin5sGivesNoSpeech() {
        val d = EndpointDetector(threshold = 300.0)
        val verdicts = feed(d, silent, 250) // 5000 ms
        assertTrue(verdicts.dropLast(1).all { it == EndpointDetector.Verdict.Continue })
        assertEquals(EndpointDetector.Verdict.NoSpeech, verdicts.last())
        assertFalse(d.heardSpeech)
    }

    @Test fun speechThen800msSilenceEnds() {
        val d = EndpointDetector(threshold = 300.0)
        assertTrue(feed(d, loud, 50).all { it == EndpointDetector.Verdict.Continue }) // 1 s of speech
        val after = feed(d, silent, 40) // 800 ms of silence
        assertTrue(after.dropLast(1).all { it == EndpointDetector.Verdict.Continue })
        assertEquals(EndpointDetector.Verdict.SpeechEnded, after.last())
        assertEquals(800, d.trailingSilenceMs)
        assertTrue(d.heardSpeech)
    }

    @Test fun pauseShorterThanSilenceKeepsListening() {
        val d = EndpointDetector(threshold = 300.0)
        feed(d, loud, 25)
        assertTrue(feed(d, silent, 30).all { it == EndpointDetector.Verdict.Continue }) // 600 ms pause
        feed(d, loud, 25)
        assertEquals(0, d.trailingSilenceMs)
        assertEquals(EndpointDetector.Verdict.SpeechEnded, feed(d, silent, 40).last())
    }

    @Test fun stopsAt15s() {
        val d = EndpointDetector(threshold = 300.0)
        val verdicts = feed(d, loud, 750) // 15 000 ms of talking
        assertTrue(verdicts.dropLast(1).all { it == EndpointDetector.Verdict.Continue })
        assertEquals(EndpointDetector.Verdict.MaxReached, verdicts.last())
    }

    @Test fun aClickIsNotSpeech() {
        val d = EndpointDetector(threshold = 300.0)
        d.feed(loud) // one 20 ms frame (a remote button click)
        assertEquals(EndpointDetector.Verdict.NoSpeech, feed(d, silent, 249).last())
        assertFalse(d.heardSpeech)
    }

    @Test fun quietRoomBelowThresholdIsNoSpeech() {
        val d = EndpointDetector(threshold = 300.0)
        val hum = ShortArray(320) { if (it % 2 == 0) 200 else -200 }
        assertEquals(EndpointDetector.Verdict.NoSpeech, feed(d, hum, 250).last())
    }

    @Test fun thresholdHasAFloorOf300() {
        assertEquals(300.0, micThreshold(0), 0.0)
        assertEquals(300.0, micThreshold(120), 0.0)
        assertEquals(1500.0, micThreshold(1500), 0.0)
    }

    @Test fun wavHeaderIs16kMono16bit() {
        val pcm = shortArrayOf(1, -2, 3, 0x1234)
        val bytes = wav(pcm)
        assertEquals(44 + 8, bytes.size)
        val b = ByteBuffer.wrap(bytes).order(ByteOrder.LITTLE_ENDIAN)
        assertEquals("RIFF", String(bytes, 0, 4))
        assertEquals(36 + 8, b.getInt(4))
        assertEquals("WAVEfmt ", String(bytes, 8, 8))
        assertEquals(16, b.getInt(16))
        assertEquals(1, b.getShort(20).toInt()) // PCM
        assertEquals(1, b.getShort(22).toInt()) // mono
        assertEquals(16000, b.getInt(24))
        assertEquals(32000, b.getInt(28))
        assertEquals(2, b.getShort(32).toInt())
        assertEquals(16, b.getShort(34).toInt())
        assertEquals("data", String(bytes, 36, 4))
        assertEquals(8, b.getInt(40))
        assertEquals(1, b.getShort(44).toInt())
        assertEquals(-2, b.getShort(46).toInt())
        assertEquals(0x1234, b.getShort(50).toInt())
    }

    @Test fun wavTakesOnlyTheRecordedPart() {
        val bytes = wav(ShortArray(100) { 7 }, length = 10)
        assertEquals(44 + 20, bytes.size)
        assertEquals(20, ByteBuffer.wrap(bytes).order(ByteOrder.LITTLE_ENDIAN).getInt(40))
    }
}
