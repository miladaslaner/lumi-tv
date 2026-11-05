package io.github.miladaslaner.lumitv

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent

/** Starts the TV link at boot and after an update (an install stops the app; nothing else would start it again). */
class BootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action in STARTS) LinkService.startGuarded(context)
    }

    private companion object {
        val STARTS = setOf(Intent.ACTION_BOOT_COMPLETED, Intent.ACTION_MY_PACKAGE_REPLACED)
    }
}
