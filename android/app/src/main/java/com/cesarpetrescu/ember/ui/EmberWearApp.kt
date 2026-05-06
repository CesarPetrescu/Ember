package com.cesarpetrescu.ember.ui

import android.content.Intent
import android.net.Uri
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.focusable
import androidx.compose.foundation.ScrollState
import androidx.compose.foundation.gestures.scrollBy
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ColumnScope
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.imePadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.safeDrawingPadding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.LazyListState
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.BasicTextField
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.focus.FocusRequester
import androidx.compose.ui.focus.focusRequester
import androidx.compose.ui.input.rotary.onRotaryScrollEvent
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.cesarpetrescu.ember.EmberUiState
import com.cesarpetrescu.ember.EmberViewModel
import com.cesarpetrescu.ember.SignInUiState
import com.cesarpetrescu.ember.UiMessage
import com.cesarpetrescu.ember.data.Chat
import com.cesarpetrescu.ember.data.MaxContextWindows
import com.cesarpetrescu.ember.data.ModelSpecs
import com.cesarpetrescu.ember.data.formatTokenCount
import com.cesarpetrescu.ember.theme.Accent
import com.cesarpetrescu.ember.theme.AccentFg
import com.cesarpetrescu.ember.theme.AppBg
import com.cesarpetrescu.ember.theme.Border
import com.cesarpetrescu.ember.theme.Error
import com.cesarpetrescu.ember.theme.Muted
import com.cesarpetrescu.ember.theme.Panel
import com.cesarpetrescu.ember.theme.PanelAlt
import com.cesarpetrescu.ember.theme.Success
import com.cesarpetrescu.ember.theme.TextDim
import com.cesarpetrescu.ember.theme.TextMain
import kotlinx.coroutines.launch

private val WearScreenPadding = PaddingValues(horizontal = 18.dp, vertical = 10.dp)
private const val RotaryScrollMultiplier = 1.35f

@Composable
fun EmberWearApp(viewModel: EmberViewModel) {
  val state by viewModel.state.collectAsStateWithLifecycle()
  var screen by rememberSaveable { mutableStateOf(WearScreen.Chat) }

  state.signIn?.let {
    WearSignInScreen(
      state = it,
      onCancel = viewModel::cancelSignIn,
      onRetry = viewModel::startSignIn,
    )
    return
  }

  when (screen) {
    WearScreen.Chat ->
      WearChatScreen(
        state = state,
        onChats = { screen = WearScreen.Chats },
        onSettings = { screen = WearScreen.Settings },
        onSend = viewModel::sendMessage,
        onStop = viewModel::stopStreaming,
        onSignIn = viewModel::startSignIn,
      )
    WearScreen.Chats ->
      WearChatListScreen(
        state = state,
        onBack = { screen = WearScreen.Chat },
        onNew = {
          viewModel.newChat()
          screen = WearScreen.Chat
        },
        onSelect = {
          viewModel.switchChat(it)
          screen = WearScreen.Chat
        },
        onDelete = viewModel::deleteChat,
      )
    WearScreen.Settings ->
      WearSettingsScreen(
        state = state,
        onBack = { screen = WearScreen.Chat },
        onModel = viewModel::changeModel,
        onEffort = viewModel::changeEffort,
        onToggleContext = viewModel::toggleContextWindow,
        onSignIn = viewModel::startSignIn,
        onSignOut = viewModel::signOut,
      )
  }
}

private enum class WearScreen { Chat, Chats, Settings }

@Composable
private fun WearChatScreen(
  state: EmberUiState,
  onChats: () -> Unit,
  onSettings: () -> Unit,
  onSend: (String) -> Unit,
  onStop: () -> Unit,
  onSignIn: () -> Unit,
) {
  Column(
    modifier =
      Modifier
        .fillMaxSize()
        .background(AppBg)
        .safeDrawingPadding()
        .padding(WearScreenPadding),
  ) {
    WearHeader(state = state, onChats = onChats, onSettings = onSettings)
    WearContextLine(state)
    if (state.signedIn) {
      WearMessageList(
        messages = state.messages,
        livePhase = state.status,
        modifier = Modifier.weight(1f),
      )
      WearComposer(
        state = state,
        onSend = onSend,
        onStop = onStop,
      )
    } else {
      WearSignedOutHome(
        signedInLabel = state.signedInLabel,
        onSignIn = onSignIn,
        modifier = Modifier.weight(1f),
      )
    }
  }
}

