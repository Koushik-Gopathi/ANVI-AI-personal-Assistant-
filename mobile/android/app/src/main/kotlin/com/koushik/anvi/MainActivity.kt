package com.koushik.anvi

import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.provider.OpenableColumns
import io.flutter.embedding.android.FlutterActivity
import io.flutter.embedding.engine.FlutterEngine
import io.flutter.embedding.engine.FlutterEngineCache
import io.flutter.embedding.engine.dart.DartExecutor
import io.flutter.plugin.common.MethodChannel
import java.io.File

/**
 * Karen's screen. Her brain (the Flutter engine running the assistant) is kept in a cache instead of
 * belonging to this window, so with background listening on she keeps listening after the app is
 * swiped away from Recents: the foreground service keeps the process alive, and reopening the app
 * reconnects to the same running Karen. Also receives things shared to Karen from other apps.
 */
class MainActivity : FlutterActivity() {
    companion object {
        private const val ENGINE_ID = "karen"

        /** Set from Dart: true while background listening is on. */
        @Volatile
        var keepAlive = false
    }

    private var channel: MethodChannel? = null
    private var pending: Map<String, Any?>? = null
    private var reusedEngine = false

    override fun provideFlutterEngine(context: Context): FlutterEngine {
        FlutterEngineCache.getInstance().get(ENGINE_ID)?.let {
            reusedEngine = true
            return it
        }
        val engine = FlutterEngine(context.applicationContext)
        engine.dartExecutor.executeDartEntrypoint(DartExecutor.DartEntrypoint.createDefault())
        FlutterEngineCache.getInstance().put(ENGINE_ID, engine)
        return engine
    }

    override fun shouldDestroyEngineWithHost(): Boolean = !keepAlive

    override fun onDestroy() {
        if (!keepAlive) FlutterEngineCache.getInstance().remove(ENGINE_ID)
        super.onDestroy()
    }

    override fun configureFlutterEngine(flutterEngine: FlutterEngine) {
        super.configureFlutterEngine(flutterEngine)
        channel = MethodChannel(flutterEngine.dartExecutor.binaryMessenger, "karen/share").also {
            it.setMethodCallHandler { call, result ->
                when (call.method) {
                    "takeShared" -> {
                        result.success(pending)
                        pending = null
                    }
                    "keepAlive" -> {
                        keepAlive = call.arguments == true
                        result.success(null)
                    }
                    else -> result.notImplemented()
                }
            }
        }
        val shared = readShare(intent)
        if (reusedEngine && shared != null) {
            channel?.invokeMethod("shared", shared) // Karen is already running: hand it over now
        } else {
            pending = shared
        }
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        val shared = readShare(intent) ?: return
        pending = null
        channel?.invokeMethod("shared", shared)
    }

    private fun readShare(intent: Intent?): Map<String, Any?>? {
        if (intent == null || (intent.action != Intent.ACTION_SEND && intent.action != Intent.ACTION_SEND_MULTIPLE)) {
            return null
        }
        val text = intent.getStringExtra(Intent.EXTRA_TEXT)
        val subject = intent.getStringExtra(Intent.EXTRA_SUBJECT)
        val uris = mutableListOf<Uri>()
        if (intent.action == Intent.ACTION_SEND) {
            streamExtra(intent)?.let { uris.add(it) }
        } else {
            streamListExtra(intent)?.let { uris.addAll(it) }
        }
        val files = uris.take(5).mapNotNull { copyToCache(it) }
        intent.action = null // don't handle the same share twice
        if (text.isNullOrBlank() && files.isEmpty()) return null
        return mapOf("text" to text, "subject" to subject, "files" to files)
    }

    @Suppress("DEPRECATION")
    private fun streamExtra(intent: Intent): Uri? =
        if (Build.VERSION.SDK_INT >= 33) intent.getParcelableExtra(Intent.EXTRA_STREAM, Uri::class.java)
        else intent.getParcelableExtra(Intent.EXTRA_STREAM)

    @Suppress("DEPRECATION")
    private fun streamListExtra(intent: Intent): List<Uri>? =
        if (Build.VERSION.SDK_INT >= 33) intent.getParcelableArrayListExtra(Intent.EXTRA_STREAM, Uri::class.java)
        else intent.getParcelableArrayListExtra(Intent.EXTRA_STREAM)

    private fun copyToCache(uri: Uri): Map<String, Any?>? = try {
        var name = "shared"
        contentResolver.query(uri, arrayOf(OpenableColumns.DISPLAY_NAME), null, null, null)?.use { c ->
            if (c.moveToFirst() && !c.isNull(0)) name = c.getString(0)
        }
        val dir = File(cacheDir, "shared").apply { mkdirs() }
        val out = File(dir, name.replace(Regex("[\\\\/:*?\"<>|]"), "_"))
        contentResolver.openInputStream(uri)?.use { input -> out.outputStream().use { input.copyTo(it) } }
        mapOf("path" to out.absolutePath, "name" to name, "mime" to contentResolver.getType(uri))
    } catch (e: Exception) {
        null
    }
}
