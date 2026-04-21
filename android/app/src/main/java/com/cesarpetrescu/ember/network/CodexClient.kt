package com.cesarpetrescu.ember.network

import android.util.Base64
import com.cesarpetrescu.ember.data.AuthTokens
import com.cesarpetrescu.ember.data.DeviceCode
import com.cesarpetrescu.ember.data.SparkModel
import com.cesarpetrescu.ember.data.nowIso
import java.io.IOException
import java.util.UUID
import java.util.concurrent.TimeUnit
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.ensureActive
import kotlinx.coroutines.withContext
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonArray
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.intOrNull
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import okhttp3.FormBody
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody

private const val ClientId = "app_EMoamEEZ73f0CkXaXp7hrann"
private const val Issuer = "https://auth.openai.com"
private const val DeviceUserCodeUrl = "$Issuer/api/accounts/deviceauth/usercode"
private const val DeviceTokenUrl = "$Issuer/api/accounts/deviceauth/token"
private const val OAuthTokenUrl = "$Issuer/oauth/token"
private const val DeviceRedirectUri = "$Issuer/deviceauth/callback"
private const val VerificationUrl = "$Issuer/codex/device"
private const val BackendResponsesUrl = "https://chatgpt.com/backend-api/codex/responses"
private const val Originator = "codex_cli_rs"
private const val CodexCliVersion = "0.105.0"
private const val UserAgent = "$Originator/$CodexCliVersion (Android) ember"

data class ResponseMessage(val role: String, val text: String)

interface StreamSink {
  fun onPhase(phase: String)
  fun onDelta(delta: String)
  fun onReasoning(delta: String)
  fun onReasoningStep(kind: String)
  fun onUsage(totalTokens: Int)
}

class CodexClient {
  private val json = Json { ignoreUnknownKeys = true }
  private val http =
    OkHttpClient.Builder()
      .retryOnConnectionFailure(true)
      .connectTimeout(60, TimeUnit.SECONDS)
      .writeTimeout(60, TimeUnit.SECONDS)
      .readTimeout(300, TimeUnit.SECONDS)
      .callTimeout(0, TimeUnit.SECONDS)
      .build()
  private val jsonType = "application/json".toMediaType()

  suspend fun requestDeviceCode(): DeviceCode =
    withContext(Dispatchers.IO) {
      val result = postJson(DeviceUserCodeUrl, buildJsonObject { put("client_id", JsonPrimitive(ClientId)) })
      if (result.status != 200) throw IOException("device code request failed [${result.status}]: ${result.body.take(300)}")
      val obj = json.parseToJsonElement(result.body).jsonObject
      DeviceCode(
        verificationUrl = VerificationUrl,
        userCode = obj.string("user_code") ?: error("missing user_code"),
        deviceAuthId = obj.string("device_auth_id") ?: error("missing device_auth_id"),
        intervalSeconds = obj.intish("interval") ?: 5,
      )
    }

  suspend fun pollDeviceAuthorization(
    deviceCode: DeviceCode,
    isCancelled: () -> Boolean,
    onTransientError: (String) -> Unit = {},
  ): AuthTokens =
    withContext(Dispatchers.IO) {
      val deadline = System.currentTimeMillis() + 15 * 60 * 1000
      var authCode: String? = null
      var codeVerifier: String? = null

      while (authCode == null) {
        ensureActive()
        if (isCancelled()) throw IOException("device auth cancelled")
        if (System.currentTimeMillis() > deadline) throw IOException("device auth timed out after 15 minutes")

        val result =
          try {
            postJson(
              DeviceTokenUrl,
              buildJsonObject {
                put("device_auth_id", JsonPrimitive(deviceCode.deviceAuthId))
                put("user_code", JsonPrimitive(deviceCode.userCode))
              },
            )
          } catch (e: IOException) {
            onTransientError("Network issue while checking approval; retrying...")
            delay(deviceCode.intervalSeconds * 1000L)
            continue
          }

        if (result.status == 200) {
          val obj = json.parseToJsonElement(result.body).jsonObject
          authCode = obj.string("authorization_code")
          codeVerifier = obj.string("code_verifier")
        } else if (result.status == 403 || result.status == 404) {
          delay(deviceCode.intervalSeconds * 1000L)
        } else {
          throw IOException("device auth failed [${result.status}]: ${result.body.take(300)}")
        }
      }

      var tokenResult: HttpResult? = null
      while (tokenResult == null) {
        ensureActive()
        if (isCancelled()) throw IOException("device auth cancelled")
        if (System.currentTimeMillis() > deadline) throw IOException("device auth timed out after 15 minutes")
        tokenResult =
          try {
            postForm(
              OAuthTokenUrl,
              mapOf(
                "grant_type" to "authorization_code",
                "code" to authCode,
                "redirect_uri" to DeviceRedirectUri,
                "client_id" to ClientId,
                "code_verifier" to codeVerifier.orEmpty(),
              ),
            )
          } catch (e: IOException) {
            onTransientError("Network issue while finishing sign-in; retrying...")
            delay(deviceCode.intervalSeconds * 1000L)
            null
          }
      }
      if (tokenResult.status != 200) {
        throw IOException("token exchange failed [${tokenResult.status}]: ${tokenResult.body.take(300)}")
      }
      tokensFromOAuthResponse(tokenResult.body, null)
    }