@Composable
private fun WearHeader(
  state: EmberUiState,
  onChats: () -> Unit,
  onSettings: () -> Unit,
) {
  Row(
    modifier = Modifier.fillMaxWidth().height(30.dp),
    verticalAlignment = Alignment.CenterVertically,
    horizontalArrangement = Arrangement.spacedBy(6.dp),
  ) {
    WearChip(text = "Chats", onClick = onChats, modifier = Modifier.width(46.dp))
    Text(
      "Ember",
      color = TextMain,
      style = MaterialTheme.typography.titleMedium,
      textAlign = TextAlign.Center,
      maxLines = 1,
      overflow = TextOverflow.Ellipsis,
      modifier = Modifier.weight(1f),
    )
    Surface(
      color = if (state.signedIn) Success else Muted,
      shape = RoundedCornerShape(50),
      modifier = Modifier.size(7.dp),
      content = {},
    )
    WearChip(text = "Set", onClick = onSettings, modifier = Modifier.width(38.dp))
  }
}

@Composable
private fun WearContextLine(state: EmberUiState) {
  Row(
    modifier = Modifier.fillMaxWidth().padding(top = 6.dp, bottom = 6.dp),
    verticalAlignment = Alignment.CenterVertically,
  ) {
    Text(
      state.model.removePrefix("gpt-"),
      color = Muted,
      fontSize = 10.sp,
      maxLines = 1,
      overflow = TextOverflow.Ellipsis,
      modifier = Modifier.width(58.dp),
    )
    LinearProgressIndicator(
      progress = { state.contextPercentLeft / 100f },
      color = Accent,
      trackColor = PanelAlt,
      modifier = Modifier.weight(1f).height(4.dp).clip(RoundedCornerShape(3.dp)),
    )
    Text(
      "${state.contextPercentLeft}%",
      color = Muted,
      fontSize = 10.sp,
      textAlign = TextAlign.End,
      modifier = Modifier.width(38.dp),
    )
  }
}

@Composable
private fun WearSignedOutHome(
  signedInLabel: String,
  onSignIn: () -> Unit,
  modifier: Modifier = Modifier,
) {
  Box(modifier = modifier.fillMaxWidth(), contentAlignment = Alignment.Center) {
    Column(
      horizontalAlignment = Alignment.CenterHorizontally,
      verticalArrangement = Arrangement.spacedBy(10.dp),
      modifier = Modifier.fillMaxWidth(),
    ) {
      Text("ChatGPT sign-in", color = TextMain, fontSize = 17.sp, fontWeight = FontWeight.Bold)
      Text(
        signedInLabel,
        color = Muted,
        fontSize = 12.sp,
        lineHeight = 16.sp,
        textAlign = TextAlign.Center,
      )
      WearChip(
        text = "Sign in",
        onClick = onSignIn,
        accent = true,
        modifier = Modifier.fillMaxWidth().height(42.dp),
      )
    }
  }
}

@Composable
private fun WearMessageList(
  messages: List<UiMessage>,
  livePhase: String,
  modifier: Modifier = Modifier,
) {
  val listState = rememberLazyListState()
  LaunchedEffect(messages.size, messages.lastOrNull()?.text?.length, livePhase) {
    if (messages.isNotEmpty()) listState.animateScrollToItem(messages.lastIndex)
  }

  if (messages.isEmpty()) {
    Box(modifier = modifier.fillMaxWidth(), contentAlignment = Alignment.Center) {
      Text("Say something", color = Muted, fontSize = 13.sp)
    }
    return
  }

  LazyColumn(
    state = listState,
    modifier = modifier.fillMaxWidth().rotaryScroll(listState),
    contentPadding = PaddingValues(top = 4.dp, bottom = 8.dp),
    verticalArrangement = Arrangement.spacedBy(8.dp),
  ) {
    items(messages, key = { it.id }) { message ->
      WearMessageItem(message)
    }
  }
}

