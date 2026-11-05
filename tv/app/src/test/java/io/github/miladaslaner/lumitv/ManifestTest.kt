package io.github.miladaslaner.lumitv

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.w3c.dom.Element
import java.io.File
import javax.xml.parsers.DocumentBuilderFactory
import org.junit.Test

class ManifestTest {
    private val ns = "http://schemas.android.com/apk/res/android"

    /** Prefers the merged manifest (includes library contributions); falls back to the source one. */
    private fun manifest(): Element {
        val merged = File("build/intermediates/merged_manifests/debug").walkTopDown().firstOrNull { it.name == "AndroidManifest.xml" }
        val file = merged ?: File("src/main/AndroidManifest.xml")
        return DocumentBuilderFactory.newInstance().apply { isNamespaceAware = true }
            .newDocumentBuilder().parse(file).documentElement
    }

    private fun names(tag: String) = manifest().getElementsByTagName(tag).let { l ->
        (0 until l.length).map { (l.item(it) as Element).getAttributeNS(ns, "name") }
    }

    @Test fun hasLauncherBannerAndIcon() {
        val app = manifest().getElementsByTagName("application").item(0) as Element
        assertEquals("@drawable/banner", app.getAttributeNS(ns, "banner"))
        assertEquals("@drawable/ic_launcher", app.getAttributeNS(ns, "icon"))
        // PNG IHDR: width and height are big-endian ints at bytes 16 and 20
        val png = java.nio.ByteBuffer.wrap(File("src/main/res/drawable-xhdpi/banner.png").readBytes())
        assertEquals(640 to 360, png.getInt(16) to png.getInt(20)) // 320x180 dp at xhdpi
        assertTrue(File("src/main/res/drawable-xxxhdpi/ic_launcher.png").exists())
    }

    @Test fun noHomeCategory() = assertFalse(names("category").contains("android.intent.category.HOME"))

    @Test fun nothingIsBackedUpOrTransferred() {
        // the link token lives in the prefs: never in a cloud backup, never copied to another device
        val app = manifest().getElementsByTagName("application").item(0) as Element
        assertEquals("false", app.getAttributeNS(ns, "allowBackup"))
        assertEquals("@xml/full_backup_content", app.getAttributeNS(ns, "fullBackupContent"))
        assertEquals("@xml/data_extraction_rules", app.getAttributeNS(ns, "dataExtractionRules"))
        val legacy = DocumentBuilderFactory.newInstance().newDocumentBuilder()
            .parse(File("src/main/res/xml/full_backup_content.xml")).documentElement
        assertEquals(0, legacy.getElementsByTagName("include").length)
        assertTrue(legacy.getElementsByTagName("exclude").length >= 4)
        val rules = DocumentBuilderFactory.newInstance().newDocumentBuilder()
            .parse(File("src/main/res/xml/data_extraction_rules.xml")).documentElement
        listOf("cloud-backup", "device-transfer").forEach { section ->
            val s = rules.getElementsByTagName(section).item(0) as Element
            val excluded = s.getElementsByTagName("exclude").let { l -> (0 until l.length).map { (l.item(it) as Element).getAttribute("domain") } }
            assertTrue(section, "sharedpref" in excluded && "file" in excluded && "database" in excluded && "root" in excluded)
            assertEquals(section, 0, s.getElementsByTagName("include").length)
        }
    }

    @Test fun showsInLeanbackLauncher() = assertTrue(names("category").contains("android.intent.category.LEANBACK_LAUNCHER"))

    @Test fun declaresRequiredPermissions() {
        val perms = names("uses-permission")
        listOf("INTERNET", "ACCESS_NETWORK_STATE", "RECEIVE_BOOT_COMPLETED", "FOREGROUND_SERVICE", "SYSTEM_ALERT_WINDOW")
            .forEach { assertTrue(it, perms.contains("android.permission.$it")) }
    }

