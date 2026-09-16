package com.koushik.anvi

import android.content.Intent
import android.net.Uri
import android.os.Build
import android.provider.OpenableColumns
import io.flutter.embedding.android.FlutterActivity
import io.flutter.embedding.engine.FlutterEngine
import io.flutter.plugin.common.MethodChannel
import java.io.File

/** Receives things shared to Karen from other apps (text, links, documents). */
class MainActivity : FlutterActivity() {
    private var channel: MethodChannel? = null
    private var pending: Map<String, Any?>? = null

    override fun configureFlutterEngine(flutterEngine: FlutterEngine) {
        super.configureFlutterEngine(flutterEngine)
        channel = MethodChannel(flutterEngine.dartExecutor.binaryMessenger, "karen/share").also {
            it.setMethodCallHandler { call, result ->
                if (call.method == "takeShared") {
                    result.success(pending)
                    pending = null
                } else {
                    result.notImplemented()
                }
            }
        }
        pending = readShare(intent)
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
