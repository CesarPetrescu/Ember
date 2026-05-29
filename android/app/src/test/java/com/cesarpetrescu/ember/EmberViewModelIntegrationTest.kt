package com.cesarpetrescu.ember

import android.app.Application
import androidx.test.core.app.ApplicationProvider
import com.cesarpetrescu.ember.data.TokenStore
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue

@RunWith(RobolectricTestRunner::class)
class EmberViewModelIntegrationTest {
  private lateinit var app: Application

  @Before
  fun setUp() {
    app = ApplicationProvider.getApplicationContext()
    val tokenStore = TokenStore(app)
    tokenStore.clear()
    app.deleteDatabase("ember.sqlite3")
  }

  @Test
  fun lifecycleBootBuildsDefaultChatState() {
    val vm = EmberViewModel(app)
    val state = vm.state.value
    assertNotNull(state.currentChatId)
    assertEquals(false, state.signedIn)
    assertTrue(state.chats.isNotEmpty())
    assertEquals("signed out", state.signedInLabel)
  }

  @Test
  fun canCreateSwitchDeleteChatWithoutNetworking() {
    val vm = EmberViewModel(app)
    val initial = vm.state.value.chats.size
    vm.newChat()
    assertEquals(initial + 1, vm.state.value.chats.size)

    val all = vm.state.value.chats
    val next = all.first { it.id != vm.state.value.currentChatId }
    vm.switchChat(next.id)
    assertEquals(next.id, vm.state.value.currentChatId)

    vm.deleteChat(next.id)
    assertEquals(initial, vm.state.value.chats.size)
  }

  @Test
  fun modelAndContextMutationsPropagateToChat() {
    val vm = EmberViewModel(app)
    vm.changeModel("gpt-5.4")
    assertEquals("gpt-5.4", vm.state.value.model)
    vm.changeEffort("xhigh")
    assertEquals("xhigh", vm.state.value.effort)
    vm.toggleContextWindow()

    assertEquals(1_000_000, vm.state.value.contextWindow)
    vm.changeEffort("low")
    vm.toggleContextWindow()
    assertEquals(272_000, vm.state.value.contextWindow)
  }
}
