import type { State } from "./types";
import { TerminalDisplay } from "./displays";
import { activityEntries, turnSummary } from "./transcript";
import { codeHtml, markdownHtml } from "./rendering";
import { executionOutputHtml } from "./observation-output";

const $ = (id: string) => document.getElementById(id)!;
const button = (id: string) => $(id) as HTMLButtonElement;
const game = new TerminalDisplay($("screen"), true);
const shell = new TerminalDisplay($("shell-terminal"));
const entries = new Map<string, HTMLElement>();
const turns = new Map<number, HTMLDetailsElement>();
let current: State | null = null;
let connected = false,
  pending = false;
let lastRun: number | null = null;
let settingsDirty = false;
let savedSettings: State["config"] | null = null;
const settingsForm = $("settings-form") as HTMLFormElement;
const modelInput = $("setting-model") as HTMLSelectElement;
const reasoningInput = $("setting-reasoning") as HTMLSelectElement;
const turnsInput = $("setting-turns") as HTMLInputElement;

function syncSettings(state: State) {
  if (savedSettings &&
      state.config.model === savedSettings.model &&
      state.config.reasoning_effort === savedSettings.reasoning_effort &&
      state.config.max_turns === savedSettings.max_turns) {
    settingsDirty = false;
    savedSettings = null;
  }
  if (settingsDirty) return;
  // Preserve explicit CLI model overrides without adding an editable model field.
  if (state.config.model && !Array.from(modelInput.options).some(option => option.value === state.config.model))
    modelInput.add(new Option(state.config.model, state.config.model));
  modelInput.value = state.config.model ?? "";
  reasoningInput.value = state.config.reasoning_effort === "default" ? "low" : state.config.reasoning_effort;
  turnsInput.value = String(state.config.max_turns);
  if (state.config.reasoning_effort === "default") settingsDirty = true;
}
settingsForm.addEventListener("input", () => {
  settingsDirty = true;
  savedSettings = null;
  text($("settings-status"), "Changes apply when you press Start.");
});
settingsForm.addEventListener("submit", (event) => event.preventDefault());

function text(element: HTMLElement, value: string) {
  if (element.textContent !== value) element.textContent = value;
}

function controls() {
  button("start").disabled =
    !connected || pending || current?.status === "running";
  button("stop").disabled =
    !connected || pending || current?.status !== "running";
  text($("status"), !connected ? "Reconnecting…" : "");
  ($("settings-fields") as HTMLFieldSetElement).disabled =
    !connected || pending || current?.status === "running";
  button("save-defaults").disabled = !connected || pending || current?.status === "running";
}