  suspend fun refreshTokens(tokens: AuthTokens): AuthTokens =
    withContext(Dispatchers.IO) {
      val result =
        postJson(
          OAuthTokenUrl,
          buildJsonObject {
            put("client_id", JsonPrimitive(ClientId))
            put("grant_type", JsonPrimitive("refresh_token"))
            put("refresh_token", JsonPrimitive(tokens.refreshToken))
          },
        )
      if (result.status != 200) throw IOException("refresh failed [${result.status}]: ${result.body.take(300)}")
      tokensFromOAuthResponse(result.body, tokens)
    }

  suspend fun streamChat(
    tokens: AuthTokens,
    refreshAndSave: suspend (AuthTokens) -> AuthTokens,
    history: List<ResponseMessage>,
    model: String,
    effort: String,
    sessionId: String,
    sink: StreamSink,
  ): AuthTokens =
    withContext(Dispatchers.IO) {
      var currentTokens = tokens
      val payload = buildResponsesRequest(history, model, effort, stream = true)

      repeat(2) { attempt ->
        ensureActive()
        val response = http.newCall(streamRequest(payload, currentTokens, sessionId)).execute()
        response.use { resp ->
          if (resp.code == 401 && attempt == 0) {
            currentTokens = refreshAndSave(currentTokens)
            return@repeat
          }
          if (!resp.isSuccessful) {
            throw IOException("HTTP ${resp.code}: ${resp.body.string().take(800)}")
          }

          var gotAnyDelta = false
          var fallbackText = ""
          val source = resp.body.source()
          while (true) {
            ensureActive()
            val line = source.readUtf8Line() ?: break
            if (!line.startsWith("data:")) continue
            val raw = line.removePrefix("data:").trim()
            if (raw.isEmpty() || raw == "[DONE]") continue
            val event = runCatching { json.parseToJsonElement(raw).jsonObject }.getOrNull() ?: continue
            when (event.string("type")) {
              "response.created" -> sink.onPhase("thinking")
              "response.output_item.added" -> {
                val item = event["item"]?.jsonObjectOrNull()
                when (item?.string("type")) {
                  "reasoning" -> sink.onPhase("reasoning")
                  "message" -> sink.onPhase("writing")
                }
              }
              "response.output_text.delta" -> {
                val delta = event.string("delta").orEmpty()
                if (delta.isNotEmpty()) {
                  gotAnyDelta = true
                  sink.onDelta(delta)
                }
              }
              "response.reasoning_summary_text.delta",
              "response.reasoning_text.delta" -> {
                event.string("delta")?.takeIf { it.isNotEmpty() }?.let(sink::onReasoning)
              }
              "response.reasoning_summary_part.added" -> sink.onReasoningStep("start")
              "response.reasoning_summary_part.done" -> sink.onReasoningStep("end")
              "response.reasoning_summary_text.done" -> sink.onReasoningStep("text_done")
              "response.output_item.done" -> {
                val item = event["item"]?.jsonObjectOrNull()
                if (item?.string("type") == "message") fallbackText += extractTextFromItem(item)
              }
              "response.completed" -> {
                if (!gotAnyDelta && fallbackText.isNotEmpty()) {
                  gotAnyDelta = true
                  sink.onDelta(fallbackText)
                }
                event["response"]
                  ?.jsonObjectOrNull()
                  ?.get("usage")
                  ?.jsonObjectOrNull()
                  ?.intish("total_tokens")
                  ?.let(sink::onUsage)
              }
              "response.failed", "response.incomplete" -> throw IOException(formatStreamError(event))
              "response.error", "error" -> throw IOException("server error: ${event["error"] ?: event.string("message") ?: raw}")
            }
          }
          if (!gotAnyDelta) throw IOException("no output produced (empty stream)")
          return@withContext currentTokens
        }
      }
      currentTokens
    }

