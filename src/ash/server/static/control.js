"use strict";

const els = {
  token: document.querySelector("#token"),
  connect: document.querySelector("#connect"),
  state: document.querySelector("#connection-state"),
  summary: document.querySelector("#runtime-summary"),
  details: document.querySelector("#connection-details"),
  transcript: document.querySelector("#transcript"),
  prompt: document.querySelector("#prompt"),
  send: document.querySelector("#send"),
  stop: document.querySelector("#stop"),
  sessions: document.querySelector("#sessions"),
  refreshSessions: document.querySelector("#refresh-sessions"),
  newSession: document.querySelector("#new-session"),
  approvals: document.querySelector("#approvals"),
  notice: document.querySelector("#notice"),
};

let token = "";
let connected = false;
let turnRunning = false;
let turnController = null;
let approvalGeneration = 0;
let noticeTimer = null;
let currentSessionId = null;
const MAX_SSE_BUFFER_CHARS = 8 * 1024 * 1024;
const MAX_TRANSCRIPT_ENTRIES = 300;

function authHeaders(extra = {}) {
  return {
    Authorization: `Bearer ${token}`,
    ...extra,
  };
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    cache: "no-store",
    credentials: "omit",
    redirect: "error",
    ...options,
    headers: authHeaders(options.headers || {}),
  });
  const raw = await response.text();
  let payload = {};
  if (raw) {
    try {
      payload = JSON.parse(raw);
    } catch {
      throw new Error(`Ash returned invalid JSON (HTTP ${response.status})`);
    }
  }
  if (!response.ok) {
    const detail =
      typeof payload.detail === "string" ? payload.detail : `HTTP ${response.status}`;
    throw new Error(detail);
  }
  return payload;
}

function setConnected(value) {
  connected = value;
  els.state.textContent = value ? "Connected" : "Disconnected";
  els.token.disabled = value;
  els.connect.textContent = value ? "Disconnect" : "Connect";
  els.prompt.disabled = !value;
  els.send.disabled = !value;
  els.stop.disabled = true;
  els.sessions.disabled = !value;
  els.refreshSessions.disabled = !value;
  els.newSession.disabled = !value;
  els.prompt.placeholder = value ? "Message Ash…" : "Connect to Ash to start.";
  if (!value) {
    approvalGeneration += 1;
    currentSessionId = null;
    token = "";
    els.token.value = "";
    els.summary.textContent = "";
    els.details.replaceChildren();
    els.sessions.replaceChildren();
    renderApprovals([]);
  }
}

function notify(message) {
  els.notice.textContent = message;
  els.notice.classList.add("visible");
  if (noticeTimer !== null) {
    window.clearTimeout(noticeTimer);
  }
  noticeTimer = window.setTimeout(() => {
    els.notice.classList.remove("visible");
  }, 3500);
}

function addEntry(label, text, kind = "") {
  const entry = document.createElement("article");
  entry.className = `entry ${kind}`.trim();
  const heading = document.createElement("div");
  heading.className = "entry-label";
  heading.textContent = label;
  const body = document.createElement("div");
  body.className = "entry-body";
  body.textContent = text;
  entry.append(heading, body);
  els.transcript.append(entry);
  while (els.transcript.childElementCount > MAX_TRANSCRIPT_ENTRIES) {
    els.transcript.firstElementChild?.remove();
  }
  els.transcript.scrollTop = els.transcript.scrollHeight;
  return body;
}

function renderDetails(status) {
  const fields = [
    ["Model", status.model],
    ["Mode", status.mode],
    ["Workspace", status.workspace],
    ["Session", status.session_id],
  ];
  els.details.replaceChildren();
  for (const [label, value] of fields) {
    const dt = document.createElement("dt");
    dt.textContent = label;
    const dd = document.createElement("dd");
    dd.textContent = value == null ? "—" : String(value);
    els.details.append(dt, dd);
  }
  els.summary.textContent = [status.model, status.session_id]
    .filter(Boolean)
    .join(" · ");
}

async function refreshStatus() {
  const payload = await api("/rpc", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({
      jsonrpc: "2.0",
      id: 1,
      method: "status",
      params: {},
    }),
  });
  if (payload.error || typeof payload.result !== "object" || payload.result === null) {
    throw new Error(payload.error?.message || "Invalid Ash status response");
  }
  renderDetails(payload.result);
  return payload.result;
}

function sessionLabel(item) {
  const id = item.session_id || item.id || "unknown";
  const name = item.title || item.name || item.branch_name || "";
  return name ? `${name} — ${id}` : id;
}

