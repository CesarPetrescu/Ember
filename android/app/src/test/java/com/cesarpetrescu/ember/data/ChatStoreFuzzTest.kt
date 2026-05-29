package com.cesarpetrescu.ember.data

import android.content.Context
import androidx.test.core.app.ApplicationProvider
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import kotlin.random.Random

@RunWith(RobolectricTestRunner::class)
class ChatStoreFuzzTest {
  private lateinit var context: Context
  private lateinit var store: ChatStore

  @Before
  fun setUp() {
    context = ApplicationProvider.getApplicationContext()
    context.deleteDatabase("ember.sqlite3")
    store = ChatStore(context)
  }

  @After
  fun tearDown() {
    context.deleteDatabase("ember.sqlite3")
  }

  @Test
  fun randomChatOperationsDoNotViolatePersistenceInvariants() {
    repeat(120) {
      val chats = store.listChats()
      when (Random.nextInt(0, 5)) {
        0 -> {
          val model = listOf("gpt-5.5", "gpt-5.4", "gpt-5.4-mini", SparkModel).random()
          val effort = listOf("low", "medium", "high", "xhigh").random()
          store.createChat(model, effort, title = "Fuzz $it")
        }
        1 -> {
          if (chats.isNotEmpty()) {
            val chat = chats.random()
            store.saveMessage(chat.id, "user", "random message ${Random.nextInt()}")
            store.saveMessage(chat.id, "assistant", "reply ${Random.nextInt()}")
          }
        }
        2 -> {
          if (chats.isNotEmpty()) {
            val chat = chats.random()
            store.updateUsage(chat.id, Random.nextInt(0, 500_000))
            val override = if (Random.nextBoolean()) 1_000_000 else null
            store.updateContextOverride(chat.id, override)
            store.touchChat(chat.id)
          }
        }
        3 -> {
          if (chats.isNotEmpty()) {
            store.renameChat(chats.random().id, "  ", generated = Random.nextBoolean())
          }
        }
        4 -> {
          if (chats.isNotEmpty()) {
            store.deleteChat(chats.random().id)
          }
        }
      }
    }

    store.listChats().forEach { chat ->
      val messages = store.loadMessages(chat.id)
      messages.forEachIndexed { index, message ->
        assertNotNull(message.chatId)
        assertEquals(chat.id, message.chatId)
        if (index > 0) {
          assertTrue(messages[index - 1].createdAt <= message.createdAt)
        }
      }
    }
  }
}
