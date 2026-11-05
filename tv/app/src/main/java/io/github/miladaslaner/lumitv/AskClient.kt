package io.github.miladaslaner.lumitv

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.HttpUrl.Companion.toHttpUrl
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody
import okhttp3.RequestBody.Companion.toRequestBody
import java.io.IOException
import java.util.concurrent.TimeUnit

/** The HTTP URL of [path] on the server the TV link points at (`ws://h:p/tv` → `http://h:p/ask`; `wss` → `https`). */
fun askUrl(wsUrl: String, path: String): String =
    toHttpScheme(wsUrl).toHttpUrl().newBuilder().encodedPath(path).query(null).fragment(null).build().toString()

/** How the server took an ask. */
sealed interface PostResult {
    /** 2xx: the server works on it; the answer comes over the TV link. */
    data object Accepted : PostResult

    /** The server answered with [code] (400 bad body, 401 token, 503 asking disabled). */
    data class Rejected(val code: Int) : PostResult

    /** No answer: not configured, bad URL, no network, or the 10 s call timeout. */
    data object Unreachable : PostResult
}

/** A fixed name for logs (release builds shorten class names). */
val PostResult.logName: String
    get() = when (this) {
        PostResult.Accepted -> "Accepted"
        is PostResult.Rejected -> "Rejected($code)"
        PostResult.Unreachable -> "Unreachable"
    }

/**
 * Sends asks to the server's `POST /ask` (text) and `POST /ask/audio` (WAV). [config] gives the link's (url, token)
 * from the app's prefs, or null when not configured.
 */
class AskClient(http: OkHttpClient, private val config: () -> Pair<String, String>?) {
    private val http = http.newBuilder().callTimeout(CALL_TIMEOUT_S, TimeUnit.SECONDS).build()

    suspend fun postText(
        askId: String, text: String, source: String, answerId: String?, focusedToken: String?, viewers: List<String> = emptyList(),
    ): PostResult =
        post({ url -> askUrl(url, "/ask") }, askRequestJson(askId, text, source, answerId, focusedToken, viewers).toRequestBody(JSON))

    /**
     * Sends a spoken question as WAV to `POST /ask/audio` (Path A: the server transcribes it). [wav] is only read
     * here; the caller zeroes it afterwards.
     */
    suspend fun postAudio(
        askId: String, wav: ByteArray, answerId: String?, focusedToken: String?, viewers: List<String> = emptyList(),
    ): PostResult =
        post({ url ->
            askUrl(url, "/ask/audio").toHttpUrl().newBuilder()
                .addQueryParameter("ask_id", askId)
                .apply { answerId?.let { addQueryParameter("answer_id", it) } }
                .apply { focusedToken?.let { addQueryParameter("focused_token", it) } }
                .apply { if (viewers.isNotEmpty()) addQueryParameter("viewers", viewers.joinToString(",")) }
                .build().toString()
        }, wav.toRequestBody(WAV))

    private suspend fun post(target: (String) -> String, body: RequestBody): PostResult {
        val (url, token) = config() ?: return PostResult.Unreachable
        val request = try {
            Request.Builder()
                .url(target(url))
                .header("Authorization", "Bearer $token")
                .post(body)
                .build()
        } catch (e: IllegalArgumentException) {
            return PostResult.Unreachable
        }
        return withContext(Dispatchers.IO) {
            try {
                http.newCall(request).execute().use { if (it.isSuccessful) PostResult.Accepted else PostResult.Rejected(it.code) }
            } catch (e: IOException) {
                PostResult.Unreachable
            }
        }
    }

    private companion object {
        // 10 s, not 5: a busy host can take over 5 s just to accept a spoken question's upload
        const val CALL_TIMEOUT_S = 10L
        val JSON = "application/json".toMediaType()
        val WAV = "audio/wav".toMediaType()
    }
}