  suspend fun generateTitle(tokens: AuthTokens, userText: String, assistantText: String): String =
    withContext(Dispatchers.IO) {
      val titleHistory =
        listOf(
          ResponseMessage("user", userText.take(2_000)),
          ResponseMessage("assistant", assistantText.take(2_000)),
          ResponseMessage("user", "Title this chat in 1-3 words."),
        )
      val payload =
        buildResponsesRequest(titleHistory, SparkModel, "low", stream = true).toMutableMap().apply {
          this["instructions"] =
            JsonPrimitive(
              "You generate short chat titles. Reply with 1 to 3 words. Title Case. No punctuation, quotes, or explanation.",
            )
        }
      val response = http.newCall(streamRequest(JsonObject(payload), tokens, UUID.randomUUID().toString())).execute()
      response.use { resp ->
        if (!resp.isSuccessful) return@withContext ""
        val out = StringBuilder()
        val source = resp.body.source()
        while (true) {
          ensureActive()
          val line = source.readUtf8Line() ?: break
          if (!line.startsWith("data:")) continue
          val raw = line.removePrefix("data:").trim()
          if (raw.isEmpty() || raw == "[DONE]") continue
          val event = runCatching { json.parseToJsonElement(raw).jsonObject }.getOrNull() ?: continue
          if (event.string("type") == "response.output_text.delta") out.append(event.string("delta").orEmpty())
        }
        out.toString().lineSequence().firstOrNull().orEmpty().trim().trim('"', '\'', '`', '.', ',', ':', ';', '!', '?')
          .split(Regex("\\s+"))
          .filter { it.isNotBlank() }
          .take(3)
          .joinToString(" ")
      }
    }

  private fun buildResponsesRequest(
    history: List<ResponseMessage>,
    model: String,
    effort: String,
    stream: Boolean,
  ): JsonObject {
    val isSpark = model == SparkModel
    return buildJsonObject {
      put("model", JsonPrimitive(model))
      put("input", buildJsonArray { history.forEach { add(it.toResponsesJson()) } })
      put("stream", JsonPrimitive(stream))
      put("store", JsonPrimitive(false))
      put(
        "reasoning",
        buildJsonObject {
          put("effort", JsonPrimitive(effort))
          if (!isSpark) put("summary", JsonPrimitive("auto"))
        },
      )
      put(
        "instructions",
        JsonPrimitive("You are a helpful, concise conversational assistant. Answer directly. Do not use tools or file operations."),
      )
      if (!isSpark) put("include", JsonArray(listOf(JsonPrimitive("reasoning.encrypted_content"))))
    }
  }

  private fun ResponseMessage.toResponsesJson(): JsonObject =
    buildJsonObject {
      put("role", JsonPrimitive(role))
      put(
        "content",
        JsonArray(
          listOf(
            buildJsonObject {
              put("type", JsonPrimitive(if (role == "assistant") "output_text" else "input_text"))
              put("text", JsonPrimitive(text))
            },
          ),
        ),
      )
    }

  private fun streamRequest(payload: JsonObject, tokens: AuthTokens, sessionId: String): Request =
    Request.Builder()
      .url(BackendResponsesUrl)
      .headersFor(tokens, sessionId)
      .header("Content-Type", "application/json")
      .header("Accept", "text/event-stream")
      .header("Cache-Control", "no-cache")
      .post(payload.toString().toRequestBody(jsonType))
      .build()

