"use strict";
// Memoria dashboard. Memory text is always inserted as plain text (never as HTML),
// so nothing an agent writes in a memory can run in this page.

const $ = (selector) => document.querySelector(selector);
const state = { memories: [], agents: [], current: null };

// Build an element: h("div", {class: "x", onclick: fn}, "text", childElement, ...)
function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === false || value == null) continue;
    if (key.startsWith("on")) el.addEventListener(key.slice(2), value);
    else if (key === "class") el.className = value;
    else el.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat()) {
    if (child == null || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}

async function api(path, body) {
  const response = await fetch("/dashboard/api" + path, {
    method: body ? "POST" : "GET",
    headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
    credentials: "same-origin",
  });
  let data = {};
  try { data = await response.json(); } catch { /* empty reply */ }
  if (!response.ok) throw new Error(data.message || "Something went wrong.");
  return data;
}

let toastTimer;
function toast(message, isError = false) {
  const el = $("#toast");
  el.textContent = message;
  el.className = "toast show" + (isError ? " error" : "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.className = "toast"; }, 3500);
}

const when = (iso) => iso
  ? new Date(iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" })
  : "";

const verifiedBadge = (m) => m.verification === "verified"
  ? h("span", { class: "badge ok", title: m.verification_detail }, "✓ Verified")
  : h("span", { class: "badge bad", title: m.verification_detail }, "✗ NOT verified");

const confidenceBadge = (c) => h("span", { class: "badge conf-" + c }, c + " confidence");

const sharingText = (m) => m.visibility === "shared"
  ? "Shared with " + m.shared_with.join(", ")
  : "Private";

// ---------- the list ----------

async function loadMemories() {
  const data = await api("/memories");
  state.memories = data.memories;
  state.agents = data.agents;
  fillFilter("#filter-agent", "All agents", state.memories.map((m) => m.saved_by));
  fillFilter("#filter-topic", "All topics", state.memories.map((m) => m.topic));
  render();
}

function fillFilter(selector, allLabel, values) {
  const select = $(selector);
  const chosen = select.value;
  const options = [...new Set(values)].sort();
  select.replaceChildren(h("option", { value: "" }, allLabel),
    ...options.map((v) => h("option", { value: v }, v)));
  select.value = options.includes(chosen) ? chosen : "";
}

function matches(m) {
  const words = $("#search").value.toLowerCase().split(/\s+/).filter(Boolean);
  const haystack = [m.note, m.reason, m.topic, m.saved_by, m.updated_by, m.memory_id]
    .join(" ").toLowerCase();
  return words.every((w) => haystack.includes(w))
    && (!$("#filter-agent").value || m.saved_by === $("#filter-agent").value)
    && (!$("#filter-topic").value || m.topic === $("#filter-topic").value)
    && (!$("#filter-verified").value || m.verification === $("#filter-verified").value);
}

function render() {
  const shown = state.memories.filter(matches);
  $("#count").textContent = `Showing ${shown.length} of ${state.memories.length} memories`;
  $("#empty").hidden = shown.length > 0;
  $("#list").replaceChildren(...shown.map((m) => {
    const open = () => openDetail(m.memory_id);
    return h("li", {
      class: "item", tabindex: "0", role: "button",
      "aria-label": "Open memory " + m.memory_id,
      onclick: open, onkeydown: (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); } },
    },
      h("div", { class: "item-top" },
        h("span", { class: "badge topic" }, m.topic),
        verifiedBadge(m),
        h("span", { class: "badge" }, m.visibility === "shared" ? "👥 " + sharingText(m) : "🔒 Private"),
        h("span", { class: "spacer" }),
        confidenceBadge(m.confidence)),
      h("p", { class: "item-note" }, m.note),
      h("div", { class: "meta" },
        h("span", {}, "By ", h("b", {}, m.saved_by)),
        h("span", {}, m.updated_at ? "Updated " + when(m.updated_at) : "Saved " + when(m.saved_at)),
        m.times_updated ? h("span", {}, m.times_updated + 1 + " versions") : null,
        h("span", { class: "mono" }, m.memory_id)));
  }));
}

for (const id of ["#search", "#filter-agent", "#filter-topic", "#filter-verified"]) {
  $(id).addEventListener("input", render);
}

// ---------- details ----------

async function openDetail(memoryId) {
  let m;
  try { m = await api("/memories/" + encodeURIComponent(memoryId)); }
  catch (error) { toast(error.message, true); await loadMemories(); return; }
  state.current = m;
  $("#detail-title").textContent = "Memory " + m.memory_id;
  $("#detail-body").replaceChildren(
    h("section", { class: "section" },
      h("div", { class: "row" },
        h("span", { class: "badge topic" }, m.topic), verifiedBadge(m), confidenceBadge(m.confidence)),
      h("p", { class: "full-note" }, m.note),
      h("p", { class: "reason" }, "Why: " + m.reason)),
    h("section", { class: "section" },
      h("h3", {}, "Details"),
      h("dl", { class: "facts" },
        h("dt", {}, "Written by"), h("dd", {}, m.saved_by + " · " + when(m.saved_at)),
        m.updated_at ? [h("dt", {}, "Last updated"), h("dd", {}, m.updated_by + " · " + when(m.updated_at))] : null,
        h("dt", {}, "Signature"), h("dd", {}, m.verification + " (" + m.verification_detail + ")"),
        m.public_key ? [h("dt", {}, "Public key"), h("dd", { class: "mono" }, m.public_key)] : null,
        h("dt", {}, "Who can read it"), h("dd", {}, sharingText(m)))),
    h("section", { class: "section" },
      h("h3", {}, `History (${m.versions.length} version${m.versions.length > 1 ? "s" : ""})`),
      h("ol", { class: "versions" }, m.versions.map((v) =>
        h("li", { class: "version" + (v.current ? " current" : "") },
          h("div", { class: "version-head" },
            h("strong", {}, "Version " + v.version + (v.current ? " (current)" : "")),
            h("span", {}, "by " + v.written_by + " · " + when(v.written_at)),
            h("span", { class: "badge conf-" + v.confidence }, v.confidence)),
          h("p", {}, v.note),
          h("p", { class: "reason" }, "Why: " + v.reason))))),
    sharingSection(m),
    h("section", { class: "section danger-zone" },
      h("div", { class: "row" },
        h("p", { class: "hint grow" },
          "Deleting removes this memory for every agent. It can't be undone."),
        h("button", { class: "btn danger-ghost", onclick: () => askDelete(m) }, "Delete memory"))),
  );
  if (!$("#detail").open) $("#detail").showModal();
}

function sharingSection(m) {
  const others = state.agents.filter((a) => a !== m.saved_by);
  const radio = (value, label) => h("label", { class: "choice" },
    h("input", { type: "radio", name: "visibility", value, checked: m.visibility === value,
      onchange: syncAgents }), label);
  const agentBoxes = h("div", { class: "agents", id: "share-agents" }, others.map((a) =>
    h("label", { class: "choice" },
      h("input", { type: "checkbox", value: a, checked: m.shared_with.includes(a) }), a)));
  const save = h("button", { class: "btn primary", onclick: () => saveSharing(m, save) }, "Save sharing");
  const section = h("section", { class: "section" },
    h("h3", {}, "Sharing"),
    h("p", { class: "hint" },
      `Changes are made as ${m.saved_by}, the agent that wrote this memory, using its own key.`),
    h("div", { class: "choices" }, radio("private", "🔒 Private"), radio("shared", "👥 Shared")),
    agentBoxes,
    others.length ? null : h("p", { class: "hint" }, "No other registered agents to share with."),
    h("div", { class: "row" }, save));
  queueMicrotask(syncAgents);
  return section;
}

function syncAgents() {
  const shared = document.querySelector('input[name="visibility"][value="shared"]')?.checked;
  const box = $("#share-agents");
  if (!box) return;
  box.classList.toggle("disabled", !shared);
  box.querySelectorAll("input").forEach((input) => { input.disabled = !shared; });
}

async function saveSharing(m, button) {
  const visibility = document.querySelector('input[name="visibility"]:checked')?.value;
  const sharedWith = [...document.querySelectorAll("#share-agents input:checked")].map((i) => i.value);
  button.disabled = true;
  button.textContent = "Saving…";
  try {
    await api(`/memories/${encodeURIComponent(m.memory_id)}/sharing`,
      { visibility, shared_with: visibility === "shared" ? sharedWith : [] });
    toast(visibility === "shared" ? "Now shared with " + sharedWith.join(", ") : "Now private");
    await loadMemories();
    await openDetail(m.memory_id);
  } catch (error) {
    toast(error.message, true);
  } finally {
    button.disabled = false;
    button.textContent = "Save sharing";
  }
}

// ---------- delete, with "are you sure?" ----------

function askDelete(m) {
  $("#confirm-text").textContent =
    `“${m.note.slice(0, 120)}${m.note.length > 120 ? "…" : ""}” (${m.memory_id}) will be removed `
    + `for every agent. This can't be undone. It will be deleted as ${m.saved_by}, the agent that wrote it.`;
  $("#confirm-yes").onclick = async () => {
    $("#confirm-yes").disabled = true;
    $("#confirm-no").disabled = true;
    $("#confirm-yes").textContent = "Deleting…";
    try {
      await api(`/memories/${encodeURIComponent(m.memory_id)}/delete`, {});
      $("#confirm").close();
      $("#detail").close();
      toast("Memory deleted");
      await loadMemories();
    } catch (error) {
      toast(error.message, true);
    } finally {
      $("#confirm-yes").disabled = false;
      $("#confirm-no").disabled = false;
      $("#confirm-yes").textContent = "Yes, delete it";
    }
  };
  $("#confirm").showModal();
}
$("#confirm-no").addEventListener("click", () => $("#confirm").close());

// Close the details panel with its ✕ button or by clicking outside it.
document.querySelectorAll("[data-close]").forEach((b) =>
  b.addEventListener("click", () => b.closest("dialog").close()));
$("#detail").addEventListener("click", (e) => { if (e.target === $("#detail")) $("#detail").close(); });

// ---------- start ----------

loadMemories().catch((error) => toast(error.message, true));