    /** Visibility by intent, never by package name: a service added to the server's config needs no rebuild. */
    @Test fun seesEveryTvAppAndTheLinkHandlerWithoutNamingPackages() {
        val queries = manifest().getElementsByTagName("queries").item(0) as Element
        assertEquals(0, queries.getElementsByTagName("package").length)
        val intents = queries.getElementsByTagName("intent").let { l -> (0 until l.length).map { l.item(it) as Element } }
            .map { i -> Triple(actionsOf(i).single(), categoriesOf(i), i.getElementsByTagName("data").let { d -> (0 until d.length).map { (d.item(it) as Element).getAttributeNS(ns, "scheme") } }) }
        assertTrue(Triple("android.intent.action.MAIN", listOf("android.intent.category.LEANBACK_LAUNCHER"), emptyList<String>()) in intents)
        assertTrue(Triple("android.intent.action.MAIN", listOf("android.intent.category.LAUNCHER"), emptyList<String>()) in intents)
        assertTrue(Triple("android.intent.action.VIEW", emptyList<String>(), listOf("https")) in intents)
    }

    @Test fun declaresUsageAccess() =
        assertTrue(names("uses-permission").contains("android.permission.PACKAGE_USAGE_STATS"))

    @Test fun configReceiverIsGuardedByDump() {
        val l = manifest().getElementsByTagName("receiver")
        val config = (0 until l.length).map { l.item(it) as Element }.single { it.getAttributeNS(ns, "name").endsWith(".ConfigReceiver") }
        assertEquals("android.permission.DUMP", config.getAttributeNS(ns, "permission"))
    }

    private fun element(tag: String, nameSuffix: String): Element {
        val l = manifest().getElementsByTagName(tag)
        return (0 until l.length).map { l.item(it) as Element }.single { it.getAttributeNS(ns, "name").endsWith(nameSuffix) }
    }

    @Test fun allowsCleartextForLanLink() {
        val app = manifest().getElementsByTagName("application").item(0) as Element
        assertEquals("@xml/network_security_config", app.getAttributeNS(ns, "networkSecurityConfig"))
        val cfg = DocumentBuilderFactory.newInstance().newDocumentBuilder()
            .parse(File("src/main/res/xml/network_security_config.xml")).documentElement
        val base = cfg.getElementsByTagName("base-config").item(0) as Element
        assertEquals("true", base.getAttribute("cleartextTrafficPermitted"))
    }

    @Test fun linkServiceIsSpecialUseForeground() {
        assertTrue(names("uses-permission").contains("android.permission.FOREGROUND_SERVICE_SPECIAL_USE"))
        assertEquals("specialUse", element("service", ".LinkService").getAttributeNS(ns, "foregroundServiceType"))
    }

    @Test fun bootReceiverNotExported() =
        assertEquals("false", element("receiver", ".BootReceiver").getAttributeNS(ns, "exported"))

    // after an update (adb install -r, Play) the link must come back by itself, or answers stop reaching the TV
    @Test fun linkRestartsAfterBootAndAfterAnUpdate() = assertEquals(
        listOf("android.intent.action.BOOT_COMPLETED", "android.intent.action.MY_PACKAGE_REPLACED"),
        actionsOf(element("receiver", ".BootReceiver")),
    )

    @Test fun playbackWatcherIsNotificationListener() {
        val e = element("service", ".PlaybackWatcher")
        assertEquals("android.permission.BIND_NOTIFICATION_LISTENER_SERVICE", e.getAttributeNS(ns, "permission"))
        assertEquals("true", e.getAttributeNS(ns, "exported"))
        val actions = e.getElementsByTagName("action")
        val names = (0 until actions.length).map { (actions.item(it) as Element).getAttributeNS(ns, "name") }
        assertTrue(names.contains("android.service.notification.NotificationListenerService"))
    }
    private fun actionsOf(e: Element): List<String> = e.getElementsByTagName("action").let { l ->
        (0 until l.length).map { (l.item(it) as Element).getAttributeNS(ns, "name") }
    }

    private fun categoriesOf(e: Element): List<String> = e.getElementsByTagName("category").let { l ->
        (0 until l.length).map { (l.item(it) as Element).getAttributeNS(ns, "name") }
    }

