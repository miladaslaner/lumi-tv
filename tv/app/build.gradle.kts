import java.util.Properties

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("org.jetbrains.kotlin.plugin.compose")
}

// Release signing: a `keystore.properties` next to this project (storeFile, storePassword, keyAlias, keyPassword) or
// the LUMI_KEYSTORE / LUMI_KEYSTORE_PASSWORD / LUMI_KEY_ALIAS / LUMI_KEY_PASSWORD environment; neither is committed.
// Without either, the release is signed with this machine's debug key (Android upgrades an app in place only under
// the key it was installed with; moving to a real key means one reinstall). See README › Release signing.
val keystoreProperties = rootProject.file("keystore.properties").takeIf { it.isFile }?.let { f ->
    Properties().apply { f.inputStream().use { load(it) } }
}
fun signing(property: String, env: String): String? = keystoreProperties?.getProperty(property) ?: System.getenv(env)
val releaseStore = signing("storeFile", "LUMI_KEYSTORE")

android {
    namespace = "io.github.miladaslaner.lumitv"
    compileSdk = 34
    defaultConfig {
        applicationId = "io.github.miladaslaner.lumitv"
        minSdk = 30
        targetSdk = 34
        versionCode = 3
        versionName = "1.0.0"
    }
    signingConfigs {
        if (releaseStore != null) {
            create("release") {
                storeFile = rootProject.file(releaseStore)
                storePassword = signing("storePassword", "LUMI_KEYSTORE_PASSWORD")
                keyAlias = signing("keyAlias", "LUMI_KEY_ALIAS")
                keyPassword = signing("keyPassword", "LUMI_KEY_PASSWORD")
            }
        }
    }
    buildTypes {
        // What goes on the family TV: not debuggable (no run-as, no debugger attach), shrunk and obfuscated by R8.
        release {
            isMinifyEnabled = true
            isShrinkResources = true
            proguardFiles(getDefaultProguardFile("proguard-android-optimize.txt"), "proguard-rules.pro")
            signingConfig = if (releaseStore != null) {
                signingConfigs.getByName("release")
            } else {
                logger.warn("release signed with the debug key; see README › Release signing")
                signingConfigs.getByName("debug")
            }
        }
    }
    buildFeatures { compose = true }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions { jvmTarget = "17" }
    testOptions {
        // A plain JVM test that reaches an Android method fails loudly instead of silently getting 0/null/false.
        unitTests.isReturnDefaultValues = false
        unitTests.isIncludeAndroidResources = true
    }
    lint {
        abortOnError = true
        warningsAsErrors = false
        // A TV app ships one banner (xhdpi, 320x180 dp) and one icon; phone density folders are not wanted.
        disable += listOf("IconDensities", "IconMissingDensityFolder")
    }
}

dependencies {
    // Compose foundation only: focus is driven by onPreviewKeyEvent (EditorialScreen), no Material or TV Material.
    implementation(platform("androidx.compose:compose-bom:2024.09.00"))
    implementation("androidx.compose.ui:ui")
    implementation("androidx.compose.foundation:foundation")
    implementation("androidx.activity:activity-compose:1.9.2")
    implementation("com.squareup.okhttp3:okhttp:4.12.0")
    implementation("io.coil-kt:coil:2.7.0")
    implementation("io.coil-kt:coil-compose:2.7.0")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.11.0")

    testImplementation("junit:junit:4.13.2")
    testImplementation("org.json:json:20260814")
    testImplementation("org.robolectric:robolectric:4.13")
    testImplementation("com.squareup.okhttp3:mockwebserver:4.12.0")
}
