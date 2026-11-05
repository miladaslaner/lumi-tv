package io.github.miladaslaner.lumitv

import android.Manifest
import android.content.Context
import android.content.pm.PackageManager
import android.media.AudioFormat
import android.media.AudioManager
import android.media.AudioRecord
import android.media.MediaRecorder
import android.util.Log
import kotlin.math.sqrt

/**
 * Speech threshold (frame RMS, 16-bit PCM) of the remote-mic capture when none is configured: about 3 × the room's
 * noise floor, never below 300. Measured on a TCL Google TV with a Bluetooth remote: noise RMS 11–30 with nobody
 * talking, speech 700–1800. A noisier room sets its own with `CONFIGURE --ei mic_threshold <3 × noise floor>`.
 */
const val DEFAULT_MIC_THRESHOLD = 300

/**
 * The `AudioRecord` source when none is configured (`--ei mic_source`). Measured on a TCL Google TV with a Bluetooth
 * remote: after a mic press VOICE_RECOGNITION carries the remote's voice (speech RMS 700–1800, transcribed fine);
 * MIC, DEFAULT, UNPROCESSED and VOICE_COMMUNICATION got only near-silence (RMS 11–48).
 */
const val DEFAULT_MIC_SOURCE = MediaRecorder.AudioSource.VOICE_RECOGNITION

/** The speech threshold to use for a configured value: never below [DEFAULT_MIC_THRESHOLD]. */
fun micThreshold(configured: Int): Double = configured.coerceAtLeast(DEFAULT_MIC_THRESHOLD).toDouble()

/**
 * Finds the end of one spoken question in 20 ms frames by their RMS: [Verdict.NoSpeech] when nothing louder than
 * [threshold] lasts [minSpeechMs] within [startTimeoutMs]; [Verdict.SpeechEnded] after [silenceMs] of quiet that
 * follows speech; [Verdict.MaxReached] at [maxMs] in any case. Pure; one detector per capture.
 */
class EndpointDetector(
    private val sampleRate: Int = 16000,
    private val startTimeoutMs: Int = 5000,
    private val silenceMs: Int = 800,
    private val maxMs: Int = 15000,
    private val threshold: Double = DEFAULT_MIC_THRESHOLD.toDouble(),
    private val minSpeechMs: Int = 60,
) {
    enum class Verdict { Continue, SpeechEnded, NoSpeech, MaxReached }

    /** Speech (louder than the threshold for at least [minSpeechMs]) was heard. */
    var heardSpeech = false
        private set

    /** Quiet since the last speech, in ms: how long ago speech ended when [Verdict.SpeechEnded] is returned. */
    var trailingSilenceMs = 0
        private set

    private var elapsedMs = 0
    private var loudRunMs = 0
    private var verdict = Verdict.Continue

    fun feed(frame: ShortArray): Verdict {
        if (verdict != Verdict.Continue) return verdict
        val ms = frame.size * 1000 / sampleRate
        elapsedMs += ms
        if (rms(frame) >= threshold) {
            loudRunMs += ms
            if (loudRunMs >= minSpeechMs) heardSpeech = true
            trailingSilenceMs = 0
        } else {
            loudRunMs = 0
            if (heardSpeech) trailingSilenceMs += ms
        }
        verdict = when {
            heardSpeech && trailingSilenceMs >= silenceMs -> Verdict.SpeechEnded
            elapsedMs >= maxMs -> Verdict.MaxReached
            !heardSpeech && elapsedMs >= startTimeoutMs -> Verdict.NoSpeech
            else -> Verdict.Continue
        }
        return verdict
    }

    private fun rms(frame: ShortArray): Double {
        if (frame.isEmpty()) return 0.0
        var sum = 0.0
        for (s in frame) sum += s.toDouble() * s
        return sqrt(sum / frame.size)
    }
}

/** The first [length] samples of [pcm] as a WAV file: 44-byte RIFF header, mono, 16-bit PCM, [sampleRate]. */
fun wav(pcm: ShortArray, sampleRate: Int = 16000, length: Int = pcm.size): ByteArray {
    val dataBytes = length * 2
    val out = ByteArray(44 + dataBytes)
    fun ascii(at: Int, s: String) = s.forEachIndexed { i, c -> out[at + i] = c.code.toByte() }
    fun le16(at: Int, v: Int) { out[at] = v.toByte(); out[at + 1] = (v shr 8).toByte() }
    fun le32(at: Int, v: Int) { le16(at, v); le16(at + 2, v shr 16) }
    ascii(0, "RIFF"); le32(4, 36 + dataBytes); ascii(8, "WAVEfmt ")
    le32(16, 16); le16(20, 1); le16(22, 1); le32(24, sampleRate); le32(28, sampleRate * 2); le16(32, 2); le16(34, 16)
    ascii(36, "data"); le32(40, dataBytes)
    for (i in 0 until length) le16(44 + 2 * i, pcm[i].toInt())
    return out
}

