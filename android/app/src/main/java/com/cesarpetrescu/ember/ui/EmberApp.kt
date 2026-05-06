package com.cesarpetrescu.ember.ui

import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.imePadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.safeDrawingPadding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.selection.SelectionContainer
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.DividerDefaults
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.ModalDrawerSheet
import androidx.compose.material3.ModalNavigationDrawer
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TextFieldDefaults
import androidx.compose.material3.rememberDrawerState
import androidx.compose.material3.DrawerValue
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalClipboardManager
import androidx.compose.ui.platform.LocalConfiguration
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.AnnotatedString
import androidx.compose.ui.text.SpanStyle
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.buildAnnotatedString
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.text.withStyle
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.lifecycle.viewmodel.compose.viewModel
import com.cesarpetrescu.ember.EmberUiState
import com.cesarpetrescu.ember.EmberViewModel
import com.cesarpetrescu.ember.SignInUiState
import com.cesarpetrescu.ember.UiMessage
import com.cesarpetrescu.ember.data.Chat
import com.cesarpetrescu.ember.data.ContextWindows
import com.cesarpetrescu.ember.data.MaxContextWindows
import com.cesarpetrescu.ember.data.ModelSpecs
import com.cesarpetrescu.ember.data.formatTokenCount
import com.cesarpetrescu.ember.theme.Accent
import com.cesarpetrescu.ember.theme.AccentFg
import com.cesarpetrescu.ember.theme.AppBg
import com.cesarpetrescu.ember.theme.AppBgAlt
import com.cesarpetrescu.ember.theme.Border
import com.cesarpetrescu.ember.theme.CodeBg
import com.cesarpetrescu.ember.theme.Error
import com.cesarpetrescu.ember.theme.Muted
import com.cesarpetrescu.ember.theme.Panel
import com.cesarpetrescu.ember.theme.PanelAlt
import com.cesarpetrescu.ember.theme.Success
import com.cesarpetrescu.ember.theme.TextDim
import com.cesarpetrescu.ember.theme.TextMain
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlin.math.min

@Composable
fun EmberApp(viewModel: EmberViewModel = viewModel()) {
  val context = LocalContext.current
  val configuration = LocalConfiguration.current
  val isWearDevice =
    context.packageManager.hasSystemFeature(PackageManager.FEATURE_WATCH) ||
      min(configuration.screenWidthDp, configuration.screenHeightDp) <= 240
  if (isWearDevice) {
    EmberWearApp(viewModel)
    return
  }

  val state by viewModel.state.collectAsStateWithLifecycle()
  val drawerState = rememberDrawerState(DrawerValue.Closed)
  val scope = rememberCoroutineScope()
  var renameTarget by remember { mutableStateOf<Chat?>(null) }

  ModalNavigationDrawer(
    drawerState = drawerState,
    drawerContent = {
      ChatDrawer(
        state = state,
        onNew = {
          viewModel.newChat()
          scope.launch { drawerState.close() }
        },
        onSelect = {
          viewModel.switchChat(it)
          scope.launch { drawerState.close() }
        },
        onDelete = viewModel::deleteChat,
        onRename = { renameTarget = it },
      )
    },
  ) {
    Column(
      modifier =
        Modifier
          .fillMaxSize()
          .background(AppBg)
          .safeDrawingPadding(),
    ) {
      TopBar(
        state = state,
        onMenu = { scope.launch { drawerState.open() } },
        onSignIn = viewModel::startSignIn,
        onSignOut = viewModel::signOut,
      )
      HorizontalDivider(color = Border)
      ContextBar(state = state, onToggle = viewModel::toggleContextWindow)
      HorizontalDivider(color = Border)
      MessageList(messages = state.messages, livePhase = state.status, modifier = Modifier.weight(1f))
      Composer(
        state = state,
        onModel = viewModel::changeModel,
        onEffort = viewModel::changeEffort,
        onSend = viewModel::sendMessage,
        onStop = viewModel::stopStreaming,
      )
    }
  }

  state.signIn?.let {
    SignInDialog(state = it, onCancel = viewModel::cancelSignIn, onRetry = viewModel::startSignIn)
  }

  renameTarget?.let { chat ->
    RenameDialog(
      initial = chat.title,
      onDismiss = { renameTarget = null },
      onSave = {
        viewModel.renameChat(chat.id, it)
        renameTarget = null
      },
    )
  }
}