  private fun Request.Builder.headersFor(tokens: AuthTokens, sessionId: String): Request.Builder =
    apply {
      header("User-Agent", UserAgent)
      header("originator", Originator)
      header("Authorization", "Bearer ${tokens.accessToken}")
      header("OpenAI-Beta", "responses=experimental")
      header("version", CodexCliVersion)
      header("session_id", sessionId)
      tokens.accountId?.let { header("chatgpt-account-id", it) }
    }

  private fun postJson(url: String, payload: JsonObject): HttpResult {
    val req =
      Request.Builder()
        .url(url)
        .header("User-Agent", UserAgent)
        .header("originator", Originator)
        .header("Accept", "application/json")
        .post(payload.toString().toRequestBody(jsonType))
        .build()
    return http.newCall(req).execute().use { HttpResult(it.code, it.body.string()) }
  }

  private fun postForm(url: String, form: Map<String, String>): HttpResult {
    val body = FormBody.Builder().apply { form.forEach { (k, v) -> add(k, v) } }.build()
    val req =
      Request.Builder()
        .url(url)
        .header("User-Agent", UserAgent)
        .header("originator", Originator)
        .header("Accept", "application/json")
        .post(body)
        .build()
    return http.newCall(req).execute().use { HttpResult(it.code, it.body.string()) }
  }

  private fun tokensFromOAuthResponse(body: String, previous: AuthTokens?): AuthTokens {
    val obj = json.parseToJsonElement(body).jsonObject
    val idToken = obj.string("id_token") ?: previous?.idToken.orEmpty()
    val claims = parseIdTokenInfo(idToken)
    return AuthTokens(
      idToken = idToken,
      accessToken = obj.string("access_token") ?: previous?.accessToken.orEmpty(),
      refreshToken = obj.string("refresh_token") ?: previous?.refreshToken.orEmpty(),
      accountId = claims.accountId ?: previous?.accountId,
      email = claims.email ?: previous?.email,
      planType = claims.planType ?: previous?.planType,
      lastRefresh = nowIso(),
    )
  }

  private fun parseIdTokenInfo(idToken: String): ClaimInfo {
    val payload = idToken.split(".").getOrNull(1) ?: return ClaimInfo()
    return runCatching {
      val padded = payload + "=".repeat((4 - payload.length % 4) % 4)
      val decoded = String(Base64.decode(padded, Base64.URL_SAFE or Base64.NO_WRAP), Charsets.UTF_8)
      val claims = json.parseToJsonElement(decoded).jsonObject
      val auth = claims["https://api.openai.com/auth"]?.jsonObjectOrNull()
      val profile = claims["https://api.openai.com/profile"]?.jsonObjectOrNull()
      ClaimInfo(
        accountId = auth?.string("chatgpt_account_id"),
        email = claims.string("email") ?: profile?.string("email"),
        planType = auth?.string("chatgpt_plan_type"),
      )
    }.getOrDefault(ClaimInfo())
  }

  private fun extractTextFromItem(item: JsonObject): String =
    item["content"]
      ?.jsonArrayOrNull()
      ?.mapNotNull { piece ->
        piece.jsonObjectOrNull()?.takeIf { it.string("type") in setOf("output_text", "text") }?.string("text")
      }
      ?.joinToString("")
      .orEmpty()

  private fun formatStreamError(event: JsonObject): String {
    val response = event["response"]?.jsonObjectOrNull()
    val error = response?.get("error")?.jsonObjectOrNull()
    if (error != null) return error.string("message") ?: error.string("code") ?: error.toString()
    val incomplete = response?.get("incomplete_details")?.jsonObjectOrNull()
    if (incomplete != null) return "incomplete: ${incomplete.string("reason") ?: "unknown"}"
    return event.string("type") ?: "stream error"
  }

  private data class HttpResult(val status: Int, val body: String)

  private data class ClaimInfo(val accountId: String? = null, val email: String? = null, val planType: String? = null)
}

private fun JsonObject.string(key: String): String? = this[key]?.jsonPrimitiveOrNull()?.contentOrNull

private fun JsonObject.intish(key: String): Int? = this[key]?.jsonPrimitiveOrNull()?.intOrNull ?: string(key)?.toIntOrNull()

private fun JsonElement.jsonPrimitiveOrNull() = this as? JsonPrimitive

private fun JsonElement.jsonObjectOrNull() = this as? JsonObject

private fun JsonElement.jsonArrayOrNull() = this as? JsonArray
