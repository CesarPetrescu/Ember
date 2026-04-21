package com.cesarpetrescu.ember.data

import android.content.Context
import android.content.SharedPreferences
import androidx.security.crypto.EncryptedSharedPreferences
import androidx.security.crypto.MasterKey
import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.Json

class TokenStore(context: Context) {
  private val prefs: SharedPreferences =
    try {
      val masterKey =
        MasterKey.Builder(context)
          .setKeyScheme(MasterKey.KeyScheme.AES256_GCM)
          .build()
      EncryptedSharedPreferences.create(
        context,
        "ember_tokens",
        masterKey,
        EncryptedSharedPreferences.PrefKeyEncryptionScheme.AES256_SIV,
        EncryptedSharedPreferences.PrefValueEncryptionScheme.AES256_GCM,
      )
    } catch (_: Throwable) {
      context.getSharedPreferences("ember_tokens_fallback", Context.MODE_PRIVATE)
    }

  fun load(): AuthTokens? =
    prefs.getString("tokens", null)?.let { raw ->
      runCatching { Json.decodeFromString<AuthTokens>(raw) }.getOrNull()
    }

  fun save(tokens: AuthTokens) {
    prefs.edit().putString("tokens", Json.encodeToString(tokens)).apply()
  }

  fun clear() {
    prefs.edit().clear().apply()
  }
}
