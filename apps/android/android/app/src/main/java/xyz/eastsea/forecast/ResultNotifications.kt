package xyz.eastsea.forecast

import android.Manifest
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.util.Log
import android.webkit.CookieManager
import androidx.core.app.ActivityCompat
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import androidx.work.Constraints
import androidx.work.CoroutineWorker
import androidx.work.ExistingPeriodicWorkPolicy
import androidx.work.ExistingWorkPolicy
import androidx.work.NetworkType
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.PeriodicWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.WorkerParameters
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL
import java.util.concurrent.TimeUnit

/**
 * Result notifications without a push service.
 *
 * The site already keeps a per-account activity feed (finalized outcomes, followed
 * creators publishing). A periodic WorkManager job reads that feed with the WebView's
 * own session cookie and raises a local notification for each unread item it has not
 * shown before. Tapping a notification opens the forecast through the app link. No
 * token, key or account identifier leaves the device; the job only calls the same
 * origin the page uses.
 */
object ResultNotifications {
    const val ORIGIN = "https://forecast.eastsea.xyz"
    const val CHANNEL_ID = "results"
    const val WORK_NAME = "forecast-activity-check"
    private const val PREFS = "forecast_notifications"
    private const val PREF_SEEN = "seen_activity_ids"
    private const val MAX_SEEN = 200
    private const val TAG = "ForecastNotify"

    private val online = Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build()

    fun schedule(context: Context) {
        val request = PeriodicWorkRequestBuilder<ActivityWorker>(30, TimeUnit.MINUTES)
            .setConstraints(online)
            .build()
        WorkManager.getInstance(context)
            .enqueueUniquePeriodicWork(WORK_NAME, ExistingPeriodicWorkPolicy.KEEP, request)
    }

    /** One immediate check, used when the app leaves the foreground; the periodic job never runs early. */
    fun checkNow(context: Context) {
        val request = OneTimeWorkRequestBuilder<ActivityWorker>().setConstraints(online).build()
        WorkManager.getInstance(context).enqueueUniqueWork("$WORK_NAME-now", ExistingWorkPolicy.REPLACE, request)
    }

    fun requestPermission(activity: android.app.Activity) {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU) return
        val granted = ActivityCompat.checkSelfPermission(activity, Manifest.permission.POST_NOTIFICATIONS)
        if (granted != PackageManager.PERMISSION_GRANTED) {
            ActivityCompat.requestPermissions(activity, arrayOf(Manifest.permission.POST_NOTIFICATIONS), 41)
        }
    }

    fun ensureChannel(context: Context) {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return
        val manager = context.getSystemService(NotificationManager::class.java)
        if (manager.getNotificationChannel(CHANNEL_ID) != null) return
        manager.createNotificationChannel(
            NotificationChannel(CHANNEL_ID, context.getString(R.string.channel_results), NotificationManager.IMPORTANCE_DEFAULT)
                .apply { description = context.getString(R.string.channel_results_description) },
        )
    }

    data class Item(val id: String, val forecastId: String?, val kind: String, val title: String, val body: String)

    /** Pure parsing so the feed shape stays testable: only unread items are candidates. */
    fun unreadItems(payload: String): List<Item> {
        val items = JSONObject(payload).optJSONObject("data")?.optJSONArray("items") ?: return emptyList()
        return (0 until items.length()).mapNotNull { index ->
            val item = items.optJSONObject(index) ?: return@mapNotNull null
            if (!item.isNull("readAt")) return@mapNotNull null
            val id = item.optString("id"); if (id.isEmpty()) return@mapNotNull null
            Item(
                id = id,
                forecastId = item.optString("forecastId").ifEmpty { null },
                kind = item.optString("kind"),
                title = item.optString("title"),
                body = item.optString("body"),
            )
        }
    }

    fun seen(context: Context): Set<String> =
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE).getStringSet(PREF_SEEN, emptySet()) ?: emptySet()

    fun remember(context: Context, ids: Collection<String>) {
        val merged = (seen(context) + ids).toList().takeLast(MAX_SEEN).toSet()
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit().putStringSet(PREF_SEEN, merged).apply()
    }

    fun show(context: Context, item: Item) {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU &&
            ActivityCompat.checkSelfPermission(context, Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED
        ) return
        ensureChannel(context)
        val target = if (item.forecastId != null) "$ORIGIN/forecasts/${Uri.encode(item.forecastId)}" else "$ORIGIN/activity"
        val open = Intent(Intent.ACTION_VIEW, Uri.parse(target), context, MainActivity::class.java)
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_SINGLE_TOP)
        val pending = PendingIntent.getActivity(
            context, item.id.hashCode(), open, PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
        val notification = NotificationCompat.Builder(context, CHANNEL_ID)
            .setSmallIcon(R.drawable.ic_stat_forecast)
            .setContentTitle(item.title.ifEmpty { context.getString(R.string.app_name) })
            .setContentText(item.body)
            .setStyle(NotificationCompat.BigTextStyle().bigText(item.body))
            .setContentIntent(pending)
            .setAutoCancel(true)
            .build()
        NotificationManagerCompat.from(context).notify(item.id.hashCode(), notification)
    }

    class ActivityWorker(context: Context, params: WorkerParameters) : CoroutineWorker(context, params) {
        override suspend fun doWork(): Result = withContext(Dispatchers.IO) {
            val cookie = CookieManager.getInstance().getCookie(ORIGIN)
            if (cookie.isNullOrEmpty() || !cookie.contains("__Host-forecast_session=")) {
                Log.i(TAG, "no session cookie; skipping activity check")
                return@withContext Result.success()
            }
            val payload = try {
                fetchActivity(cookie)
            } catch (e: Exception) {
                Log.w(TAG, "activity check failed: $e")
                return@withContext Result.retry()
            }
            val already = seen(applicationContext)
            val fresh = unreadItems(payload).filter { it.id !in already }
            Log.i(TAG, "activity check: ${fresh.size} new unread item(s)")
            fresh.forEach { show(applicationContext, it) }
            if (fresh.isNotEmpty()) remember(applicationContext, fresh.map { it.id })
            Result.success()
        }

        private fun fetchActivity(cookie: String): String {
            val connection = (URL("$ORIGIN/api/activity").openConnection() as HttpURLConnection).apply {
                requestMethod = "GET"; connectTimeout = 15_000; readTimeout = 20_000
                setRequestProperty("Cookie", cookie)
                setRequestProperty("Accept", "application/json")
                setRequestProperty("User-Agent", "ForecastAndroid/${BuildConfig.VERSION_NAME}")
            }
            try {
                if (connection.responseCode != 200) throw IllegalStateException("HTTP ${connection.responseCode}")
                return connection.inputStream.bufferedReader().use { it.readText() }
            } finally {
                connection.disconnect()
            }
        }
    }
}
