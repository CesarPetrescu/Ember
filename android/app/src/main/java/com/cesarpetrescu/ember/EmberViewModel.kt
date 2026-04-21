package com.cesarpetrescu.ember

import android.app.Application
import android.os.SystemClock
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.cesarpetrescu.ember.data.AuthTokens
import com.cesarpetrescu.ember.data.Chat
import com.cesarpetrescu.ember.data.ChatStore
import com.cesarpetrescu.ember.data.ContextWindows
import com.cesarpetrescu.ember.data.DefaultEffort
import com.cesarpetrescu.ember.data.DefaultModel
import com.cesarpetrescu.ember.data.DeviceCode
import com.cesarpetrescu.ember.data.MaxContextWindows
import com.cesarpetrescu.ember.data.ModelSpecs
import com.cesarpetrescu.ember.data.TokenStore
import com.cesarpetrescu.ember.network.CodexClient
import com.cesarpetrescu.ember.network.ResponseMessage
import com.cesarpetrescu.ember.network.StreamSink
import java.util.UUID
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Job
import kotlinx.coroutines.launch
import kotlinx.coroutines.withTimeoutOrNull
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update

data class EmberUiState(
  val chats: List<Chat> = emptyList(),
  val currentChatId: String? = null,
  val messages: List<UiMessage> = emptyList(),
  val model: String = DefaultModel,
  val effort: String = DefaultEffort,
  val signedInLabel: String = "signed out",
  val signedIn: Boolean = false,
  val status: String = "",
  val streaming: Boolean = false,
  val signIn: SignInUiState? = null,
  val contextWindow: Int = ContextWindows[DefaultModel] ?: 272_000,
  val contextUsed: Int = 0,
  val contextPercentLeft: Int = 100,
)

data class UiMessage(
  val id: String = UUID.randomUUID().toString(),
  val role: String,
  val text: String,
  val reasoningSteps: List<String> = emptyList(),
  val error: Boolean = false,
  val active: Boolean = false,
  val phase: String = "",
)

data class SignInUiState(
  val phase: String,
  val deviceCode: DeviceCode? = null,
  val error: String? = null,
)

class EmberViewModel(application: Application) : AndroidViewModel(application) {
  private val store = ChatStore(application)
  private val tokenStore = TokenStore(application)
  private val appContext = application.applicationContext
  private val client = CodexClient()
  private var tokens: AuthTokens? = tokenStore.load()
  private var streamJob: Job? = null
  private var signInJob: Job? = null

  private val _state = MutableStateFlow(EmberUiState())
  val state: StateFlow<EmberUiState> = _state.asStateFlow()

  init {
    boot()
  }

  fun newChat() {
    val chat = store.createChat(_state.value.model, _state.value.effort)
    switchChat(chat.id)
  }

  fun switchChat(chatId: String) {
    if (_state.value.streaming) stopStreaming()
    loadChat(chatId)
  }

  fun deleteChat(chatId: String) {
    store.deleteChat(chatId)
    val remaining = store.listChats()
    val next = remaining.firstOrNull()?.id ?: store.createChat(_state.value.model, _state.value.effort).id
    loadChat(next)
  }

  fun renameChat(chatId: String, title: String) {
    store.renameChat(chatId, title.trim().ifBlank { "New chat" }, generated = true)
    refreshChats()
  }

  fun changeModel(model: String) {
    val effort = if (_state.value.effort in (ModelSpecs[model] ?: emptyList())) _state.value.effort else (ModelSpecs[model]?.firstOrNull() ?: DefaultEffort)
    _state.update { it.copy(model = model, effort = effort) }
    _state.value.currentChatId?.let { store.updateChatModel(it, model, effort) }
    refreshChats()
    updateContextBar()
  }

  fun changeEffort(effort: String) {
    _state.update { it.copy(effort = effort) }
    _state.value.currentChatId?.let { store.updateChatModel(it, _state.value.model, effort) }
    refreshChats()
  }

  fun toggleContextWindow() {
    val chatId = _state.value.currentChatId ?: return
    val high = MaxContextWindows[_state.value.model] ?: return
    val current = store.getChat(chatId)
    store.updateContextOverride(chatId, if (current?.contextWindowOverride == high) null else high)
    refreshChats()
    updateContextBar()
  }

