package io.github.miladaslaner.lumitv

import kotlinx.coroutines.runBlocking
import okhttp3.OkHttpClient
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import org.json.JSONObject
import org.junit.After
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Test
import java.net.ServerSocket

class AskClientTest {
    private val server = MockWebServer()
    private val http = OkHttpClient()

    @After fun tearDown() {
        server.shutdown()
    }

    private fun wsUrl() = "ws://${server.hostName}:${server.port}/tv"

    @Test fun askUrlMapsSchemeAndPath() {
        assertEquals("http://10.0.0.2:8765/ask", askUrl("ws://10.0.0.2:8765/tv", "/ask"))
        assertEquals("https://lumi.example:8443/ask", askUrl("wss://lumi.example:8443/tv", "/ask"))
    }

    @Test fun postsBearerAndJsonAndAcceptsOn202() = runBlocking {
        server.enqueue(MockResponse().setResponseCode(202).setBody("""{"ask_id":"k1k1k1k1k1k1"}"""))
        server.start()
        val client = AskClient(http) { wsUrl() to "t" }
        assertEquals(PostResult.Accepted, client.postText("k1k1k1k1k1k1", "something funny", "typed", "a9", null))
        val req = server.takeRequest()
        assertEquals("POST", req.method)
        assertEquals("/ask", req.path)
        assertEquals("Bearer t", req.getHeader("Authorization"))
        val body = JSONObject(req.body.readUtf8())
        assertEquals("k1k1k1k1k1k1", body.getString("ask_id"))
        assertEquals("something funny", body.getString("text"))
        assertEquals("typed", body.getString("source"))
        assertEquals("a9", body.getString("answer_id"))
    }

    @Test fun badRequestIsRejected() = runBlocking {
        server.enqueue(MockResponse().setResponseCode(400))
        server.start()
        assertEquals(PostResult.Rejected(400), AskClient(http) { wsUrl() to "t" }.postText("k1k1k1k1k1k1", "x", "typed", null, null))
    }

    @Test fun noServerIsUnreachable() = runBlocking {
        val port = ServerSocket(0).use { it.localPort } // free port, nothing listening
        val client = AskClient(http) { "ws://127.0.0.1:$port/tv" to "t" }
        assertEquals(PostResult.Unreachable, client.postText("k1k1k1k1k1k1", "x", "typed", null, null))
    }

    @Test fun postAudioSendsWavWithBearer() = runBlocking {
        server.enqueue(MockResponse().setResponseCode(202).setBody("""{"ask_id":"k1k1k1k1k1k1"}"""))
        server.start()
        val wav = wav(shortArrayOf(1, 2, 3))
        val client = AskClient(http) { wsUrl() to "t" }
        assertEquals(PostResult.Accepted, client.postAudio("k1k1k1k1k1k1", wav, "a9", "tv:1399"))
        val req = server.takeRequest()
        assertEquals("POST", req.method)
        assertEquals("/ask/audio", req.requestUrl!!.encodedPath)
        assertEquals("k1k1k1k1k1k1", req.requestUrl!!.queryParameter("ask_id"))
        assertEquals("a9", req.requestUrl!!.queryParameter("answer_id"))
        assertEquals("tv:1399", req.requestUrl!!.queryParameter("focused_token"))
        assertEquals("Bearer t", req.getHeader("Authorization"))
        assertEquals("audio/wav", req.getHeader("Content-Type"))
        assertArrayEquals(wav, req.body.readByteArray())
    }

    @Test fun postAudioLeavesOutUnknownContext() = runBlocking {
        server.enqueue(MockResponse().setResponseCode(503))
        server.start()
        val result = AskClient(http) { wsUrl() to "t" }.postAudio("k1k1k1k1k1k1", wav(ShortArray(1)), null, null)
        assertEquals(PostResult.Rejected(503), result)
        assertEquals(setOf("ask_id"), server.takeRequest().requestUrl!!.queryParameterNames)
    }

    @Test fun notConfiguredIsUnreachable() = runBlocking {
        assertEquals(PostResult.Unreachable, AskClient(http) { null }.postText("k1k1k1k1k1k1", "x", "typed", null, null))
    }
}