@Composable
private fun WearMessageItem(message: UiMessage) {
  val isUser = message.role == "user"
  val label =
    when (message.role) {
      "user" -> "You"
      "assistant" -> "Ember"
      else -> "System"
    }
  Column(
    modifier =
      Modifier
        .fillMaxWidth()
        .clip(RoundedCornerShape(8.dp))
        .background(if (isUser) Panel else Color.Transparent)
        .padding(if (isUser) 8.dp else 2.dp),
  ) {
    Row(verticalAlignment = Alignment.CenterVertically) {
      Text(
        label,
        color = if (isUser) Accent else if (message.error) Error else TextMain,
        fontSize = 11.sp,
        fontWeight = FontWeight.Bold,
      )
      if (message.active && message.phase.isNotBlank()) {
        Text(
          "  ${message.phase}",
          color = Muted,
          fontSize = 10.sp,
          maxLines = 1,
          overflow = TextOverflow.Ellipsis,
        )
      }
    }
    message.reasoningSteps.lastOrNull { it.isNotBlank() }?.let {
      Text(
        it.take(220),
        color = TextDim,
        fontSize = 11.sp,
        lineHeight = 15.sp,
        fontStyle = FontStyle.Italic,
        modifier = Modifier.padding(top = 4.dp),
      )
    }
    val body =
      when {
        message.text.isNotBlank() -> message.text
        message.active -> message.phase.ifBlank { "connecting" }
        else -> ""
      }
    if (body.isNotBlank()) {
      Text(
        body,
        color = if (message.error) Error else TextMain,
        fontSize = 13.sp,
        lineHeight = 18.sp,
        modifier = Modifier.padding(top = 4.dp),
      )
    }
  }
}

@Composable
private fun WearComposer(
  state: EmberUiState,
  onSend: (String) -> Unit,
  onStop: () -> Unit,
) {
  var text by rememberSaveable(state.currentChatId) { mutableStateOf("") }
  fun submit() {
    when {
      state.streaming -> onStop()
      text.isNotBlank() -> {
        onSend(text)
        text = ""
      }
    }
  }

  Column(
    modifier =
      Modifier
        .fillMaxWidth()
        .imePadding(),
  ) {
    Box(
      modifier =
        Modifier
          .fillMaxWidth()
          .height(48.dp)
          .clip(RoundedCornerShape(8.dp))
          .background(Panel)
          .padding(horizontal = 10.dp, vertical = 8.dp),
      contentAlignment = Alignment.CenterStart,
    ) {
      if (text.isBlank()) Text("Message", color = Muted, fontSize = 13.sp)
      BasicTextField(
        value = text,
        onValueChange = { value -> text = value },
        modifier = Modifier.fillMaxWidth(),
        textStyle = TextStyle(color = TextMain, fontSize = 13.sp, lineHeight = 16.sp),
        keyboardOptions = KeyboardOptions(imeAction = ImeAction.Send),
        keyboardActions = KeyboardActions(onSend = { submit() }),
        maxLines = 2,
      )
    }
    Row(
      modifier = Modifier.fillMaxWidth().padding(top = 6.dp),
      verticalAlignment = Alignment.CenterVertically,
      horizontalArrangement = Arrangement.spacedBy(8.dp),
    ) {
      Text(
        state.status.ifBlank { state.effort },
        color = Muted,
        fontSize = 10.sp,
        maxLines = 1,
        overflow = TextOverflow.Ellipsis,
        modifier = Modifier.weight(1f),
      )
      WearChip(
        text = if (state.streaming) "Stop" else "Send",
        onClick = { submit() },
        accent = !state.streaming,
        modifier = Modifier.width(68.dp),
      )
    }
  }
}

@Composable
private fun WearChatListScreen(
  state: EmberUiState,
  onBack: () -> Unit,
  onNew: () -> Unit,
  onSelect: (String) -> Unit,
  onDelete: (String) -> Unit,
) {
  val listState = rememberLazyListState()
  LazyColumn(
    state = listState,
    modifier =
      Modifier
        .fillMaxSize()
        .background(AppBg)
        .safeDrawingPadding()
        .padding(WearScreenPadding)
        .rotaryScroll(listState),
    contentPadding = PaddingValues(bottom = 18.dp),
    verticalArrangement = Arrangement.spacedBy(8.dp),
  ) {
    item {
      WearPageHeader(title = "Chats", onBack = onBack) {
        WearChip("New", onNew, accent = true, modifier = Modifier.width(54.dp))
      }
    }
    items(state.chats, key = { it.id }) { chat ->
      WearChatRow(
        chat = chat,
        selected = chat.id == state.currentChatId,
        canDelete = state.chats.size > 1,
        onSelect = { onSelect(chat.id) },
        onDelete = { onDelete(chat.id) },
      )
    }
  }
}

