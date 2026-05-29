<script lang="ts">
  import { onDestroy, onMount, tick } from "svelte";
  import { invoke } from "@tauri-apps/api/core";
  import { listen, type UnlistenFn } from "@tauri-apps/api/event";
  import { openUrl } from "@tauri-apps/plugin-opener";

  type MessagePart = { type: string; text: string };
  type Message = { role: string; content: MessagePart[] };
  type Chat = {
    id: string;
    title: string;
    model: string;
    effort: string;
    created_at: string;
    updated_at: string;
    title_generated: boolean;
    last_total_tokens: number;
    context_window_override?: number | null;
  };
  type ModelCatalog = {
    models: Record<string, string[]>;
    groups: Array<[string, string[]]>;
    defaults: string[];
    default_model: string;
    default_effort: string;
  };
  type StreamEvent =
    | { type: "user"; text?: string; chatId?: string }
    | { type: "delta"; text?: string }
    | { type: "reasoning"; text?: string }
    | { type: "phase"; phase?: string }
    | { type: "reasoning_step"; phase?: string }
    | { type: "usage"; total_tokens?: number }
    | { type: "error"; text?: string }
    | { type: "done" };
  type AuthStatus = {
    signed_in: boolean;
    email?: string | null;
    account_id?: string | null;
    plan_type?: string | null;
  };
  type DeviceCode = {
    verification_url: string;
    user_code: string;
    device_auth_id: string;
    interval: number;
  };
  type UiMessage = {
    role: string;
    text: string;
    reasoning?: string[];
  };
  type MarkdownBlock =
    | { type: "heading"; level: number; text: string }
    | { type: "paragraph"; text: string }
    | { type: "quote"; text: string }
    | { type: "ul"; items: string[] }
    | { type: "ol"; items: string[] }
    | { type: "code"; lang: string; code: string };

  const BASELINE_TOKENS = 12_000;
  const CONTEXT_WINDOWS: Record<string, number> = {
    "gpt-5.5": 272_000,
    "gpt-5.4": 272_000,
    "gpt-5.4-mini": 272_000,
    "gpt-5.3-codex": 272_000,
    "gpt-5.3-codex-spark": 128_000,
    "gpt-5.2": 272_000,
  };
  const MAX_CONTEXT_WINDOWS: Record<string, number> = {
    "gpt-5.5": 1_000_000,
    "gpt-5.4": 1_000_000,
  };

  let catalog: ModelCatalog | null = null;
  let chats: Chat[] = [];
  let activeChatId = "";
  let messages: Message[] = [];
  let uiMessages: UiMessage[] = [];
  let selectedModel = "gpt-5.4";
  let selectedEffort = "medium";
  let prompt = "";
  let status = "";
  let loading = false;
  let auth: AuthStatus = { signed_in: false, email: null };
  let signInBusy = false;
  let deviceCode: DeviceCode | null = null;
  let isStreaming = false;
  let streamingText = "";
  let reasoningSteps: string[] = [];
  let activeReasoningIndex = -1;
  let phase = "";
  let lastUsageTotal = 0;
  let unlisten: UnlistenFn | null = null;
  let chatLog: HTMLElement;

  $: activeChat = chats.find((chat) => chat.id === activeChatId) ?? null;
  $: visibleContextWindow = effectiveContextWindow(activeChat, selectedModel);
  $: visibleTokenTotal = activeChat?.last_total_tokens || lastUsageTotal || 0;
  $: percentLeft = percentOfContextRemaining(visibleTokenTotal, visibleContextWindow);
  $: contextSegments = Array.from({ length: 20 }, (_, i) => i < Math.round((percentLeft / 100) * 20));

  const modelEfforts = (model: string) => catalog?.models[model] ?? ["low", "medium", "high", "xhigh"];

  $: if (catalog && !modelEfforts(selectedModel).includes(selectedEffort)) {
    selectedEffort = modelEfforts(selectedModel)[0] ?? catalog.default_effort;
  }

  onMount(async () => {
    unlisten = await listen<StreamEvent>("chat-event", (event) => handleStreamEvent(event.payload));
    await initialize();
  });

  onDestroy(() => unlisten?.());

  async function initialize() {
    catalog = await invoke("get_model_catalog");
    auth = await invoke("get_auth_status");
    if (catalog) {
      selectedModel = catalog.default_model;
      selectedEffort = catalog.default_effort;
    }
    await loadChats();
  }

  async function loadChats() {
    chats = await invoke("list_chats_cmd");
    if (activeChatId && !chats.some((chat) => chat.id === activeChatId)) activeChatId = "";
    if (!activeChatId && chats.length > 0) await openChat(chats[0].id);
  }

  async function openChat(chatId: string) {
    if (loading) await stopStream();
    activeChatId = chatId;
    messages = await invoke("load_messages_cmd", { chatId });
    const chat = chats.find((item) => item.id === chatId);
    selectedModel = chat?.model ?? selectedModel;
    selectedEffort = chat?.effort ?? selectedEffort;
    lastUsageTotal = chat?.last_total_tokens ?? 0;
    uiMessages = messages.map((message) => ({ role: message.role, text: toTextItems(message.content) }));
    await scrollToBottom();
  }

  async function createChat() {
    const chat = await invoke<Chat>("create_chat_cmd", { model: selectedModel, effort: selectedEffort });
    chats = [chat, ...chats];
    activeChatId = chat.id;
    messages = [];
    uiMessages = [];
    lastUsageTotal = 0;
    status = "";
  }

  async function deleteChat(chatId: string) {
    const chat = chats.find((item) => item.id === chatId);
    if (!chat || !confirm(`Delete "${chat.title}"?`)) return;
    await invoke("delete_chat_cmd", { chatId });
    if (activeChatId === chatId) {
      activeChatId = "";
      uiMessages = [];
      messages = [];
    }
    await loadChats();
  }

  async function renameChat(chatId: string) {
    const chat = chats.find((item) => item.id === chatId);
    if (!chat) return;
    const title = promptDialog("Rename chat", chat.title);
    if (!title) return;
    await invoke("rename_chat_cmd", { chatId, title, generated: false });
    await loadChats();
  }

  async function persistModelSettings() {
    if (!activeChatId) return;
    await invoke("update_chat_model_cmd", {
      chatId: activeChatId,
      model: selectedModel,
      effort: selectedEffort,
    });
    await loadChats();
  }

  async function toggleContextWindow() {
    if (!activeChatId || !MAX_CONTEXT_WINDOWS[selectedModel]) return;
    await invoke("toggle_context_window_cmd", { chatId: activeChatId });
    await loadChats();
  }

  async function startSignIn() {
    signInBusy = true;
    status = "requesting device code";
    try {
      const code = await invoke<DeviceCode>("request_device_code_cmd");
      deviceCode = code;
      status = "waiting for browser approval";
      await openUrl(code.verification_url);
      auth = await invoke("wait_device_authorization_cmd", { code });
      status = "signed in";
      deviceCode = null;
    } catch (err) {
      status = `sign-in failed: ${String(err)}`;
    } finally {
      signInBusy = false;
    }
  }

  async function copyDeviceCode() {
    if (!deviceCode) return;
    await navigator.clipboard.writeText(deviceCode.user_code);
    status = "code copied";
  }

  async function signOut() {
    await invoke("clear_tokens_cmd");
    auth = { signed_in: false, email: null };
    status = "signed out";
  }

  async function send() {
    const text = prompt.trim();
    if (!text || loading) return;
    if (!auth.signed_in) {
      await startSignIn();
      if (!auth.signed_in) return;
    }
    if (!activeChatId) await createChat();
    if (!activeChatId) return;

    prompt = "";
    status = "connecting";
    loading = true;
    isStreaming = false;
    streamingText = "";
    reasoningSteps = [];
    activeReasoningIndex = -1;
    phase = "connecting";
    uiMessages = [...uiMessages, { role: "user", text }];
    await scrollToBottom();

    try {
      await invoke("send_message", {
        chatId: activeChatId,
        text,
        model: selectedModel,
        effort: selectedEffort,
      });
      await loadChats();
      await openChat(activeChatId);
      status = "";
    } catch (err) {
      status = "failed";
      uiMessages = [...uiMessages, { role: "error", text: String(err) }];
    } finally {
      loading = false;
      isStreaming = false;
      streamingText = "";
      reasoningSteps = [];
      activeReasoningIndex = -1;
      phase = "";
    }
  }

  async function stopStream() {
    if (!loading) return;
    await invoke("stop_stream");
    phase = "stopping";
    status = "stopping";
  }

  function handleStreamEvent(payload: StreamEvent) {
    if (!payload || typeof payload !== "object") return;
    if (payload.type === "phase" && payload.phase) {
      phase = payload.phase;
      status = payload.phase;
      return;
    }
    if (payload.type === "reasoning_step") {
      if (payload.phase === "start" || activeReasoningIndex < 0) {
        reasoningSteps = [...reasoningSteps, ""];
        activeReasoningIndex = reasoningSteps.length - 1;
        syncReasoning();
      } else if (payload.phase === "end") {
        activeReasoningIndex = -1;
      }
      return;
    }
    if (payload.type === "reasoning" && typeof payload.text === "string") {
      if (activeReasoningIndex < 0) {
        reasoningSteps = [...reasoningSteps, ""];
        activeReasoningIndex = reasoningSteps.length - 1;
      }
      const next = [...reasoningSteps];
      next[activeReasoningIndex] = `${next[activeReasoningIndex] ?? ""}${payload.text}`;
      reasoningSteps = next;
      syncReasoning();
      void scrollToBottom();
      return;
    }
    if (payload.type === "delta" && typeof payload.text === "string") {
      streamingText += payload.text;
      if (!isStreaming) {
        isStreaming = true;
        uiMessages = [...uiMessages, { role: "assistant", text: streamingText, reasoning: reasoningSteps }];
      } else {
        updateLastAssistant({ text: streamingText, reasoning: reasoningSteps });
      }
      void scrollToBottom();
      return;
    }
    if (payload.type === "usage" && typeof payload.total_tokens === "number") {
      lastUsageTotal = payload.total_tokens;
      return;
    }
    if (payload.type === "error" && typeof payload.text === "string") {
      isStreaming = false;
      uiMessages = [...uiMessages, { role: "error", text: payload.text }];
      void scrollToBottom();
      return;
    }
    if (payload.type === "done") {
      isStreaming = false;
      status = "";
    }
  }

  function syncReasoning() {
    const idx = uiMessages.length - 1;
    if (idx >= 0 && uiMessages[idx]?.role === "assistant") {
      updateLastAssistant({ reasoning: reasoningSteps });
    }
  }

  function updateLastAssistant(update: Partial<UiMessage>) {
    const next = [...uiMessages];
    const idx = next.length - 1;
    if (idx >= 0 && next[idx]?.role === "assistant") {
      next[idx] = { ...next[idx], ...update };
      uiMessages = next;
    } else {
      uiMessages = [...next, { role: "assistant", text: update.text ?? "", reasoning: update.reasoning }];
    }
  }

  function toTextItems(items: MessagePart[]): string {
    return items.map((part) => part.text).join("");
  }

  function effectiveContextWindow(chat: Chat | null, model: string): number {
    const base = CONTEXT_WINDOWS[model] ?? 272_000;
    const max = MAX_CONTEXT_WINDOWS[model];
    if (chat?.context_window_override && max && chat.context_window_override === max) return max;
    return base;
  }

  function formatTokenCount(n: number): string {
    if (n >= 1_000_000) return `${Number((n / 1_000_000).toFixed(1))}M`;
    if (n >= 1_000) return `${Number((n / 1_000).toFixed(1))}k`;
    return `${n}`;
  }

  function percentOfContextRemaining(totalTokens: number, contextWindow: number): number {
    if (contextWindow <= BASELINE_TOKENS) return 0;
    const effective = contextWindow - BASELINE_TOKENS;
    const used = Math.max(0, totalTokens - BASELINE_TOKENS);
    const remaining = Math.max(0, effective - used);
    return Math.max(0, Math.min(100, Math.round((remaining / effective) * 100)));
  }

  function parseMarkdown(text: string): MarkdownBlock[] {
    const lines = text.replace(/\r\n/g, "\n").split("\n");
    const blocks: MarkdownBlock[] = [];
    let paragraph: string[] = [];
    let listItems: string[] = [];
    let listType: "ul" | "ol" | null = null;
    let inCode = false;
    let codeLang = "";
    let codeLines: string[] = [];

    const flushParagraph = () => {
      if (paragraph.length) {
        blocks.push({ type: "paragraph", text: paragraph.join("\n") });
        paragraph = [];
      }
    };
    const flushList = () => {
      if (listType && listItems.length) blocks.push({ type: listType, items: listItems });
      listType = null;
      listItems = [];
    };

    for (const line of lines) {
      const fence = line.match(/^```(.*)$/);
      if (fence) {
        if (inCode) {
          blocks.push({ type: "code", lang: codeLang, code: codeLines.join("\n") });
          inCode = false;
          codeLang = "";
          codeLines = [];
        } else {
          flushParagraph();
          flushList();
          inCode = true;
          codeLang = fence[1]?.trim() ?? "";
        }
        continue;
      }
      if (inCode) {
        codeLines.push(line);
        continue;
      }
      if (!line.trim()) {
        flushParagraph();
        flushList();
        continue;
      }
      const heading = line.match(/^(#{1,3})\s+(.+)$/);
      if (heading) {
        flushParagraph();
        flushList();
        blocks.push({ type: "heading", level: heading[1].length, text: heading[2] });
        continue;
      }
      const bullet = line.match(/^\s*[-*]\s+(.+)$/);
      if (bullet) {
        flushParagraph();
        if (listType !== "ul") flushList();
        listType = "ul";
        listItems.push(bullet[1]);
        continue;
      }
      const ordered = line.match(/^\s*\d+\.\s+(.+)$/);
      if (ordered) {
        flushParagraph();
        if (listType !== "ol") flushList();
        listType = "ol";
        listItems.push(ordered[1]);
        continue;
      }
      const quote = line.match(/^>\s?(.+)$/);
      if (quote) {
        flushParagraph();
        flushList();
        blocks.push({ type: "quote", text: quote[1] });
        continue;
      }
      flushList();
      paragraph.push(line);
    }
    if (inCode) blocks.push({ type: "code", lang: codeLang, code: codeLines.join("\n") });
    flushParagraph();
    flushList();
    return blocks.length ? blocks : [{ type: "paragraph", text }];
  }

  function inlineHtml(text: string): string {
    return escapeHtml(text)
      .replace(/`([^`]+)`/g, "<code>$1</code>")
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
      .replace(/\*([^*]+)\*/g, "<em>$1</em>");
  }

  function escapeHtml(text: string): string {
    return text
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#039;");
  }

  async function copyText(text: string) {
    await navigator.clipboard.writeText(text);
    status = "copied";
    setTimeout(() => {
      if (status === "copied") status = "";
    }, 900);
  }

  async function scrollToBottom() {
    await tick();
    if (chatLog) chatLog.scrollTop = chatLog.scrollHeight;
  }

  function promptDialog(title: string, current: string): string {
    return (window.prompt(title, current) ?? "").trim();
  }
</script>

<main class="app-shell">
  <aside class="sidebar">
    <div class="brand-row">
      <div>
        <h1>Ember</h1>
        <p>{auth.signed_in ? auth.email ?? "Signed in" : "Not signed in"}</p>
      </div>
      {#if auth.signed_in}
        <button class="ghost" on:click={signOut}>Sign out</button>
      {:else}
        <button on:click={startSignIn} disabled={signInBusy}>Sign in</button>
      {/if}
    </div>

    {#if deviceCode}
      <section class="signin-box">
        <span>Use this code at ChatGPT</span>
        <button class="code-button" on:click={copyDeviceCode}>{deviceCode.user_code}</button>
        <button class="ghost" on:click={() => openUrl(deviceCode?.verification_url ?? "")}>Open browser</button>
      </section>
    {/if}

    <div class="sidebar-actions">
      <button on:click={createChat}>New chat</button>
    </div>

    <nav class="chat-list" aria-label="Chats">
      {#each chats as chat}
        <div class:active={chat.id === activeChatId} class="chat-row">
          <button class="chat-open" on:click={() => openChat(chat.id)} on:dblclick={() => renameChat(chat.id)}>
            <span>{chat.title}</span>
            <small>{chat.model}</small>
          </button>
          <button class="icon-button" title="Rename" on:click={() => renameChat(chat.id)}>R</button>
          <button class="icon-button danger" title="Delete" on:click={() => deleteChat(chat.id)}>X</button>
        </div>
      {/each}
    </nav>
  </aside>

  <section class="workspace">
    <header class="toolbar">
      <div class="context-bar">
        <span>{selectedModel}</span>
        <div class="segments" aria-label="Context remaining">
          {#each contextSegments as filled}
            <i class:filled></i>
          {/each}
        </div>
        <span>{formatTokenCount(visibleTokenTotal)} / {formatTokenCount(visibleContextWindow)} · {percentLeft}% left</span>
        {#if MAX_CONTEXT_WINDOWS[selectedModel]}
          <button class="ghost compact" on:click={toggleContextWindow}>
            {visibleContextWindow === MAX_CONTEXT_WINDOWS[selectedModel] ? "Using 1M" : "1M context"}
          </button>
        {/if}
      </div>

      <div class="selectors">
        <label>
          Model
          <select bind:value={selectedModel} on:change={persistModelSettings} disabled={loading}>
            {#if catalog}
              {#each catalog.groups as group}
                <optgroup label={group[0]}>
                  {#each group[1] as model}
                    <option value={model}>{model}</option>
                  {/each}
                </optgroup>
              {/each}
            {/if}
          </select>
        </label>
        <label>
          Effort
          <select bind:value={selectedEffort} on:change={persistModelSettings} disabled={loading}>
            {#each modelEfforts(selectedModel) as effort}
              <option value={effort}>{effort}</option>
            {/each}
          </select>
        </label>
      </div>
    </header>

    <div class="status-line">
      {#if loading}
        <span>{phase || "streaming"}</span>
      {:else if status}
        <span>{status}</span>
      {/if}
    </div>

    <section class="chat-log" bind:this={chatLog}>
      {#if uiMessages.length === 0}
        <div class="empty-state">Create or select a chat, then send a message.</div>
      {/if}

      {#each uiMessages as message}
        <article class={`message ${message.role}`}>
          <div class="message-label">{message.role}</div>
          {#if message.reasoning?.length}
            <details class="reasoning" open>
              <summary>Reasoning</summary>
              {#each message.reasoning as step, i}
                {#if step.trim()}
                  <p><span>step {i + 1}</span>{@html inlineHtml(step)}</p>
                {/if}
              {/each}
            </details>
          {/if}

          <div class="markdown">
            {#each parseMarkdown(message.text) as block}
              {#if block.type === "heading"}
                <svelte:element this={`h${block.level + 1}`}>{block.text}</svelte:element>
              {:else if block.type === "paragraph"}
                <p>{@html inlineHtml(block.text)}</p>
              {:else if block.type === "quote"}
                <blockquote>{@html inlineHtml(block.text)}</blockquote>
              {:else if block.type === "ul"}
                <ul>{#each block.items as item}<li>{@html inlineHtml(item)}</li>{/each}</ul>
              {:else if block.type === "ol"}
                <ol>{#each block.items as item}<li>{@html inlineHtml(item)}</li>{/each}</ol>
              {:else if block.type === "code"}
                <div class="code-block">
                  <div class="code-head">
                    <span>{block.lang || "code"}</span>
                    <button class="ghost compact" on:click={() => copyText(block.code)}>Copy</button>
                  </div>
                  <pre><code>{block.code}</code></pre>
                </div>
              {/if}
            {/each}
          </div>
        </article>
      {/each}
    </section>

    <form class="composer" on:submit|preventDefault={send}>
      <textarea
        placeholder={auth.signed_in ? "Ask Ember..." : "Sign in to start chatting..."}
        bind:value={prompt}
        rows="4"
        disabled={loading}
        on:keydown={(event) => {
          if (event.key === "Enter" && !event.shiftKey) {
            event.preventDefault();
            void send();
          }
        }}
      ></textarea>
      {#if loading}
        <button type="button" class="stop" on:click={stopStream}>Stop</button>
      {:else}
        <button type="submit" disabled={!prompt.trim()}>{auth.signed_in ? "Send" : "Sign in"}</button>
      {/if}
    </form>
  </section>
</main>

<style>
  :global(body) {
    margin: 0;
    font-family: Inter, "Segoe UI", Arial, sans-serif;
    background: #0d0e10;
    color: #f2f2f2;
  }

  button,
  select,
  textarea {
    font: inherit;
  }

  button {
    border: 0;
    border-radius: 8px;
    padding: 0.55rem 0.75rem;
    background: #e38b56;
    color: #1a1208;
    cursor: pointer;
  }

  button:disabled {
    cursor: default;
    opacity: 0.55;
  }

  .ghost {
    background: #24262b;
    color: #f2f2f2;
  }

  .compact {
    padding: 0.3rem 0.55rem;
    font-size: 0.8rem;
  }

  .app-shell {
    display: grid;
    grid-template-columns: 300px minmax(0, 1fr);
    height: 100vh;
    overflow: hidden;
  }

  .sidebar {
    border-right: 1px solid #2c2d32;
    background: #17181b;
    display: flex;
    flex-direction: column;
    min-height: 0;
  }

  .brand-row {
    display: flex;
    align-items: start;
    justify-content: space-between;
    gap: 0.75rem;
    padding: 1rem;
    border-bottom: 1px solid #2c2d32;
  }

  h1 {
    margin: 0;
    font-size: 1.15rem;
  }

  .brand-row p,
  .status-line,
  small {
    color: #92939a;
  }

  .brand-row p {
    margin: 0.25rem 0 0;
    font-size: 0.82rem;
    word-break: break-word;
  }

  .signin-box {
    display: grid;
    gap: 0.5rem;
    padding: 0.85rem 1rem;
    border-bottom: 1px solid #2c2d32;
    background: #101014;
  }

  .signin-box span {
    color: #c9c9cc;
    font-size: 0.85rem;
  }

  .code-button {
    font-family: "Cascadia Mono", Consolas, monospace;
    font-size: 1.2rem;
    letter-spacing: 0.08em;
  }

  .sidebar-actions {
    padding: 0.75rem 1rem;
  }

  .sidebar-actions button {
    width: 100%;
  }

  .chat-list {
    overflow: auto;
    padding: 0.35rem 0.5rem 1rem;
  }

  .chat-row {
    display: grid;
    grid-template-columns: minmax(0, 1fr) 32px 32px;
    gap: 0.25rem;
    align-items: center;
    border-radius: 8px;
    padding: 0.25rem;
  }

  .chat-row.active {
    background: #24262b;
  }

  .chat-open {
    min-width: 0;
    background: transparent;
    color: #f2f2f2;
    text-align: left;
    display: grid;
    gap: 0.15rem;
  }

  .chat-open span {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .icon-button {
    width: 32px;
    height: 32px;
    padding: 0;
    background: transparent;
    color: #92939a;
  }

  .icon-button:hover {
    background: #2a2b2f;
    color: #fff;
  }

  .icon-button.danger:hover {
    color: #ffb3b3;
  }

  .workspace {
    min-width: 0;
    min-height: 0;
    display: grid;
    grid-template-rows: auto 24px minmax(0, 1fr) auto;
    background: #0d0e10;
  }

  .toolbar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 1rem;
    padding: 0.75rem 1rem;
    border-bottom: 1px solid #2c2d32;
    background: #101014;
  }

  .context-bar,
  .selectors {
    display: flex;
    align-items: center;
    gap: 0.75rem;
    flex-wrap: wrap;
  }

  .context-bar {
    color: #c9c9cc;
    font-size: 0.86rem;
  }

  .segments {
    display: grid;
    grid-template-columns: repeat(20, 7px);
    gap: 2px;
  }

  .segments i {
    width: 7px;
    height: 14px;
    border-radius: 2px;
    background: #34363c;
  }

  .segments i.filled {
    background: #e38b56;
  }

  label {
    color: #92939a;
    display: grid;
    gap: 0.25rem;
    font-size: 0.78rem;
  }

  select,
  textarea {
    border: 1px solid #34363c;
    border-radius: 8px;
    background: #17181b;
    color: #f2f2f2;
  }

  select {
    padding: 0.45rem 0.6rem;
  }

  .status-line {
    padding: 0.25rem 1rem;
    font-size: 0.82rem;
  }

  .chat-log {
    overflow: auto;
    padding: 0.75rem 1rem 1.25rem;
  }

  .empty-state {
    color: #92939a;
    padding: 2rem 0;
    text-align: center;
  }

  .message {
    max-width: 980px;
    margin: 0 auto 1.1rem;
  }

  .message.user {
    max-width: 760px;
    margin-left: auto;
    margin-right: 0;
    background: #151820;
    border: 1px solid #2c2d32;
    border-radius: 8px;
    padding: 0.7rem 0.8rem;
  }

  .message.error {
    color: #ffb3b3;
  }

  .message-label {
    color: #92939a;
    font-size: 0.78rem;
    margin-bottom: 0.35rem;
    text-transform: capitalize;
  }

  .reasoning {
    border-left: 3px solid #4f5665;
    color: #c9c9cc;
    margin: 0 0 0.85rem;
    padding-left: 0.75rem;
  }

  .reasoning summary {
    cursor: pointer;
    color: #92939a;
    margin-bottom: 0.35rem;
  }

  .reasoning p {
    margin: 0.35rem 0;
    font-style: italic;
  }

  .reasoning span {
    color: #92939a;
    display: inline-block;
    min-width: 3.8rem;
    font-style: normal;
  }

  .markdown {
    line-height: 1.55;
    white-space: normal;
  }

  .markdown :global(p) {
    margin: 0.45rem 0;
    white-space: pre-wrap;
  }

  .markdown h2,
  .markdown h3,
  .markdown h4 {
    margin: 0.8rem 0 0.4rem;
  }

  .markdown blockquote {
    margin: 0.6rem 0;
    padding-left: 0.8rem;
    border-left: 3px solid #34363c;
    color: #c9c9cc;
  }

  .markdown ul,
  .markdown ol {
    margin: 0.5rem 0;
    padding-left: 1.4rem;
  }

  .markdown :global(code) {
    background: #202228;
    border-radius: 4px;
    padding: 0.1rem 0.28rem;
    font-family: "Cascadia Mono", Consolas, monospace;
    font-size: 0.92em;
  }

  .code-block {
    border: 1px solid #2c2d32;
    border-radius: 8px;
    overflow: hidden;
    margin: 0.65rem 0;
    background: #0f1013;
  }

  .code-head {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 0.35rem 0.5rem;
    border-bottom: 1px solid #2c2d32;
    color: #92939a;
    font-size: 0.78rem;
  }

  pre {
    margin: 0;
    padding: 0.75rem;
    overflow: auto;
  }

  pre code {
    background: transparent;
    padding: 0;
  }

  .composer {
    display: grid;
    grid-template-columns: minmax(0, 1fr) 104px;
    gap: 0.75rem;
    padding: 0.75rem 1rem 1rem;
    border-top: 1px solid #2c2d32;
    background: #101014;
  }

  textarea {
    resize: vertical;
    min-height: 74px;
    max-height: 220px;
    padding: 0.75rem;
  }

  .stop {
    background: #34363c;
    color: #fff;
  }

  @media (max-width: 780px) {
    .app-shell {
      grid-template-columns: 1fr;
      grid-template-rows: 240px minmax(0, 1fr);
    }

    .sidebar {
      border-right: 0;
      border-bottom: 1px solid #2c2d32;
    }

    .toolbar {
      align-items: stretch;
      flex-direction: column;
    }

    .composer {
      grid-template-columns: 1fr;
    }
  }
</style>
