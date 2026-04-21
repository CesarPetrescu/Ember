package com.cesarpetrescu.ember.data

import android.content.ContentValues
import android.content.Context
import android.database.Cursor
import android.database.sqlite.SQLiteDatabase
import android.database.sqlite.SQLiteOpenHelper
import java.time.Instant
import java.util.UUID

class ChatStore(context: Context) : SQLiteOpenHelper(context, "ember.sqlite3", null, 1) {
  override fun onCreate(db: SQLiteDatabase) {
    db.execSQL(
      """
      CREATE TABLE IF NOT EXISTS chats (
        id TEXT PRIMARY KEY,
        title TEXT NOT NULL,
        model TEXT NOT NULL,
        effort TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        title_generated INTEGER NOT NULL DEFAULT 0,
        last_total_tokens INTEGER NOT NULL DEFAULT 0,
        context_window_override INTEGER
      )
      """.trimIndent(),
    )
    db.execSQL(
      """
      CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id TEXT NOT NULL,
        role TEXT NOT NULL,
        content TEXT NOT NULL,
        created_at TEXT NOT NULL,
        FOREIGN KEY(chat_id) REFERENCES chats(id) ON DELETE CASCADE
      )
      """.trimIndent(),
    )
    db.execSQL("CREATE INDEX IF NOT EXISTS idx_messages_chat ON messages(chat_id, id)")
  }

  override fun onUpgrade(db: SQLiteDatabase, oldVersion: Int, newVersion: Int) = onCreate(db)

  override fun onConfigure(db: SQLiteDatabase) {
    super.onConfigure(db)
    db.setForeignKeyConstraintsEnabled(true)
  }

  fun createChat(model: String, effort: String, title: String = "New chat"): Chat {
    val now = nowIso()
    val chat =
      Chat(
        id = UUID.randomUUID().toString(),
        title = title,
        model = model,
        effort = effort,
        createdAt = now,
        updatedAt = now,
        titleGenerated = false,
        lastTotalTokens = 0,
        contextWindowOverride = null,
      )
    writableDatabase.insertOrThrow("chats", null, chatValues(chat))
    return chat
  }

  fun listChats(): List<Chat> =
    readableDatabase
      .rawQuery(
        """
        SELECT id, title, model, effort, created_at, updated_at, title_generated,
               last_total_tokens, context_window_override
        FROM chats ORDER BY updated_at DESC
        """.trimIndent(),
        emptyArray(),
      )
      .useRows { cursor -> buildList { while (cursor.moveToNext()) add(cursor.toChat()) } }

  fun getChat(chatId: String): Chat? =
    readableDatabase
      .rawQuery(
        """
        SELECT id, title, model, effort, created_at, updated_at, title_generated,
               last_total_tokens, context_window_override
        FROM chats WHERE id = ?
        """.trimIndent(),
        arrayOf(chatId),
      )
      .useRows { cursor -> if (cursor.moveToFirst()) cursor.toChat() else null }

  fun updateChatModel(chatId: String, model: String, effort: String) {
    writableDatabase.update(
      "chats",
      ContentValues().apply {
        put("model", model)
        put("effort", effort)
        put("updated_at", nowIso())
      },
      "id = ?",
      arrayOf(chatId),
    )
  }

  fun updateUsage(chatId: String, totalTokens: Int) {
    writableDatabase.update(
      "chats",
      ContentValues().apply { put("last_total_tokens", totalTokens) },
      "id = ?",
      arrayOf(chatId),
    )
  }

  fun updateContextOverride(chatId: String, value: Int?) {
    writableDatabase.update(
      "chats",
      ContentValues().apply {
        if (value == null) putNull("context_window_override") else put("context_window_override", value)
      },
      "id = ?",
      arrayOf(chatId),
    )
  }

  fun renameChat(chatId: String, title: String, generated: Boolean) {
    writableDatabase.update(
      "chats",
      ContentValues().apply {
        put("title", title.ifBlank { "New chat" })
        put("title_generated", if (generated) 1 else 0)
        put("updated_at", nowIso())
      },
      "id = ?",
      arrayOf(chatId),
    )
  }

  fun touchChat(chatId: String) {
    writableDatabase.update(
      "chats",
      ContentValues().apply { put("updated_at", nowIso()) },
      "id = ?",
      arrayOf(chatId),
    )
  }

  fun deleteChat(chatId: String) {
    writableDatabase.delete("chats", "id = ?", arrayOf(chatId))
  }

  fun saveMessage(chatId: String, role: String, text: String) {
    writableDatabase.insertOrThrow(
      "messages",
      null,
      ContentValues().apply {
        put("chat_id", chatId)
        put("role", role)
        put("content", text)
        put("created_at", nowIso())
      },
    )
  }

  fun loadMessages(chatId: String): List<StoredMessage> =
    readableDatabase
      .rawQuery(
        "SELECT id, chat_id, role, content, created_at FROM messages WHERE chat_id = ? ORDER BY id",
        arrayOf(chatId),
      )
      .useRows { cursor -> buildList { while (cursor.moveToNext()) add(cursor.toStoredMessage()) } }

  private fun chatValues(chat: Chat) =
    ContentValues().apply {
      put("id", chat.id)
      put("title", chat.title)
      put("model", chat.model)
      put("effort", chat.effort)
      put("created_at", chat.createdAt)
      put("updated_at", chat.updatedAt)
      put("title_generated", if (chat.titleGenerated) 1 else 0)
      put("last_total_tokens", chat.lastTotalTokens)
      if (chat.contextWindowOverride == null) putNull("context_window_override") else put("context_window_override", chat.contextWindowOverride)
    }

  private fun Cursor.toChat() =
    Chat(
      id = getString(0),
      title = getString(1),
      model = getString(2),
      effort = getString(3),
      createdAt = getString(4),
      updatedAt = getString(5),
      titleGenerated = getInt(6) == 1,
      lastTotalTokens = getInt(7),
      contextWindowOverride = if (isNull(8)) null else getInt(8),
    )

  private fun Cursor.toStoredMessage() =
    StoredMessage(
      id = getLong(0),
      chatId = getString(1),
      role = getString(2),
      text = getString(3),
      createdAt = getString(4),
    )
}

private inline fun <T> Cursor.useRows(block: (Cursor) -> T): T =
  try {
    block(this)
  } finally {
    close()
  }

fun nowIso(): String = Instant.now().toString()
