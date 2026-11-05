package io.github.miladaslaner.lumitv

import coil.Coil
import org.junit.Assert.assertEquals
import org.junit.Assert.assertSame
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.RuntimeEnvironment
import org.robolectric.annotation.Config

@RunWith(RobolectricTestRunner::class)
@Config(application = LumiApp::class, sdk = [34])
class PosterPrefetchTest {
    private fun pick(url: String?) = PickUi(
        token = "t", title = "x", posterUrl = url, serviceLabel = "", lengthLabel = null, launches = listOf(Launch("p", "a")),
    )

    @Test fun everyPosterOfAShowIsPrefetchedOnce() {
        val urls = posterUrls(listOf(pick("a"), pick(null), pick("b"), pick("a"), pick("c")))
        assertEquals(listOf("a", "b", "c"), urls)
        val seen = mutableListOf<String>()
        assertEquals(3, prefetchPosters(urls) { seen += it })
        assertEquals(urls, seen)
    }

    @Test fun noPostersNoPrefetch() {
        assertTrue(posterUrls(listOf(pick(null))).isEmpty())
    }

    @Test fun loaderIsTheAppSingleton() {
        val app = RuntimeEnvironment.getApplication()
        assertTrue(app is LumiApp)
        assertSame(Coil.imageLoader(app), Coil.imageLoader(app))
    }
}