@Composable
private fun TopBar(
  state: EmberUiState,
  onMenu: () -> Unit,
  onSignIn: () -> Unit,
  onSignOut: () -> Unit,
) {
  Row(
    modifier = Modifier.fillMaxWidth().height(56.dp).background(AppBg).padding(horizontal = 10.dp),
    verticalAlignment = Alignment.CenterVertically,
  ) {
    TextButton(onClick = onMenu) { Text("☰", color = TextMain, fontSize = 22.sp) }
    Text("✻", color = Accent, fontWeight = FontWeight.Bold, fontSize = 20.sp)
    Spacer(Modifier.width(8.dp))
    Text("Ember", color = TextMain, style = MaterialTheme.typography.titleMedium)
    Spacer(Modifier.weight(1f))
    Text(
      state.signedInLabel,
      color = if (state.signedIn) Success else Muted,
      style = MaterialTheme.typography.labelMedium,
      modifier = Modifier.padding(end = 8.dp),
    )
    TextButton(onClick = if (state.signedIn) onSignOut else onSignIn) {
      Text(if (state.signedIn) "Sign out" else "Sign in")
    }
  }
}

@Composable
private fun ChatDrawer(
  state: EmberUiState,
  onNew: () -> Unit,
  onSelect: (String) -> Unit,
  onDelete: (String) -> Unit,
  onRename: (Chat) -> Unit,
) {
  ModalDrawerSheet(drawerContainerColor = Panel, drawerContentColor = TextMain) {
    Column(Modifier.fillMaxHeight().width(320.dp).padding(12.dp)) {
      Row(verticalAlignment = Alignment.CenterVertically, modifier = Modifier.fillMaxWidth()) {
        Text("Chats", color = Muted, fontWeight = FontWeight.Bold)
        Spacer(Modifier.weight(1f))
        Button(
          onClick = onNew,
          colors = ButtonDefaults.buttonColors(containerColor = PanelAlt, contentColor = TextMain),
          shape = RoundedCornerShape(6.dp),
        ) {
          Text("+ New")
        }
      }
      Spacer(Modifier.height(8.dp))
      HorizontalDivider(color = Border)
      LazyColumn(modifier = Modifier.fillMaxSize().padding(top = 8.dp)) {
        items(state.chats, key = { it.id }) { chat ->
          val selected = chat.id == state.currentChatId
          Row(
            modifier =
              Modifier
                .fillMaxWidth()
                .clip(RoundedCornerShape(6.dp))
                .background(if (selected) PanelAlt else Panel)
                .clickable { onSelect(chat.id) }
                .padding(horizontal = 10.dp, vertical = 8.dp),
            verticalAlignment = Alignment.CenterVertically,
          ) {
            Text(chat.title, color = if (selected) TextMain else TextDim, modifier = Modifier.weight(1f), maxLines = 1)
            TextButton(onClick = { onRename(chat) }) { Text("Rename", fontSize = 12.sp) }
            TextButton(onClick = { onDelete(chat.id) }) { Text("×", color = Error, fontSize = 18.sp) }
          }
        }
      }
    }
  }
}

@Composable
private fun ContextBar(state: EmberUiState, onToggle: () -> Unit) {
  val pct = state.contextPercentLeft
  Row(
    modifier = Modifier.fillMaxWidth().background(Panel).padding(horizontal = 14.dp, vertical = 8.dp),
    verticalAlignment = Alignment.CenterVertically,
  ) {
    Text(state.model, color = Muted, style = MaterialTheme.typography.labelMedium)
    Spacer(Modifier.width(10.dp))
    LinearProgressIndicator(
      progress = { pct / 100f },
      modifier = Modifier.weight(1f).height(7.dp).clip(RoundedCornerShape(4.dp)),
      color = Accent,
      trackColor = PanelAlt,
    )
    Spacer(Modifier.width(10.dp))
    Text(
      "${formatTokenCount(state.contextUsed)} / ${formatTokenCount(state.contextWindow)} · $pct% left",
      color = Muted,
      style = MaterialTheme.typography.labelMedium,
    )
    val high = MaxContextWindows[state.model]
    if (high != null) {
      Spacer(Modifier.width(8.dp))
      TextButton(onClick = onToggle) {
        Text(if (state.contextWindow == high) "Using 1M" else "→ 1M")
      }
    }
  }
}

