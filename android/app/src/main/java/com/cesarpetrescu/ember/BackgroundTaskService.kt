package com.cesarpetrescu.ember

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder
import androidx.core.app.ServiceCompat

class BackgroundTaskService : Service() {
  override fun onBind(intent: Intent?): IBinder? = null

  override fun onCreate() {
    super.onCreate()
    ensureChannel()
  }

  override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
    if (intent?.action == ActionStop) {
      stopForeground(STOP_FOREGROUND_REMOVE)
      stopSelf()
      return START_NOT_STICKY
    }
    val reason = intent?.getStringExtra(ExtraReason) ?: "Keeping Ember connected"
    val notification = buildNotification(reason)
    ServiceCompat.startForeground(
      this,
      NotificationId,
      notification,
      ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC,
    )
    return START_NOT_STICKY
  }

  override fun onTimeout(startId: Int) {
    stopSelf(startId)
  }

  override fun onTimeout(startId: Int, fgsType: Int) {
    stopSelf(startId)
  }

  private fun ensureChannel() {
    val manager = getSystemService(NotificationManager::class.java)
    val channel =
      NotificationChannel(
        ChannelId,
        "Ember background work",
        NotificationManager.IMPORTANCE_LOW,
      ).apply {
        description = "Keeps sign-in and streaming responses active while Ember is in the background."
      }
    manager.createNotificationChannel(channel)
  }

  private fun buildNotification(reason: String): Notification {
    val launchIntent =
      PendingIntent.getActivity(
        this,
        0,
        Intent(this, MainActivity::class.java),
        PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
      )
    return Notification.Builder(this, ChannelId)
      .setSmallIcon(R.drawable.ic_stat_ember)
      .setContentTitle("Ember")
      .setContentText(reason)
      .setContentIntent(launchIntent)
      .setOngoing(true)
      .build()
  }

  companion object {
    private const val ChannelId = "ember_background"
    private const val NotificationId = 51
    private const val ActionStop = "com.cesarpetrescu.ember.STOP_BACKGROUND"
    private const val ExtraReason = "reason"

    fun start(context: Context, reason: String) {
      val intent =
        Intent(context, BackgroundTaskService::class.java)
          .putExtra(ExtraReason, reason)
      if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
        context.startForegroundService(intent)
      } else {
        context.startService(intent)
      }
    }

    fun stop(context: Context) {
      context.stopService(Intent(context, BackgroundTaskService::class.java))
    }
  }
}