async function refreshSessions(selectedId = null) {
  const payload = await api("/v1/sessions?limit=100");
  const sessions = Array.isArray(payload.sessions) ? payload.sessions : [];
  els.sessions.replaceChildren();
  if (sessions.length === 0) {
    const option = document.createElement("option");
    option.value = "";
    option.textContent = "No sessions";
    els.sessions.append(option);
    return;
  }
  for (const item of sessions) {
    const option = document.createElement("option");
    option.value = item.session_id || item.id || "";
    option.textContent = sessionLabel(item);
    if (selectedId && option.value === selectedId) {
      option.selected = true;
    }
    els.sessions.append(option);
  }
}

async function selectSession(sessionId) {
  if (!sessionId || sessionId === currentSessionId) return;
  const previous = currentSessionId;
  try {
    const messages = await fetchSessionMessages(sessionId);
    const payload = await api("/v1/sessions/resume", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({session_id: sessionId}),
    });
    currentSessionId = payload.session_id;
    renderSessionMessages(messages);
    notify(`Resumed ${payload.session_id}`);
    await refreshStatus();
  } catch (error) {
    if (previous) {
      els.sessions.value = previous;
    }
    notify(error.message);
  }
}

async function createSession() {
  const payload = await api("/v1/sessions", {method: "POST"});
  currentSessionId = payload.session_id;
  renderSessionMessages([]);
  await refreshSessions(payload.session_id);
  await refreshStatus();
  notify(`Created ${payload.session_id}`);
}

async function fetchSessionMessages(sessionId) {
  const payload = await api(
    `/v1/sessions/${encodeURIComponent(sessionId)}/messages?limit=300`
  );
  if (!Array.isArray(payload.messages)) {
    throw new Error("Ash returned an invalid session transcript.");
  }
  return payload.messages;
}

function renderSessionMessages(messages) {
  els.transcript.replaceChildren();
  for (const item of messages) {
    const role = item && typeof item.role === "string" ? item.role : "system";
    const content =
      item && typeof item.content === "string" ? item.content : "";
    if (!content) continue;
    const label =
      role === "user"
        ? "You"
        : role === "assistant"
          ? "Ash"
          : role === "tool"
            ? "Tool"
            : "System";
    addEntry(label, content, role === "tool" ? "tool" : "");
  }
}

function renderApprovals(items) {
  els.approvals.replaceChildren();
  if (!items.length) {
    const empty = document.createElement("p");
    empty.className = "empty";
    empty.textContent = "No pending approvals.";
    els.approvals.append(empty);
    return;
  }
  for (const item of items) {
    const block = document.createElement("div");
    block.className = "approval";
    const tool = document.createElement("div");
    tool.className = "approval-tool";
    tool.textContent = item.tool || "tool";
    const args = document.createElement("pre");
    args.textContent = JSON.stringify(item.arguments || {}, null, 2);
    const actions = document.createElement("div");
    actions.className = "approval-actions";
    const approve = document.createElement("button");
    approve.type = "button";
    approve.textContent = "Approve";
    approve.addEventListener("click", () => resolveApproval(item.id, true));
    const deny = document.createElement("button");
    deny.type = "button";
    deny.textContent = "Deny";
    deny.addEventListener("click", () => resolveApproval(item.id, false));
    actions.append(approve, deny);
    block.append(tool, args, actions);
    els.approvals.append(block);
  }
}

async function resolveApproval(id, approved) {
  if (!id) return;
  try {
    await api(`/v1/approvals/${encodeURIComponent(id)}`, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({approved}),
    });
    notify(approved ? "Approved tool call." : "Denied tool call.");
  } catch (error) {
    notify(error.message);
  }
}

async function approvalLoop(generation) {
  while (connected && generation === approvalGeneration) {
    try {
      const payload = await api("/v1/approvals?wait_seconds=25");
      if (generation !== approvalGeneration) return;
      const approvals = Array.isArray(payload.approvals) ? payload.approvals : [];
      renderApprovals(approvals);
      if (approvals.length > 0) {
        await new Promise((resolve) => window.setTimeout(resolve, 2000));
      }
    } catch (error) {
      if (generation !== approvalGeneration || !connected) return;
      notify(`Approval channel: ${error.message}`);
      await new Promise((resolve) => window.setTimeout(resolve, 1500));
    }
  }
}

function setTurnRunning(value) {
  turnRunning = value;
  els.send.textContent = value ? "Steer" : "Send";
  els.stop.disabled = !value;
  els.sessions.disabled = value;
  els.refreshSessions.disabled = value;
  els.newSession.disabled = value;
  els.prompt.placeholder = value ? "Steer the running turn…" : "Message Ash…";
}