function render(state: State) {
  current = state;
  syncSettings(state);
  const scroll = $("activity-scroll");
  const newRun = lastRun !== state.run_id;
  const follow =
    newRun || scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight < 60;
  if (newRun) {
    entries.clear();
    turns.clear();
    $("activity").replaceChildren();
    lastRun = state.run_id;
  }
  game.setObservation(state.observation);
  shell.setSubmissions(state.submissions);
  const items = activityEntries(state);
  const retained = new Set(items.map((item) => item.id));
  for (const [id, element] of entries)
    if (!retained.has(id)) {
      element.remove();
      entries.delete(id);
    }
  const retainedTurns = new Set(items.map((item) => item.turn));
  for (const [turn, element] of turns)
    if (!retainedTurns.has(turn)) {
      element.remove();
      turns.delete(turn);
    }
  let previousTurn: HTMLElement | null = null;
  const previousEntries = new Map<number, HTMLElement>();
  for (const item of items) {
    let group = turns.get(item.turn);
    if (!group) {
      group = document.createElement("details");
      group.className = "activity-turn";
      const title = document.createElement("summary");
      title.className = "turn-title";
      title.id = `turn-${item.turn}`;
      const number = document.createElement("span");
      number.className = "turn-number";
      number.textContent = `Turn ${item.turn + 1}`;
      const heading = document.createElement("span");
      heading.className = "turn-heading-preview";
      title.append(number, heading);
      group.setAttribute("aria-labelledby", title.id);
      group.append(title);
      // Advance disclosure only when a new latest turn appears, not on streamed updates.
      const latestTurn = Math.max(-1, ...turns.keys());
      if (item.turn > latestTurn) {
        const previous = turns.get(latestTurn);
        if (previous) previous.open = false;
        group.open = true;
      }
      turns.set(item.turn, group);
    }
    const summary = turnSummary(state, item.turn);
    const heading = group.querySelector<HTMLElement>(".turn-heading-preview")!;
    text(heading, summary.startsWith(`Turn ${item.turn + 1}: `)
      ? summary.slice(`Turn ${item.turn + 1}: `.length) : "");
    if (previousTurn !== group) {
      const next: ChildNode | null = previousTurn ? previousTurn.nextSibling : $("activity").firstChild;
      if (next !== group) $("activity").insertBefore(group, next);
      previousTurn = group;
    }
    let element = entries.get(item.id);
    if (!element) {
      element = document.createElement(item.className === "result" ? "details" : "div");
      entries.set(item.id, element);
    }
    element.className = item.className;
    element.setAttribute("aria-label", item.label);
    if (
      element.dataset.source !== item.text ||
      element.dataset.format !== item.format
    ) {
      if (item.className === "model-part tool") {
        const title = document.createElement("div");
        title.className = "tool-title";
        title.textContent =
          item.label === "execute_shell"
            ? "Execute Shell"
            : item.label === "execute_python"
              ? "Execute Python"
              : item.label;
        const code = document.createElement("pre");
        code.innerHTML = codeHtml(item.text, item.format);
        element.replaceChildren(title, code);
      } else if (item.className === "result") {
        if (!element.firstChild) {
          const summary = document.createElement("summary");
          summary.textContent = "Output";
          const output = document.createElement("div");
          output.className = "result-output";
          element.append(summary, output);
        }
        // Retain disclosure state and keyboard focus while output streams.
        (element.lastElementChild as HTMLElement).innerHTML = executionOutputHtml(item.text);
      } else if (item.format === "text") text(element, item.text);
      else
        element.innerHTML =
          item.format === "markdown"
            ? markdownHtml(item.text)
            : codeHtml(item.text, item.format);
      element.dataset.source = item.text;
      element.dataset.format = item.format;
    }
    const previous = previousEntries.get(item.turn);
    const next: ChildNode | null = previous
      ? previous.nextSibling
      : group.firstChild!.nextSibling;
    if (next !== element) group.insertBefore(element, next);
    previousEntries.set(item.turn, element);
  }
  $("error").hidden = !state.error;
  text($("error"), state.error ?? "");
  if (follow) scroll.scrollTop = scroll.scrollHeight;
  controls();
}

async function command(path: string) {
  if ((path === "/defaults" || (path === "/run" && settingsDirty)) && !settingsForm.checkValidity()) {
    selectShellView("settings");
    settingsForm.reportValidity();
    return;
  }
  pending = true;
  controls();
  try {
    if (path === "/defaults") {
      const response = await fetch(path, {
        method: "POST",
        headers: { "X-Dashboard-Request": "1", "Content-Type": "application/json" },
        body: JSON.stringify({ model: modelInput.value, reasoning_effort: reasoningInput.value,
          max_turns: Number(turnsInput.value) }),
      });
      if (!response.ok) throw new Error(await response.text());
      savedSettings = await response.json();
      if (current) syncSettings(current);
      text($("settings-status"), "Default saved.");
      return;
    }
    if (path === "/run" && settingsDirty) {
      const response = await fetch("/config", {
        method: "POST",
        headers: { "X-Dashboard-Request": "1", "Content-Type": "application/json" },
        body: JSON.stringify({
          model: modelInput.value.trim() || null,
          reasoning_effort: reasoningInput.value,
          max_turns: Number(turnsInput.value),
        }),
      });
      if (!response.ok) throw new Error(await response.text());
      // Keep edits until the ordered event stream acknowledges the saved config.
      savedSettings = await response.json();
      if (current) syncSettings(current);
      text($("settings-status"), "Applies when you press Start.");
    }
    const response = await fetch(path, {
      method: "POST",
      headers: { "X-Dashboard-Request": "1" },
    });
    if (!response.ok) throw new Error(await response.text());
  } catch (error) {
    $("error").hidden = false;
    text($("error"), error instanceof Error ? error.message : "Request failed");
  } finally {
    pending = false;
    controls();
  }
}
$("start").addEventListener("click", () => command("/run"));
$("stop").addEventListener("click", () => command("/stop"));
$("save-defaults").addEventListener("click", () => command("/defaults"));