@Composable
private fun MessageList(messages: List<UiMessage>, livePhase: String, modifier: Modifier = Modifier) {
  val listState = rememberLazyListState()
  LaunchedEffect(messages.size, messages.lastOrNull()?.text?.length, messages.lastOrNull()?.reasoningSteps?.joinToString()?.length, livePhase) {
    if (messages.isNotEmpty()) listState.animateScrollToItem(messages.lastIndex)
  }
  if (messages.isEmpty()) {
    Box(modifier = modifier.fillMaxWidth().background(AppBg), contentAlignment = Alignment.Center) {
      Column(horizontalAlignment = Alignment.CenterHorizontally) {
        Text("✻", color = Accent, fontSize = 28.sp)
        Text("Send a message to start.", color = Muted)
      }
    }
    return
  }
  LazyColumn(
    state = listState,
    modifier = modifier.fillMaxWidth().background(AppBg),
    contentPadding = androidx.compose.foundation.layout.PaddingValues(18.dp),
    verticalArrangement = Arrangement.spacedBy(18.dp),
  ) {
    items(messages, key = { it.id }) { message -> MessageItem(message) }
  }
}

@Composable
private fun MessageItem(message: UiMessage) {
  val isUser = message.role == "user"
  val label =
    when (message.role) {
      "user" -> "You"
      "assistant" -> "Assistant"
      else -> "System"
    }
  Column(Modifier.fillMaxWidth()) {
    Text(label, color = if (isUser) Accent else if (message.error) Error else TextMain, fontWeight = FontWeight.Bold)
    Spacer(Modifier.height(6.dp))
    if (message.active && message.phase.isNotBlank()) {
      LivePhaseLine(message.phase)
    }
    if (message.reasoningSteps.any { it.isNotBlank() } || (message.active && message.phase == "reasoning")) {
      ReasoningBlock(message.reasoningSteps, live = message.active && message.phase == "reasoning")
    }
    if (message.text.isNotBlank()) {
      Surface(
        color = if (isUser) AppBgAlt else Color.Transparent,
        shape = RoundedCornerShape(6.dp),
        modifier = Modifier.fillMaxWidth(),
      ) {
        Column(Modifier.padding(if (isUser) 12.dp else 0.dp)) {
          MarkdownContent(message.text, color = if (message.error) Error else TextMain)
        }
      }
    } else if (message.active) {
      Surface(color = Color.Transparent, modifier = Modifier.fillMaxWidth()) {
        AnimatedPhaseText(message.phase.ifBlank { "connecting" }, modifier = Modifier.padding(top = 2.dp))
      }
    }
  }
}

@Composable
private fun ReasoningBlock(steps: List<String>, live: Boolean = false) {
  Column(Modifier.fillMaxWidth().padding(bottom = 10.dp)) {
    Text("▌ Reasoning", color = Accent, fontWeight = FontWeight.Bold, style = MaterialTheme.typography.labelMedium)
    steps.filter { it.isNotBlank() }.forEachIndexed { index, step ->
      Text("▸ step ${index + 1}", color = Accent, style = MaterialTheme.typography.labelMedium, modifier = Modifier.padding(top = 6.dp))
      Text(step, color = TextDim, fontStyle = FontStyle.Italic, modifier = Modifier.padding(start = 16.dp, top = 2.dp))
    }
    if (live && steps.none { it.isNotBlank() }) {
      AnimatedPhaseText("reasoning", modifier = Modifier.padding(start = 16.dp, top = 6.dp))
    }
  }
}

@Composable
private fun LivePhaseLine(phase: String) {
  Text(
    text = phase.replaceFirstChar { it.uppercase() },
    color = Muted,
    style = MaterialTheme.typography.labelMedium,
    modifier = Modifier.padding(bottom = 6.dp),
  )
}

@Composable
private fun AnimatedPhaseText(phase: String, modifier: Modifier = Modifier) {
  var tick by remember { mutableStateOf(0) }
  LaunchedEffect(phase) {
    tick = 0
    while (true) {
      delay(280)
      tick = (tick + 1) % 4
    }
  }
  Text(
    text = phase + ".".repeat(tick),
    color = Muted,
    fontStyle = FontStyle.Italic,
    modifier = modifier,
  )
}

