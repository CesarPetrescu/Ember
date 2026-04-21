package com.cesarpetrescu.ember.data

import kotlinx.serialization.Serializable
import java.util.Locale

const val SparkModel = "gpt-5.3-codex-spark"
const val DefaultModel = "gpt-5.4"
const val DefaultEffort = "medium"
const val BaselineTokens = 12_000

val ModelSpecs =
  linkedMapOf(
    "gpt-5.4" to listOf("low", "medium", "high", "xhigh"),
    "gpt-5.4-mini" to listOf("low", "medium", "high", "xhigh"),
    "gpt-5.3-codex" to listOf("low", "medium", "high", "xhigh"),
    SparkModel to listOf("low", "medium", "high", "xhigh"),
    "gpt-5.2" to listOf("low", "medium", "high", "xhigh"),
  )

val ContextWindows =
  mapOf(
    "gpt-5.4" to 272_000,
    "gpt-5.4-mini" to 272_000,
    "gpt-5.3-codex" to 272_000,
    SparkModel to 128_000,
    "gpt-5.2" to 272_000,
  )

val MaxContextWindows = mapOf("gpt-5.4" to 1_000_000)

fun formatTokenCount(n: Int): String =
  when {
    n >= 1_000_000 -> compactDecimal(n / 1_000_000.0, "M")
    n >= 1_000 -> compactDecimal(n / 1_000.0, "k")
    else -> n.toString()
  }

private fun compactDecimal(value: Double, suffix: String): String =
  String.format(Locale.US, "%.1f", value).trimEnd('0').trimEnd('.') + suffix

fun percentOfContextRemaining(totalTokens: Int, contextWindow: Int): Int {
  if (contextWindow <= BaselineTokens) return 0
  val effective = contextWindow - BaselineTokens
  val used = maxOf(0, totalTokens - BaselineTokens)
  val remaining = maxOf(0, effective - used)
  return ((remaining.toDouble() / effective.toDouble()) * 100.0).toInt().coerceIn(0, 100)
}

@Serializable
data class AuthTokens(
  val idToken: String,
  val accessToken: String,
  val refreshToken: String,
  val accountId: String? = null,
  val email: String? = null,
  val planType: String? = null,
  val lastRefresh: String,
)

data class Chat(
  val id: String,
  val title: String,
  val model: String,
  val effort: String,
  val createdAt: String,
  val updatedAt: String,
  val titleGenerated: Boolean,
  val lastTotalTokens: Int,
  val contextWindowOverride: Int?,
)

data class StoredMessage(
  val id: Long,
  val chatId: String,
  val role: String,
  val text: String,
  val createdAt: String,
)

data class DeviceCode(
  val verificationUrl: String,
  val userCode: String,
  val deviceAuthId: String,
  val intervalSeconds: Int,
)