async function streamTurn(text) {
  turnController = new AbortController();
  const response = await fetch("/v1/turn/stream", {
    method: "POST",
    cache: "no-store",
    credentials: "omit",
    redirect: "error",
    headers: authHeaders({"Content-Type": "application/json"}),
    body: JSON.stringify({
      input: text,
      ...(currentSessionId ? {session_id: currentSessionId} : {}),
    }),
    signal: turnController.signal,
  });
  if (!response.ok || !response.body) {
    const raw = await response.text();
    throw new Error(raw || `HTTP ${response.status}`);
  }
  const contentType = (response.headers.get("content-type") || "").split(";")[0];
  if (contentType !== "text/event-stream") {
    throw new Error("Ash did not return an event stream.");
  }

  const assistantBody = addEntry("Ash", "");
  const reader = response.body.getReader();
  const decoder = new TextDecoder("utf-8", {fatal: true});
  let buffer = "";
  let eventName = "message";
  let dataLines = [];

  function dispatch() {
    if (!dataLines.length) return;
    const payload = JSON.parse(dataLines.join("\n"));
    if (eventName === "assistant.delta" && typeof payload.text === "string") {
      assistantBody.textContent += payload.text;
      els.transcript.scrollTop = els.transcript.scrollHeight;
    } else if (eventName === "tool.requested") {
      addEntry("Tool", `Requested: ${payload.tool || "tool"}`, "tool");
    } else if (eventName === "tool.completed") {
      addEntry("Tool", `Completed: ${payload.tool || "tool"}`, "tool");
    } else if (eventName === "turn.error" || eventName === "turn.cancelled") {
      addEntry("Ash", payload.error || payload.reason || eventName, "system");
    }
  }

  while (true) {
    const {value, done} = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, {stream: true});
    if (buffer.length > MAX_SSE_BUFFER_CHARS) {
      await reader.cancel();
      throw new Error("Ash event stream exceeded the browser client limit.");
    }
    while (true) {
      const newline = buffer.indexOf("\n");
      if (newline < 0) break;
      const rawLine = buffer.slice(0, newline);
      buffer = buffer.slice(newline + 1);
      const line = rawLine.endsWith("\r") ? rawLine.slice(0, -1) : rawLine;
      if (line === "") {
        dispatch();
        eventName = "message";
        dataLines = [];
      } else if (line.startsWith("event:")) {
        eventName = line.slice(6).trimStart();
      } else if (line.startsWith("data:")) {
        dataLines.push(line.slice(5).trimStart());
      }
    }
  }
  if (buffer || dataLines.length) {
    throw new Error("Ash event stream ended mid-event.");
  }
}

async function submit() {
  const text = els.prompt.value.trim();
  if (!connected || !text) return;
  els.prompt.value = "";
  if (turnRunning) {
    try {
      const result = await api("/v1/turn/steer", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({input: text}),
      });
      addEntry("You · steer", text);
      notify(`Queued steering (${result.pending}).`);
    } catch (error) {
      addEntry("System", error.message, "system");
    }
    return;
  }

  addEntry("You", text);
  setTurnRunning(true);
  try {
    await streamTurn(text);
    await refreshStatus();
    await refreshSessions();
  } catch (error) {
    if (error.name === "AbortError") {
      addEntry("System", "Turn cancelled.", "system");
    } else {
      addEntry("System", error.message, "system");
    }
  } finally {
    turnController = null;
    setTurnRunning(false);
  }
}

async function connect() {
  if (connected) {
    turnController?.abort();
    setConnected(false);
    return;
  }
  const candidate = els.token.value;
  if (candidate.length < 16) {
    notify("Enter the server bearer token.");
    return;
  }
  token = candidate;
  try {
    const status = await refreshStatus();
    await refreshSessions(status.session_id || null);
    const initialSessionId = status.session_id || null;
    const initialMessages = initialSessionId
      ? await fetchSessionMessages(initialSessionId)
      : [];
    setConnected(true);
    currentSessionId = initialSessionId;
    renderSessionMessages(initialMessages);
    approvalGeneration += 1;
    void approvalLoop(approvalGeneration);
    els.prompt.focus();
  } catch (error) {
    token = "";
    currentSessionId = null;
    notify(error.message);
  }
}

els.connect.addEventListener("click", () => void connect());
els.send.addEventListener("click", () => void submit());
els.stop.addEventListener("click", () => {
  if (turnController !== null) {
    turnController.abort();
  }
});
els.newSession.addEventListener("click", () => void createSession());
els.refreshSessions.addEventListener("click", () => void refreshSessions());
els.sessions.addEventListener("change", () => void selectSession(els.sessions.value));
els.prompt.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    void submit();
  }
});