@Composable
private fun Composer(
  state: EmberUiState,
  onModel: (String) -> Unit,
  onEffort: (String) -> Unit,
  onSend: (String) -> Unit,
  onStop: () -> Unit,
) {
  var text by rememberSaveable(state.currentChatId) { mutableStateOf("") }
  Column(Modifier.fillMaxWidth().background(AppBg).imePadding().padding(14.dp)) {
    OutlinedTextField(
      value = text,
      onValueChange = { text = it },
      modifier = Modifier.fillMaxWidth(),
      minLines = 2,
      maxLines = 6,
      placeholder = { Text("Message Ember", color = Muted) },
      colors =
        TextFieldDefaults.colors(
          focusedContainerColor = Panel,
          unfocusedContainerColor = Panel,
          focusedTextColor = TextMain,
          unfocusedTextColor = TextMain,
          focusedIndicatorColor = Border,
          unfocusedIndicatorColor = Border,
          cursorColor = Accent,
        ),
    )
    Spacer(Modifier.height(8.dp))
    Row(verticalAlignment = Alignment.CenterVertically, modifier = Modifier.fillMaxWidth()) {
      Picker("Model", state.model, ModelSpecs.keys.toList(), onModel)
      Spacer(Modifier.width(8.dp))
      Picker("Effort", state.effort, ModelSpecs[state.model].orEmpty(), onEffort)
      Spacer(Modifier.weight(1f))
      if (state.status.isNotBlank()) Text(state.status, color = Muted, style = MaterialTheme.typography.labelMedium)
      Spacer(Modifier.width(8.dp))
      Button(
        modifier = Modifier.width(132.dp),
        onClick = {
          if (state.streaming) {
            onStop()
          } else {
            onSend(text)
            text = ""
          }
        },
        contentPadding = PaddingValues(horizontal = 4.dp, vertical = 0.dp),
        colors = ButtonDefaults.buttonColors(containerColor = if (state.streaming) PanelAlt else Accent, contentColor = if (state.streaming) TextMain else AccentFg),
        shape = RoundedCornerShape(6.dp),
      ) {
        Text(
          if (state.streaming) "Stop" else "Send",
          fontSize = 16.sp,
          maxLines = 1,
          softWrap = false,
          overflow = TextOverflow.Clip,
        )
      }
    }
  }
}

@Composable
private fun Picker(label: String, selected: String, values: List<String>, onPick: (String) -> Unit) {
  var expanded by remember { mutableStateOf(false) }
  Box {
    Button(
      onClick = { expanded = true },
      colors = ButtonDefaults.buttonColors(containerColor = PanelAlt, contentColor = TextMain),
      shape = RoundedCornerShape(6.dp),
    ) {
      Text("$label  $selected")
    }
    DropdownMenu(expanded = expanded, onDismissRequest = { expanded = false }, containerColor = Panel) {
      values.forEach { value ->
        DropdownMenuItem(
          text = { Text(if (value == selected) "●  $value" else "   $value", color = TextMain) },
          onClick = {
            expanded = false
            onPick(value)
          },
        )
      }
    }
  }
}

@Composable
private fun SignInDialog(state: SignInUiState, onCancel: () -> Unit, onRetry: () -> Unit) {
  val context = LocalContext.current
  val clipboard = LocalClipboardManager.current
  AlertDialog(
    onDismissRequest = onCancel,
    containerColor = Panel,
    title = { Text("Sign in with ChatGPT", color = TextMain) },
    text = {
      Column {
        Text(state.phase, color = TextDim)
        state.deviceCode?.let { code ->
          Spacer(Modifier.height(14.dp))
          Text(code.userCode, color = Accent, fontSize = 28.sp, fontWeight = FontWeight.Bold, fontFamily = FontFamily.Monospace)
          Spacer(Modifier.height(8.dp))
          Text(code.verificationUrl, color = TextDim)
        }
        state.error?.let {
          Spacer(Modifier.height(10.dp))
          Text(it, color = Error)
        }
      }
    },
    confirmButton = {
      Row {
        state.deviceCode?.let { code ->
          TextButton(onClick = { clipboard.setText(AnnotatedString(code.userCode)) }) { Text("Copy code") }
          TextButton(onClick = { context.startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(code.verificationUrl))) }) { Text("Open browser") }
        }
        if (state.error != null) TextButton(onClick = onRetry) { Text("Retry") }
      }
    },
    dismissButton = { TextButton(onClick = onCancel) { Text("Cancel") } },
  )
}

