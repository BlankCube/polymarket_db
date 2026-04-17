/* ===========================================================
   Polymarket Explorer — client code
   Vanilla JS. No build step, no dependencies.
   =========================================================== */

(() => {
  "use strict";

  // crypto.randomUUID() is only exposed in secure contexts (HTTPS / localhost).
  // This app is commonly accessed over plain HTTP via an IP, where
  // `crypto.randomUUID` is undefined and calling it throws TypeError at
  // module init, killing the whole IIFE before any listener binds.
  //
  // The polyfill uses crypto.getRandomValues (available on HTTP) to build an
  // RFC 4122 v4 UUID. Final fallback is Math.random — cryptographically weak
  // but fine for a client-side session ID.
  function randomUUID() {
    try {
      if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
        return crypto.randomUUID();
      }
      if (typeof crypto !== "undefined" && typeof crypto.getRandomValues === "function") {
        const b = crypto.getRandomValues(new Uint8Array(16));
        b[6] = (b[6] & 0x0f) | 0x40; // version 4
        b[8] = (b[8] & 0x3f) | 0x80; // variant
        const hex = Array.from(b, (x) => x.toString(16).padStart(2, "0")).join("");
        return hex.slice(0, 8) + "-" + hex.slice(8, 12) + "-" +
               hex.slice(12, 16) + "-" + hex.slice(16, 20) + "-" + hex.slice(20, 32);
      }
    } catch (_) { /* fall through */ }
    return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, (c) => {
      const r = (Math.random() * 16) | 0;
      return (c === "x" ? r : (r & 0x3) | 0x8).toString(16);
    });
  }

  // Surface any init-time error as a visible banner so the UI never
  // silently becomes a dead form.
  window.addEventListener("error", (ev) => {
    const existing = document.getElementById("js-boot-error");
    if (existing) return;
    const banner = document.createElement("div");
    banner.id = "js-boot-error";
    banner.textContent = "Frontend error: " + (ev.message || ev.error || "unknown");
    banner.style.cssText = "position:fixed;top:0;left:0;right:0;z-index:9999;"
      + "padding:10px 14px;background:#5a2020;color:#fff;font:13px/1.4 sans-serif;"
      + "border-bottom:1px solid #ff8080;";
    document.body && document.body.prepend(banner);
  });

  // ---------- State ----------
  const state = {
    token: localStorage.getItem("pm_token") || null,
    user: localStorage.getItem("pm_user") || null,
    sessionId: randomUUID(),
    history: [],        // [{role, content}]
    busy: false,
    abortController: null,
  };

  // ---------- Utility ----------
  const $ = (id) => document.getElementById(id);

  function authHeaders() {
    const h = { "Content-Type": "application/json" };
    if (state.token) h.Authorization = "Bearer " + state.token;
    return h;
  }

  async function api(path, opts = {}) {
    const res = await fetch(path, {
      headers: authHeaders(),
      ...opts,
    });
    return res.json();
  }

  function userIdFromToken() {
    if (!state.token) return null;
    try {
      const payload = state.token.split(".")[1];
      return JSON.parse(atob(payload)).user_id || null;
    } catch { return null; }
  }

  // Safely create an element with text content and optional attrs.
  function el(tag, opts = {}, ...children) {
    const node = document.createElement(tag);
    if (opts.className) node.className = opts.className;
    if (opts.text != null) node.textContent = opts.text;
    if (opts.attrs) for (const k in opts.attrs) node.setAttribute(k, opts.attrs[k]);
    if (opts.hidden) node.hidden = true;
    for (const c of children) if (c) node.appendChild(c);
    return node;
  }

  // ---------- Auth ----------
  const authForm = $("auth-form");
  const authErr = $("auth-err");
  const btnLogin = $("btn-login");
  const btnRegister = $("btn-register");

  async function authSubmit(endpoint) {
    authErr.textContent = "";
    const username = $("auth-username").value.trim();
    const password = $("auth-password").value;
    if (!username || !password) {
      authErr.textContent = "Username and password required.";
      return;
    }
    try {
      const d = await api(endpoint, {
        method: "POST",
        body: JSON.stringify({ username, password }),
      });
      if (d.status === "ok") {
        state.token = d.token;
        state.user = d.username;
        localStorage.setItem("pm_token", d.token);
        localStorage.setItem("pm_user", d.username);
        enterApp();
      } else {
        authErr.textContent = d.message || "Login failed.";
      }
    } catch (e) {
      authErr.textContent = "Network error: " + e.message;
    }
  }

  authForm.addEventListener("submit", (e) => {
    e.preventDefault();
    authSubmit("/api/login");
  });
  btnRegister.addEventListener("click", () => authSubmit("/api/register"));

  $("btn-logout").addEventListener("click", () => {
    localStorage.removeItem("pm_token");
    localStorage.removeItem("pm_user");
    state.token = null;
    location.reload();
  });

  function enterApp() {
    $("auth").hidden = true;
    $("sidebar").hidden = false;
    $("main").hidden = false;
    $("btn-menu").hidden = false;
    $("current-user").textContent = state.user || "";
    loadSessionList();
  }

  // ---------- Sessions ----------

  // Build a DOM item for a persisted session row returned by /api/sessions.
  function persistedSessionItem(s) {
    const item = el("div", {
      className: "session-item" + (s.session_id === state.sessionId ? " active" : ""),
      text: (s.topic_summary || "Untitled").slice(0, 50),
      attrs: { role: "button", tabindex: "0", "data-session-id": s.session_id },
    });
    item.appendChild(el("small", {
      text: s.started_at ? new Date(s.started_at).toLocaleDateString() : "",
    }));
    item.addEventListener("click", () => openSession(s.session_id));
    return item;
  }

  // Build a DOM item for the current not-yet-persisted session (no messages
  // sent yet). Sits at the top of the list and is always active. Clicking
  // does nothing since we are already in it.
  function syntheticNewSessionItem() {
    const item = el("div", {
      className: "session-item active",
      text: "New session",
      attrs: { role: "button", tabindex: "0", "data-session-id": state.sessionId },
    });
    item.appendChild(el("small", { text: "just now" }));
    // No click handler — user is already in this session.
    return item;
  }

  async function loadSessionList() {
    const list = $("session-list");
    list.replaceChildren();
    let sessions = [];
    try {
      const d = await api("/api/sessions");
      sessions = Array.isArray(d.sessions) ? d.sessions.filter(s => s.session_id) : [];
    } catch (e) {
      console.error("loadSessionList failed", e);
    }

    // If the active session hasn't been persisted yet (user just landed or
    // just clicked "+ New" without sending anything), prepend a synthetic
    // placeholder so they can see where they are.
    const alreadyListed = sessions.some(s => s.session_id === state.sessionId);
    if (!alreadyListed) {
      list.appendChild(syntheticNewSessionItem());
    }

    for (const s of sessions) {
      list.appendChild(persistedSessionItem(s));
    }
  }

  $("btn-new").addEventListener("click", () => newSession());

  function newSession() {
    state.sessionId = randomUUID();
    state.history = [];
    $("chat").replaceChildren();
    $("examples").hidden = false;
    $("stage-indicator").hidden = true;
    closeSidebarIfMobile();
    loadSessionList();
  }

  async function openSession(id) {
    try {
      const d = await api("/api/sessions/" + encodeURIComponent(id));
      if (d.status !== "ok") return;
      state.sessionId = d.session_id;
      let conv = d.conversation;
      if (typeof conv === "string") conv = JSON.parse(conv);
      state.history = Array.isArray(conv) ? conv : [];
      const chat = $("chat");
      chat.replaceChildren();
      $("examples").hidden = true;
      for (const m of state.history) {
        const msg = el("div", {
          className: m.role === "user" ? "msg msg-user" : "msg msg-ai",
          text: m.content,
        });
        if (m.role === "assistant") msg.appendChild(feedbackRow(state.sessionId));
        chat.appendChild(msg);
      }
      chat.scrollTop = chat.scrollHeight;
      closeSidebarIfMobile();
      loadSessionList();
    } catch (e) {
      console.error("openSession failed", e);
    }
  }

  // ---------- Feedback ----------
  function feedbackRow(sessionId) {
    const row = el("div", { className: "feedback" });
    const label = el("span", { className: "feedback-label", text: "Helpful?" });
    const up = el("button", { className: "feedback-btn", text: "👍" });
    const down = el("button", { className: "feedback-btn", text: "👎" });
    const note = el("span", { className: "feedback-note" });

    async function submit(rating, btn) {
      if (up.disabled) return;
      up.disabled = down.disabled = true;
      btn.classList.add("selected");
      try {
        await api("/api/feedback", {
          method: "POST",
          body: JSON.stringify({ session_id: sessionId, rating }),
        });
        note.textContent = "thanks";
      } catch (e) {
        note.textContent = "(failed to save)";
        up.disabled = down.disabled = false;
      }
    }
    up.addEventListener("click", () => submit(5, up));
    down.addEventListener("click", () => submit(1, down));

    row.append(label, up, down, note);
    return row;
  }

  // ---------- Examples ----------
  // Clicking a chip fills the textarea so the user can edit before sending.
  // This is deliberate: the chips demonstrate the "scope + filter + metric"
  // pattern, and part of that pedagogy is letting the user tweak them.
  for (const chip of document.querySelectorAll(".example-chip")) {
    chip.addEventListener("click", () => {
      if (state.busy) return;
      const textarea = $("input");
      textarea.value = chip.textContent.trim();
      autoGrow();
      textarea.focus();
      // Move caret to the end so typing appends instead of overwriting.
      const end = textarea.value.length;
      textarea.setSelectionRange(end, end);
    });
  }

  // ---------- Input / Send ----------
  const input = $("input");
  const inputForm = $("input-bar");
  const btnSend = $("btn-send");
  const btnCancel = $("btn-cancel");

  function autoGrow() {
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight, 200) + "px";
  }
  input.addEventListener("input", autoGrow);

  // IME-safe Enter: Enter submits, Shift+Enter newlines, Enter during
  // IME composition (isComposing / keyCode 229) is ignored.
  input.addEventListener("keydown", (e) => {
    if (e.key !== "Enter" || e.shiftKey) return;
    if (e.isComposing || e.keyCode === 229) return;
    e.preventDefault();
    doSend();
  });

  inputForm.addEventListener("submit", (e) => {
    e.preventDefault();
    doSend();
  });

  btnCancel.addEventListener("click", () => {
    if (state.abortController) state.abortController.abort();
  });

  // ---------- 5-step status indicator ----------
  const stageText = {
    thinking: "thinking…",
    understanding: "understanding question…",
    executing: "querying database…",
    describing: "describing query…",
    interpreting: "interpreting results…",
  };
  function setStage(key) {
    const s = $("stage-indicator");
    if (!key) { s.hidden = true; return; }
    s.hidden = false;
    s.textContent = stageText[key] || key;
  }

  // ---------- SSE parsing ----------
  // Parses text/event-stream incrementally. Events are separated by a
  // blank line; each event has `event: <name>\n` and `data: <json>\n`.
  function sseParser(onEvent) {
    let buffer = "";
    return (chunk) => {
      buffer += chunk;
      let idx;
      while ((idx = buffer.indexOf("\n\n")) >= 0) {
        const frame = buffer.slice(0, idx);
        buffer = buffer.slice(idx + 2);
        let name = null, data = null;
        for (const line of frame.split("\n")) {
          if (line.startsWith("event: ")) name = line.slice(7);
          else if (line.startsWith("data: ")) data = line.slice(6);
        }
        if (name !== null && data !== null) {
          let parsed;
          try { parsed = JSON.parse(data); } catch { parsed = data; }
          onEvent(name, parsed);
        }
      }
    };
  }

  // ---------- Send ----------
  async function doSend() {
    const text = input.value.trim();
    if (!text || state.busy) return;

    state.busy = true;
    btnSend.disabled = true;
    btnCancel.hidden = false;
    input.value = "";
    autoGrow();
    $("examples").hidden = true;

    const chat = $("chat");
    const userMsg = el("div", { className: "msg msg-user", text });
    chat.appendChild(userMsg);
    state.history.push({ role: "user", content: text });

    const aiMsg = el("div", { className: "msg msg-ai" });
    const thinking = el("span", { className: "thinking", text: stageText.thinking });
    aiMsg.appendChild(thinking);
    chat.appendChild(aiMsg);
    chat.scrollTop = chat.scrollHeight;
    setStage("thinking");

    let fullText = "";
    let sawFirstText = false;
    let errNode = null;
    state.abortController = new AbortController();

    const handleEvent = (name, data) => {
      if (name === "stage") {
        // Authoritative stage transitions from the server:
        //   understanding | executing | describing | interpreting
        setStage(typeof data === "string" ? data : "thinking");
        if (!sawFirstText && typeof data === "string") {
          thinking.textContent = stageText[data] || data;
        }
      } else if (name === "status") {
        // Legacy event. Kept for compatibility; equivalent to stage=executing.
        setStage("executing");
        if (!sawFirstText) thinking.textContent = stageText.executing;
      } else if (name === "text") {
        if (!sawFirstText) {
          aiMsg.replaceChildren();
          sawFirstText = true;
        }
        fullText += data;
        aiMsg.textContent = fullText;
        chat.scrollTop = chat.scrollHeight;
      } else if (name === "error") {
        if (!sawFirstText) {
          aiMsg.replaceChildren();
          sawFirstText = true;  // prevent double clearing
        }
        if (!errNode) {
          errNode = el("div", { className: "err-banner" });
          aiMsg.appendChild(errNode);
        }
        errNode.textContent = (data && data.error) || "Error";
      }
      // "done" is a no-op here — end of stream is detected by reader.done.
    };

    try {
      const res = await fetch("/api/chat", {
        method: "POST",
        headers: authHeaders(),
        body: JSON.stringify({ messages: state.history, session_id: state.sessionId }),
        signal: state.abortController.signal,
      });
      if (!res.ok) throw new Error("HTTP " + res.status);

      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      const feed = sseParser(handleEvent);

      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        feed(decoder.decode(value, { stream: true }));
      }
    } catch (e) {
      // Clear the "Thinking..." placeholder so the error banner doesn't stack on top.
      if (!sawFirstText) aiMsg.replaceChildren();
      const msg = e.name === "AbortError" ? "Cancelled." : "Error: " + e.message;
      aiMsg.appendChild(el("div", { className: "err-banner", text: msg }));
    } finally {
      state.busy = false;
      btnSend.disabled = false;
      btnCancel.hidden = true;
      state.abortController = null;
      setStage(null);
    }

    if (fullText.trim()) {
      state.history.push({ role: "assistant", content: fullText });
      aiMsg.appendChild(feedbackRow(state.sessionId));
    }
    // Cap history length to keep prompt costs bounded.
    if (state.history.length > 40) state.history = state.history.slice(-40);

    input.focus();
    loadSessionList();
  }

  // ---------- Save on leave ----------
  window.addEventListener("beforeunload", () => {
    if (state.history.length === 0) return;
    const payload = JSON.stringify({
      session_id: state.sessionId,
      messages: state.history,
      user_id: userIdFromToken(),
    });
    navigator.sendBeacon("/api/end-session", new Blob([payload], { type: "application/json" }));
  });

  // ---------- Mobile sidebar toggle ----------
  $("btn-menu").addEventListener("click", () => {
    $("sidebar").classList.toggle("open");
  });
  function closeSidebarIfMobile() {
    if (window.matchMedia("(max-width: 720px)").matches) {
      $("sidebar").classList.remove("open");
    }
  }

  // ---------- Boot ----------
  (async function init() {
    if (!state.token) return;
    try {
      const me = await api("/api/me");
      if (me.status === "ok") {
        state.user = me.username;
        enterApp();
      }
    } catch {
      // If /api/me fails we just stay on the login screen.
    }
  })();
})();