@Composable
private fun WearChatRow(
  chat: Chat,
  selected: Boolean,
  canDelete: Boolean,
  onSelect: () -> Unit,
  onDelete: () -> Unit,
) {
  Column(
    modifier =
      Modifier
        .fillMaxWidth()
        .clip(RoundedCornerShape(8.dp))
        .background(if (selected) PanelAlt else Panel)
        .clickable(onClick = onSelect)
        .padding(10.dp),
  ) {
    Text(
      chat.title,
      color = if (selected) TextMain else TextDim,
      fontSize = 13.sp,
      fontWeight = if (selected) FontWeight.Bold else FontWeight.Normal,
      maxLines = 2,
      overflow = TextOverflow.Ellipsis,
    )
    Row(
      modifier = Modifier.fillMaxWidth().padding(top = 6.dp),
      verticalAlignment = Alignment.CenterVertically,
    ) {
      Text(
        chat.model.removePrefix("gpt-"),
        color = Muted,
        fontSize = 10.sp,
        maxLines = 1,
        overflow = TextOverflow.Ellipsis,
        modifier = Modifier.weight(1f),
      )
      if (canDelete) {
        WearChip("Delete", onDelete, modifier = Modifier.width(62.dp))
      }
    }
  }
}

@Composable
private fun WearSettingsScreen(
  state: EmberUiState,
  onBack: () -> Unit,
  onModel: (String) -> Unit,
  onEffort: (String) -> Unit,
  onToggleContext: () -> Unit,
  onSignIn: () -> Unit,
  onSignOut: () -> Unit,
) {
  val listState = rememberLazyListState()
  LazyColumn(
    state = listState,
    modifier =
      Modifier
        .fillMaxSize()
        .background(AppBg)
        .safeDrawingPadding()
        .padding(WearScreenPadding)
        .rotaryScroll(listState),
    contentPadding = PaddingValues(bottom = 18.dp),
    verticalArrangement = Arrangement.spacedBy(10.dp),
  ) {
    item { WearPageHeader(title = "Settings", onBack = onBack) }
    item {
      WearPanel {
        Text(state.signedInLabel, color = if (state.signedIn) Success else Muted, fontSize = 12.sp, lineHeight = 16.sp)
        Spacer(Modifier.height(8.dp))
        WearChip(
          text = if (state.signedIn) "Sign out" else "Sign in",
          onClick = if (state.signedIn) onSignOut else onSignIn,
          accent = !state.signedIn,
          modifier = Modifier.fillMaxWidth().height(40.dp),
        )
      }
    }
    item { WearSectionTitle("Model") }
    items(ModelSpecs.keys.toList()) { model ->
      WearOptionRow(
        text = model,
        selected = model == state.model,
        onClick = { onModel(model) },
      )
    }
    item { WearSectionTitle("Effort") }
    items(ModelSpecs[state.model].orEmpty()) { effort ->
      WearOptionRow(
        text = effort,
        selected = effort == state.effort,
        onClick = { onEffort(effort) },
      )
    }
    item {
      WearPanel {
        Text(
          "${formatTokenCount(state.contextUsed)} / ${formatTokenCount(state.contextWindow)}",
          color = TextMain,
          fontSize = 13.sp,
        )
        Spacer(Modifier.height(6.dp))
        WearContextLine(state)
        val high = MaxContextWindows[state.model]
        if (high != null) {
          Spacer(Modifier.height(8.dp))
          WearChip(
            text = if (state.contextWindow == high) "Using 1M" else "Use 1M",
            onClick = onToggleContext,
            modifier = Modifier.fillMaxWidth().height(40.dp),
          )
        }
      }
    }
  }
}

@Composable
private fun WearSignInScreen(
  state: SignInUiState,
  onCancel: () -> Unit,
  onRetry: () -> Unit,
) {
  val context = LocalContext.current
  val scrollState = rememberScrollState()
  Column(
    modifier =
      Modifier
        .fillMaxSize()
        .background(AppBg)
        .safeDrawingPadding()
        .padding(horizontal = 30.dp, vertical = 16.dp)
        .verticalScroll(scrollState)
        .rotaryScroll(scrollState),
    horizontalAlignment = Alignment.CenterHorizontally,
  ) {
    Text("Sign in", color = TextMain, fontSize = 18.sp, fontWeight = FontWeight.Bold)
    Spacer(Modifier.height(10.dp))
    Text(state.phase, color = TextDim, fontSize = 12.sp, lineHeight = 17.sp, textAlign = TextAlign.Center)
    state.deviceCode?.let { code ->
      Spacer(Modifier.height(12.dp))
      Text(
        code.userCode,
        color = Accent,
        fontSize = 25.sp,
        fontWeight = FontWeight.Bold,
        fontFamily = FontFamily.Monospace,
        textAlign = TextAlign.Center,
      )
      Spacer(Modifier.height(8.dp))
      Text(code.verificationUrl, color = Muted, fontSize = 11.sp, lineHeight = 15.sp, textAlign = TextAlign.Center)
      Spacer(Modifier.height(10.dp))
      WearChip(
        text = "Open link",
        onClick = { context.startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(code.verificationUrl))) },
        modifier = Modifier.fillMaxWidth().height(40.dp),
      )
    }
    state.error?.let {
      Spacer(Modifier.height(10.dp))
      Text(it, color = Error, fontSize = 12.sp, lineHeight = 17.sp, textAlign = TextAlign.Center)
      Spacer(Modifier.height(8.dp))
      WearChip("Retry", onRetry, accent = true, modifier = Modifier.fillMaxWidth().height(40.dp))
    }
    Spacer(Modifier.height(8.dp))
    WearChip("Cancel", onCancel, modifier = Modifier.fillMaxWidth().height(40.dp))
  }
}