function selectShellView(view: "shell" | "settings") {
  for (const name of ["shell", "settings"]) {
    const selected = name === view;
    button(`${name}-tab`).setAttribute("aria-selected", String(selected));
    button(`${name}-tab`).tabIndex = selected ? 0 : -1;
    $(`${name}-view`).setAttribute("aria-hidden", String(!selected));
    $(`${name}-view`).inert = !selected;
  }
}
for (const view of ["shell", "settings"] as const) {
  button(`${view}-tab`).addEventListener("click", () => selectShellView(view));
  button(`${view}-tab`).addEventListener("keydown", (event) => {
    if (["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) {
      event.preventDefault();
      const next = event.key === "Home" ? "shell" : event.key === "End" ? "settings" : view === "shell" ? "settings" : "shell";
      selectShellView(next);
      button(`${next}-tab`).focus();
    }
  });
}

function selectView(view: "terminal" | "tiles") {
  for (const name of ["terminal", "tiles"]) {
    const selected = name === view;
    button(`${name}-tab`).setAttribute("aria-selected", String(selected));
    button(`${name}-tab`).tabIndex = selected ? 0 : -1;
    $(name).setAttribute("aria-hidden", String(!selected));
  }
}
for (const view of ["terminal", "tiles"] as const) {
  button(`${view}-tab`).addEventListener("click", () => selectView(view));
  button(`${view}-tab`).addEventListener("keydown", (event) => {
    if (["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) {
      event.preventDefault();
      const next = event.key === "Home" ? "terminal" : event.key === "End" ? "tiles" : view === "terminal" ? "tiles" : "terminal";
      selectView(next);
      button(`${next}-tab`).focus();
    }
  });
}

function splitter(id: string, horizontal: boolean) {
  const handle = $(id),
    container = $(horizontal ? "left-workspace" : "workspace");
  let value = horizontal ? 60 : 34;
  const set = (next: number) => {
    value = Math.max(
      horizontal ? 35 : 25,
      Math.min(horizontal ? 75 : 50, next),
    );
    container.style.setProperty(
      horizontal ? "--game-share" : "--sidebar",
      value + "%",
    );
    handle.setAttribute("aria-valuenow", String(Math.round(value)));
  };
  set(value);
  handle.setAttribute("aria-valuemin", horizontal ? "35" : "25");
  handle.setAttribute("aria-valuemax", horizontal ? "75" : "50");
  handle.addEventListener("pointerdown", (event) =>
    handle.setPointerCapture(event.pointerId),
  );
  handle.addEventListener("pointermove", (event) => {
    if (!handle.hasPointerCapture(event.pointerId)) return;
    const rect = container.getBoundingClientRect();
    set(
      horizontal
        ? ((event.clientY - rect.top) / rect.height) * 100
        : ((rect.right - event.clientX) / rect.width) * 100,
    );
  });
  handle.addEventListener("pointerup", (event) =>
    handle.releasePointerCapture(event.pointerId),
  );
  handle.addEventListener("keydown", (event) => {
    const change: Record<string, number> = horizontal
      ? { ArrowUp: -3, ArrowDown: 3 }
      : { ArrowLeft: 3, ArrowRight: -3 };
    if (event.key in change) {
      event.preventDefault();
      set(value + change[event.key]!);
    }
  });
}
splitter("row-handle", true);
splitter("column-handle", false);
const stream = new EventSource("/events");
stream.onopen = () => {
  connected = true;
  controls();
};
stream.onerror = () => {
  connected = false;
  controls();
};
stream.onmessage = (event) => render(JSON.parse(event.data));