    @Test fun declaresRecordAudio() {
        assertTrue(names("uses-permission").contains("android.permission.RECORD_AUDIO"))
        // A TV without a mic can still install Lumi.
        val mic = manifest().getElementsByTagName("uses-feature").let { l -> (0 until l.length).map { l.item(it) as Element } }
            .single { it.getAttributeNS(ns, "name") == "android.hardware.microphone" }
        assertEquals("false", mic.getAttributeNS(ns, "required"))
    }

    @Test fun seesTheTvsSpeechRecognizers() {
        // Package visibility (Android 11+): without this the TV's recognizer can't be found or bound.
        val queries = manifest().getElementsByTagName("queries").item(0) as Element
        val actions = queries.getElementsByTagName("action").let { l -> (0 until l.length).map { (l.item(it) as Element).getAttributeNS(ns, "name") } }
        assertTrue(actions.contains("android.speech.RecognitionService"))
    }

    @Test fun assistActivityIsExportedOnlyWithTheAssistAction() {
        val e = element("activity", ".AssistActivity")
        assertEquals("true", e.getAttributeNS(ns, "exported"))
        assertEquals(listOf("android.intent.action.ASSIST"), actionsOf(e))
        assertEquals(listOf("android.intent.category.DEFAULT"), categoriesOf(e))
    }

    @Test fun answerActivityKeepsOnlyTheLauncherEntry() {
        val e = element("activity", ".AnswerActivity")
        assertEquals(listOf("android.intent.action.MAIN"), actionsOf(e))
        assertEquals("singleTop", e.getAttributeNS(ns, "launchMode"))
    }

    @Test fun voiceSessionServicesAreBoundOnlyByTheSystem() {
        val voice = element("service", ".LumiVoiceService")
        assertEquals("android.permission.BIND_VOICE_INTERACTION", voice.getAttributeNS(ns, "permission"))
        assertEquals(listOf("android.service.voice.VoiceInteractionService"), actionsOf(voice))
        val meta = voice.getElementsByTagName("meta-data").item(0) as Element
        assertEquals("android.voice_interaction", meta.getAttributeNS(ns, "name"))
        assertEquals("@xml/lumi_voice", meta.getAttributeNS(ns, "resource"))
        val session = element("service", ".LumiSessionService")
        assertEquals("android.permission.BIND_VOICE_INTERACTION", session.getAttributeNS(ns, "permission"))
        assertEquals(emptyList<String>(), actionsOf(session))
        val recognizer = element("service", ".LumiRecognitionService")
        assertEquals(listOf("android.speech.RecognitionService"), actionsOf(recognizer))
        // Never chosen as the TV's default speech recognizer by itself (only the assistant role points at it).
        val speech = recognizer.getElementsByTagName("meta-data").item(0) as Element
        assertEquals("android.speech", speech.getAttributeNS(ns, "name"))
        assertEquals("@xml/lumi_recognizer", speech.getAttributeNS(ns, "resource"))
        val rec = DocumentBuilderFactory.newInstance().apply { isNamespaceAware = true }.newDocumentBuilder()
            .parse(File("src/main/res/xml/lumi_recognizer.xml")).documentElement
        assertEquals("recognition-service", rec.tagName)
        assertEquals("false", rec.getAttributeNS(ns, "selectableAsDefault"))

        val xml = DocumentBuilderFactory.newInstance().apply { isNamespaceAware = true }.newDocumentBuilder()
            .parse(File("src/main/res/xml/lumi_voice.xml")).documentElement
        assertEquals("voice-interaction-service", xml.tagName)
        assertEquals("io.github.miladaslaner.lumitv.LumiSessionService", xml.getAttributeNS(ns, "sessionService"))
        assertEquals("io.github.miladaslaner.lumitv.LumiRecognitionService", xml.getAttributeNS(ns, "recognitionService"))
        assertEquals("true", xml.getAttributeNS(ns, "supportsAssist"))
    }
}
