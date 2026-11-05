package io.github.miladaslaner.lumitv

import android.app.Application
import android.content.Context
import coil.ImageLoader
import coil.ImageLoaderFactory
import coil.disk.DiskCache
import coil.memory.MemoryCache
import okhttp3.OkHttpClient
import java.util.concurrent.TimeUnit

/**
 * The one OkHttp client of the process (one dispatcher, one connection pool): the server link's WebSocket, the asks
 * and the posters all go through it. The ping keeps the link alive through the TV's idle network.
 */
val httpClient: OkHttpClient by lazy { OkHttpClient.Builder().pingInterval(20, TimeUnit.SECONDS).build() }

/**
 * Owns the one Coil [ImageLoader] (Coil asks this factory once and keeps the result as the process
 * singleton, so `context.imageLoader`, `AsyncImage` and the prefetch below all share its memory and
 * disk cache).
 */
class LumiApp : Application(), ImageLoaderFactory {
    override fun onCreate() {
        super.onCreate()
        AskController.install(LiveAskRuntime(this, tvVoiceCapture(this)))
    }

    override fun newImageLoader(): ImageLoader = ImageLoader.Builder(this)
        .okHttpClient(httpClient)
        .memoryCache { MemoryCache.Builder(this).maxSizePercent(0.25).build() }
        .diskCache { DiskCache.Builder().directory(cacheDir.resolve("posters")).maxSizeBytes(DISK_CACHE_BYTES).build() }
        .crossfade(false)
        .build()

    private companion object {
        const val DISK_CACHE_BYTES = 64L * 1024 * 1024
    }
}

/** Distinct poster URLs of a `show`, in pick order; picks without a poster are skipped. */
fun posterUrls(picks: List<PickUi>): List<String> = picks.mapNotNull { it.posterUrl }.distinct()

/** Starts a fetch (into the shared loader's caches) for every poster; returns how many were started. */
fun prefetchPosters(urls: List<String>, enqueue: (String) -> Unit): Int = urls.onEach(enqueue).size

/** Prefetch into the app's singleton loader. */
fun prefetchPosters(context: Context, urls: List<String>): Int {
    val loader = coil.Coil.imageLoader(context)
    return prefetchPosters(urls) { url ->
        loader.enqueue(coil.request.ImageRequest.Builder(context).data(url).build())
    }
}