  fun sendMessage(text: String) {
    val clean = text.trim()
    if (clean.isEmpty()) return
    val currentTokens = tokens
    if (currentTokens == null) {
      appendSystem("Sign in first.", error = true)
      startSignIn()
      return
    }
    if (_state.value.streaming) {
      stopStreaming()
      return
    }

    val chatId = _state.value.currentChatId ?: store.createChat(_state.value.model, _state.value.effort).id
    store.saveMessage(chatId, "user", clean)
    store.touchChat(chatId)

    val assistantId = UUID.randomUUID().toString()
    _state.update {
      it.copy(
        messages =
          it.messages +
            UiMessage(role = "user", text = clean) +
            UiMessage(id = assistantId, role = "assistant", text = "", active = true, phase = "connecting"),
        streaming = true,
        status = "connecting",
      )
    }
    refreshChats()

    val history = store.loadMessages(chatId).map { ResponseMessage(it.role, it.text) }
    val model = _state.value.model
    val effort = _state.value.effort
    val sessionId = UUID.randomUUID().toString()
    val assistantText = StringBuilder()
    val deltaBuffer = StringBuilder()
    var lastDeltaFlush = 0L
    var refreshedTokens = currentTokens

    fun flushDelta(force: Boolean = false) {
      val chunk =
        synchronized(deltaBuffer) {
          val now = SystemClock.elapsedRealtime()
          if (deltaBuffer.isEmpty() || (!force && now - lastDeltaFlush < 35L)) return
          lastDeltaFlush = now
          deltaBuffer.toString().also { deltaBuffer.clear() }
        }
      updateAssistant(assistantId) { it.copy(text = it.text + chunk) }
    }

    streamJob =
      viewModelScope.launch {
        BackgroundTaskService.start(appContext, "Streaming a response")
        try {
          refreshedTokens =
            client.streamChat(
              tokens = currentTokens,
              refreshAndSave = { old ->
                client.refreshTokens(old).also {
                  tokens = it
                  tokenStore.save(it)
                  updateAuthLabel()
                }
              },
              history = history,
              model = model,
              effort = effort,
              sessionId = sessionId,
              sink =
                object : StreamSink {
                  override fun onPhase(phase: String) {
                    _state.update { it.copy(status = phase) }
                    updateAssistant(assistantId) { it.copy(phase = phase, active = true) }
                  }

                  override fun onDelta(delta: String) {
                    assistantText.append(delta)
                    synchronized(deltaBuffer) { deltaBuffer.append(delta) }
                    flushDelta()
                  }

                  override fun onReasoning(delta: String) {
                    updateAssistant(assistantId) { msg ->
                      val steps = if (msg.reasoningSteps.isEmpty()) listOf(delta) else msg.reasoningSteps.dropLast(1) + (msg.reasoningSteps.last() + delta)
                      msg.copy(reasoningSteps = steps)
                    }
                  }

                  override fun onReasoningStep(kind: String) {
                    if (kind == "start") {
                      updateAssistant(assistantId) { it.copy(reasoningSteps = it.reasoningSteps + "") }
                    }
                  }

                  override fun onUsage(totalTokens: Int) {
                    store.updateUsage(chatId, totalTokens)
                    updateContextBar()
                  }
                },
            )
          flushDelta(force = true)
          tokens = refreshedTokens
          tokenStore.save(refreshedTokens)
          if (assistantText.isNotEmpty()) {
            store.saveMessage(chatId, "assistant", assistantText.toString())
            store.touchChat(chatId)
            maybeGenerateTitle(chatId)
          }
          updateAssistant(assistantId) { it.copy(active = false, phase = "") }
          _state.update { it.copy(streaming = false, status = "") }
          refreshChats()
        } catch (t: Throwable) {
          flushDelta(force = true)
          val cancelled = t is CancellationException || streamJob?.isCancelled == true
          updateAssistant(assistantId) { it.copy(active = false, phase = "") }
          _state.update { it.copy(streaming = false, status = if (cancelled) "cancelled" else "") }
          if (!cancelled) appendSystem(t.message ?: "stream failed", error = true)
        } finally {
          streamJob = null
          BackgroundTaskService.stop(appContext)
        }
      }
  }

  fun stopStreaming() {
    streamJob?.cancel()
    streamJob = null
    _state.update { state ->
      state.copy(
        streaming = false,
        status = "cancelled",
        messages = state.messages.map { if (it.active) it.copy(active = false, phase = "") else it },
      )
    }
    BackgroundTaskService.stop(appContext)
  }