@Composable
private fun RenameDialog(initial: String, onDismiss: () -> Unit, onSave: (String) -> Unit) {
  var value by rememberSaveable(initial) { mutableStateOf(initial) }
  AlertDialog(
    onDismissRequest = onDismiss,
    containerColor = Panel,
    title = { Text("Rename chat", color = TextMain) },
    text = {
      OutlinedTextField(
        value = value,
        onValueChange = { value = it },
        singleLine = true,
        colors =
          TextFieldDefaults.colors(
            focusedContainerColor = PanelAlt,
            unfocusedContainerColor = PanelAlt,
            focusedTextColor = TextMain,
            unfocusedTextColor = TextMain,
            focusedIndicatorColor = Accent,
            unfocusedIndicatorColor = Border,
          ),
      )
    },
    confirmButton = { TextButton(onClick = { onSave(value) }) { Text("Save") } },
    dismissButton = { TextButton(onClick = onDismiss) { Text("Cancel") } },
  )
}

@Composable
private fun MarkdownContent(markdown: String, color: Color) {
  val clipboard = LocalClipboardManager.current
  val lines = markdown.split("\n")
  var i = 0
  Column(verticalArrangement = Arrangement.spacedBy(4.dp)) {
    while (i < lines.size) {
      val line = lines[i]
      if (line.startsWith("```")) {
        val lang = line.removePrefix("```").trim().ifBlank { "code" }
        val code = StringBuilder()
        i += 1
        while (i < lines.size && !lines[i].startsWith("```")) {
          code.appendLine(lines[i])
          i += 1
        }
        CodeBlock(lang, code.toString().trimEnd(), onCopy = { clipboard.setText(AnnotatedString(code.toString().trimEnd())) })
      } else {
        when {
          line.startsWith("# ") -> Text(line.drop(2), color = color, fontSize = 24.sp, fontWeight = FontWeight.Bold)
          line.startsWith("## ") -> Text(line.drop(3), color = color, fontSize = 20.sp, fontWeight = FontWeight.Bold)
          line.startsWith("### ") -> Text(line.drop(4), color = color, fontSize = 18.sp, fontWeight = FontWeight.Bold)
          line.trim().startsWith("- ") || line.trim().startsWith("* ") -> Text("• ${line.trim().drop(2)}", color = color)
          line.isBlank() -> Spacer(Modifier.height(4.dp))
          else -> InlineMarkdownText(line, color)
        }
      }
      i += 1
    }
  }
}

@Composable
private fun CodeBlock(lang: String, code: String, onCopy: () -> Unit) {
  Surface(color = CodeBg, shape = RoundedCornerShape(6.dp), modifier = Modifier.fillMaxWidth().padding(vertical = 4.dp)) {
    Column(Modifier.padding(12.dp)) {
      Row(verticalAlignment = Alignment.CenterVertically) {
        Text(lang, color = Muted, fontFamily = FontFamily.Monospace, fontSize = 12.sp)
        Spacer(Modifier.weight(1f))
        TextButton(onClick = onCopy) { Text("Copy") }
      }
      SelectionContainer {
        Text(
          code,
          color = TextMain,
          fontFamily = FontFamily.Monospace,
          fontSize = 13.sp,
          modifier = Modifier.horizontalScroll(rememberScrollState()),
        )
      }
    }
  }
}

@Composable
private fun InlineMarkdownText(text: String, color: Color) {
  Text(
    inlineAnnotated(text, color),
    style = TextStyle(color = color, fontSize = 16.sp, lineHeight = 23.sp),
  )
}

private fun inlineAnnotated(text: String, color: Color): AnnotatedString =
  buildAnnotatedString {
    var i = 0
    while (i < text.length) {
      when {
        text.startsWith("**", i) -> {
          val end = text.indexOf("**", i + 2)
          if (end > i) {
            withStyle(SpanStyle(fontWeight = FontWeight.Bold, color = color)) { append(text.substring(i + 2, end)) }
            i = end + 2
          } else {
            append(text[i++])
          }
        }
        text[i] == '`' -> {
          val end = text.indexOf('`', i + 1)
          if (end > i) {
            withStyle(SpanStyle(fontFamily = FontFamily.Monospace, color = Accent, background = CodeBg)) { append(text.substring(i + 1, end)) }
            i = end + 1
          } else {
            append(text[i++])
          }
        }
        text[i] == '*' -> {
          val end = text.indexOf('*', i + 1)
          if (end > i) {
            withStyle(SpanStyle(fontStyle = FontStyle.Italic, color = color)) { append(text.substring(i + 1, end)) }
            i = end + 1
          } else {
            append(text[i++])
          }
        }
        else -> append(text[i++])
      }
    }
  }