@Composable
private fun WearPageHeader(
  title: String,
  onBack: () -> Unit,
  trailing: @Composable () -> Unit = {},
) {
  Row(
    modifier = Modifier.fillMaxWidth().height(32.dp),
    verticalAlignment = Alignment.CenterVertically,
    horizontalArrangement = Arrangement.spacedBy(8.dp),
  ) {
    WearChip("Back", onBack, modifier = Modifier.width(58.dp))
    Text(
      title,
      color = TextMain,
      fontSize = 16.sp,
      fontWeight = FontWeight.Bold,
      maxLines = 1,
      overflow = TextOverflow.Ellipsis,
      modifier = Modifier.weight(1f),
    )
    trailing()
  }
}

@Composable
private fun WearPanel(content: @Composable ColumnScope.() -> Unit) {
  Column(
    modifier =
      Modifier
        .fillMaxWidth()
        .clip(RoundedCornerShape(8.dp))
        .background(Panel)
        .padding(10.dp),
    content = content,
  )
}

@Composable
private fun WearSectionTitle(text: String) {
  Text(
    text,
    color = Accent,
    fontSize = 12.sp,
    fontWeight = FontWeight.Bold,
    modifier = Modifier.padding(top = 4.dp),
  )
}

@Composable
private fun WearOptionRow(
  text: String,
  selected: Boolean,
  onClick: () -> Unit,
) {
  Row(
    modifier =
      Modifier
        .fillMaxWidth()
        .height(40.dp)
        .clip(RoundedCornerShape(8.dp))
        .background(if (selected) Accent else Panel)
        .clickable(onClick = onClick)
        .padding(horizontal = 12.dp),
    verticalAlignment = Alignment.CenterVertically,
  ) {
    Text(
      if (selected) "* $text" else text,
      color = if (selected) AccentFg else TextMain,
      fontSize = 13.sp,
      maxLines = 1,
      overflow = TextOverflow.Ellipsis,
    )
  }
}

@Composable
private fun Modifier.rotaryScroll(listState: LazyListState): Modifier {
  val scope = rememberCoroutineScope()
  val focusRequester = remember { FocusRequester() }
  LaunchedEffect(listState) {
    focusRequester.requestFocus()
  }
  return this
    .focusRequester(focusRequester)
    .onRotaryScrollEvent { event ->
      scope.launch { listState.scrollBy(event.verticalScrollPixels * RotaryScrollMultiplier) }
      true
    }
    .focusable()
}

@Composable
private fun Modifier.rotaryScroll(scrollState: ScrollState): Modifier {
  val scope = rememberCoroutineScope()
  val focusRequester = remember { FocusRequester() }
  LaunchedEffect(scrollState) {
    focusRequester.requestFocus()
  }
  return this
    .focusRequester(focusRequester)
    .onRotaryScrollEvent { event ->
      scope.launch { scrollState.scrollBy(event.verticalScrollPixels * RotaryScrollMultiplier) }
      true
    }
    .focusable()
}

@Composable
private fun WearChip(
  text: String,
  onClick: () -> Unit,
  modifier: Modifier = Modifier,
  accent: Boolean = false,
) {
  Box(
    modifier =
      modifier
        .heightIn(min = 30.dp)
        .clip(RoundedCornerShape(8.dp))
        .background(if (accent) Accent else PanelAlt)
        .clickable(onClick = onClick)
        .padding(horizontal = 6.dp),
    contentAlignment = Alignment.Center,
  ) {
    Text(
      text,
      color = if (accent) AccentFg else TextMain,
      fontSize = 11.sp,
      fontWeight = FontWeight.Bold,
      maxLines = 1,
      overflow = TextOverflow.Ellipsis,
      textAlign = TextAlign.Center,
    )
  }
}
