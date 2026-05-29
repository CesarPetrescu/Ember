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

@RunWith(RobolectricTestRunner::class)
class ChatStoreIntegrationTest {
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
  fun createAndLoadChatPersistsMessagesAndChatData() {
    val chat = store.createChat("gpt-5.5", "medium", title = "Research")
    val loaded = requireNotNull(store.getChat(chat.id))

    assertEquals("Research", loaded.title)
    assertEquals("gpt-5.5", loaded.model)
    assertEquals("medium", loaded.effort)
    assertEquals(0, loaded.lastTotalTokens)
    assertEquals(null, loaded.contextWindowOverride)

    store.saveMessage(chat.id, "user", "hi")
    store.saveMessage(chat.id, "assistant", "yo")

    val messages = store.loadMessages(chat.id)
    assertEquals(2, messages.size)
    assertEquals("user", messages.first().role)
    assertEquals("assistant", messages.last().role)
  }

  @Test
  fun createSaveAndDeleteChatKeepsDataConsistent() {
    val chat = store.createChat("gpt-5.4", "high")
    store.saveMessage(chat.id, "user", "first")
    store.saveMessage(chat.id, "assistant", "second")
    store.updateChatModel(chat.id, "gpt-5.4-mini", "low")
    store.updateUsage(chat.id, 123_456)
    store.updateContextOverride(chat.id, 1_000_000)
    store.renameChat(chat.id, "", generated = false)

    val listBefore = store.listChats()
    assertEquals(1, listBefore.size)
    val loadedBefore = requireNotNull(store.getChat(chat.id))
    assertEquals("New chat", loadedBefore.title)
    assertEquals("low", loadedBefore.effort)
    assertEquals("gpt-5.4-mini", loadedBefore.model)
    assertEquals(123_456, loadedBefore.lastTotalTokens)
    assertEquals(1_000_000, loadedBefore.contextWindowOverride!!)

    store.deleteChat(chat.id)
    assertTrue(store.listChats().isEmpty())
    assertTrue(store.loadMessages(chat.id).isEmpty())
  }

  @Test
  fun listChatsOrdersByRecencyAndTouchUpdatesUpdatedAt() {
    val first = store.createChat("gpt-5.5", "low", title = "First")
    val second = store.createChat("gpt-5.4", "high", title = "Second")

    assertEquals("Second", requireNotNull(store.listChats().firstOrNull())?.title)

    store.touchChat(first.id)
    assertEquals("First", requireNotNull(store.listChats().firstOrNull())?.title)
  }

  @Test
  fun switchingChatsDoesNotLoseMessages() {
    val first = store.createChat("gpt-5.5", "medium", title = "First")
    val second = store.createChat("gpt-5.4", "low", title = "Second")
    store.saveMessage(first.id, "user", "first one")
    store.saveMessage(second.id, "assistant", "second one")

    val list = store.listChats().map { it.id }
    assertEquals(2, list.size)

    val loadedFirst = store.loadMessages(first.id)
    val loadedSecond = store.loadMessages(second.id)
    assertEquals(1, loadedFirst.size)
    assertEquals(1, loadedSecond.size)
    assertNotNull(store.getChat(first.id))
    assertNotNull(store.getChat(second.id))
    assertEquals("first one", loadedFirst.single().text)
    assertEquals("second one", loadedSecond.single().text)
  }
}