  fun startSignIn() {
    signInJob?.cancel()
    signInJob =
      viewModelScope.launch {
        BackgroundTaskService.start(appContext, "Waiting for ChatGPT sign-in")
        _state.update { it.copy(signIn = SignInUiState("Requesting device code...")) }
        try {
          val code = client.requestDeviceCode()
          _state.update { it.copy(signIn = SignInUiState("Open the link and enter this code.", code)) }
          val signedIn =
            client.pollDeviceAuthorization(
              deviceCode = code,
              isCancelled = { signInJob?.isCancelled == true },
              onTransientError = { message ->
                _state.update { it.copy(signIn = it.signIn?.copy(phase = message)) }
              },
            )
          tokens = signedIn
          tokenStore.save(signedIn)
          updateAuthLabel()
          _state.update { it.copy(signIn = null, status = "Signed in.") }
        } catch (t: Throwable) {
          _state.update { it.copy(signIn = SignInUiState("Sign-in failed.", error = t.message ?: "unknown error")) }
        } finally {
          BackgroundTaskService.stop(appContext)
        }
      }
  }

  fun cancelSignIn() {
    signInJob?.cancel()
    signInJob = null
    _state.update { it.copy(signIn = null) }
    BackgroundTaskService.stop(appContext)
  }

  fun signOut() {
    if (_state.value.streaming) stopStreaming()
    cancelSignIn()
    tokens = null
    tokenStore.clear()
    updateAuthLabel()
  }

  private fun boot() {
    val chat = store.listChats().firstOrNull() ?: store.createChat(DefaultModel, DefaultEffort)
    loadChat(chat.id)
    updateAuthLabel()
  }

  private fun loadChat(chatId: String) {
    val chat = store.getChat(chatId) ?: return
    val messages = store.loadMessages(chatId).map { UiMessage(role = it.role, text = it.text) }
    _state.update {
      it.copy(
        chats = store.listChats(),
        currentChatId = chat.id,
        messages = messages,
        model = chat.model,
        effort = chat.effort,
      )
    }
    updateContextBar()
  }

  private fun refreshChats() {
    _state.update { it.copy(chats = store.listChats()) }
  }

  private fun updateContextBar() {
    val chat = _state.value.currentChatId?.let(store::getChat)
    val model = _state.value.model
    val high = MaxContextWindows[model]
    val base = ContextWindows[model] ?: 272_000
    val window = if (high != null && chat?.contextWindowOverride == high) high else base
    val used = chat?.lastTotalTokens ?: 0
    val pct = com.cesarpetrescu.ember.data.percentOfContextRemaining(used, window)
    _state.update { it.copy(contextWindow = window, contextUsed = used, contextPercentLeft = pct) }
  }

  private fun updateAuthLabel() {
    val t = tokens
    _state.update {
      if (t == null) {
        it.copy(signedIn = false, signedInLabel = "signed out")
      } else {
        val label = listOfNotNull("signed in", t.email ?: t.accountId?.take(8), t.planType).joinToString(" · ")
        it.copy(signedIn = true, signedInLabel = label)
      }
    }
  }

  private fun updateAssistant(id: String, transform: (UiMessage) -> UiMessage) {
    _state.update { state ->
      state.copy(messages = state.messages.map { if (it.id == id) transform(it) else it })
    }
  }

  private fun appendSystem(text: String, error: Boolean = false) {
    _state.update { it.copy(messages = it.messages + UiMessage(role = "system", text = text, error = error)) }
  }

  private fun maybeGenerateTitle(chatId: String) {
    val chat = store.getChat(chatId) ?: return
    if (chat.titleGenerated) return
    val currentTokens = tokens ?: return
    val messages = store.loadMessages(chatId)
    val firstUser = messages.firstOrNull { it.role == "user" }?.text ?: return
    val firstAssistant = messages.firstOrNull { it.role == "assistant" }?.text ?: return
    viewModelScope.launch {
      val title = withTimeoutOrNull(25_000) { client.generateTitle(currentTokens, firstUser, firstAssistant) }.orEmpty()
      if (title.isNotBlank()) {
        store.renameChat(chatId, title, generated = true)
        refreshChats()
      }
    }
  }
}