/**
 * How the remote mic is read: an `AudioRecord` [source], an optional preferred input [deviceType], the speech
 * [threshold], and the longest clip the server takes ([maxMs], from `hello.limits`).
 */
data class MicSettings(val source: Int, val deviceType: Int, val threshold: Double, val maxMs: Int = AskLimits.DEFAULT.audioMs)

/**
 * Path A: records the remote's mic with `AudioRecord` (16 kHz mono 16-bit), ends the question with
 * [EndpointDetector] and hands it over as WAV ([CaptureEvent.Audio]) for the server's speech-to-text. The audio is
 * held only in memory (never a file, never logged) and the buffer is zeroed as soon as the WAV is made.
 */
class RemoteMicCapture(
    context: Context,
    private val settings: () -> MicSettings,
    private val clock: () -> Long = MessageBus::nowMs,
) : VoiceCapture {
    private val app = context.applicationContext
    @Volatile private var run: Recording? = null

    /** Permission and mode are checked by [TvVoiceCapture]; `AudioRecord` tells at start whether a mic is there. */
    override val available = true

    override fun start(askId: String, onEvent: (CaptureEvent) -> Unit) {
        cancel()
        if (app.checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
            return onEvent(CaptureEvent.Failed("permission"))
        }
        run = Recording(askId, settings(), onEvent).also { it.start() }
    }

    override fun stop() {
        run?.stopNow = true
    }

    override fun cancel() {
        val old = run ?: return
        run = null
        old.cancelled = true
        old.join(JOIN_MS) // the mic is exclusive: let the old recording release it before a new one opens it
    }

    private inner class Recording(
        private val askId: String,
        private val s: MicSettings,
        private val onEvent: (CaptureEvent) -> Unit,
    ) : Thread("lumi-mic") {
        @Volatile var cancelled = false
        @Volatile var stopNow = false

        private fun emit(e: CaptureEvent) {
            if (!cancelled) onEvent(e)
        }

        override fun run() {
            val frame = ShortArray(RATE / 50) // 20 ms
            val pcm = ShortArray(RATE / 1000 * s.maxMs)
            var n = 0
            var rec: AudioRecord? = null
            try {
                val minBuf = AudioRecord.getMinBufferSize(RATE, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT)
                rec = AudioRecord(s.source, RATE, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT, maxOf(minBuf, frame.size * 2 * 10))
                if (rec.state != AudioRecord.STATE_INITIALIZED) return emit(CaptureEvent.Failed("mic"))
                if (s.deviceType != 0) {
                    app.getSystemService(AudioManager::class.java).getDevices(AudioManager.GET_DEVICES_INPUTS)
                        .firstOrNull { it.type == s.deviceType }?.let { rec.setPreferredDevice(it) }
                }
                rec.startRecording()
                val detector = EndpointDetector(RATE, maxMs = s.maxMs, threshold = s.threshold)
                var verdict = EndpointDetector.Verdict.Continue
                var started = false
                while (!cancelled && !stopNow && verdict == EndpointDetector.Verdict.Continue) {
                    if (!readFrame(rec, frame)) return emit(CaptureEvent.Failed("mic"))
                    if (!started) {
                        started = true
                        emit(CaptureEvent.Started)
                    }
                    frame.copyInto(pcm, n)
                    n += frame.size
                    verdict = detector.feed(frame)
                }
                rec.stop()
                if (cancelled) return
                Log.i(TAG, "ask $askId mic: $verdict after ${n / (RATE / 1000)}ms")
                if (!detector.heardSpeech) return emit(CaptureEvent.NothingHeard)
                val endAt = clock() - if (verdict == EndpointDetector.Verdict.SpeechEnded) detector.trailingSilenceMs else 0
                emit(CaptureEvent.Audio(wav(pcm, RATE, n), endAt))
            } catch (e: SecurityException) {
                emit(CaptureEvent.Failed("permission"))
            } catch (e: RuntimeException) { // IllegalArgumentException / IllegalStateException from AudioRecord
                Log.w(TAG, "ask $askId mic failed: ${e.javaClass.simpleName}")
                emit(CaptureEvent.Failed("mic"))
            } finally {
                rec?.release()
                pcm.fill(0)
                frame.fill(0)
            }
        }

        /** Fills [frame]; false when the mic stops delivering. */
        private fun readFrame(rec: AudioRecord, frame: ShortArray): Boolean {
            var got = 0
            while (got < frame.size) {
                val r = rec.read(frame, got, frame.size - got)
                if (r <= 0) return false
                got += r
            }
            return true
        }
    }

    private companion object {
        const val RATE = 16000
        const val JOIN_MS = 200L
    }
}
