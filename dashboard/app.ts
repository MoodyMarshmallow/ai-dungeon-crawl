import type { State } from "./types";
import { TerminalDisplay } from "./displays";
import { activityEntries } from "./transcript";
import { codeHtml, markdownHtml } from "./rendering";

const $ = (id: string) => document.getElementById(id)!;
const button = (id: string) => $(id) as HTMLButtonElement;
const game = new TerminalDisplay($("screen"), true);
const repl = new TerminalDisplay($("repl-terminal"));
const entries = new Map<string, HTMLElement>();
let current: State | null = null;
let connected = false,
  pending = false;
let lastRun: number | null = null;

function text(element: HTMLElement, value: string) {
  if (element.textContent !== value) element.textContent = value;
}

function controls() {
  button("start").disabled =
    !connected || pending || current?.status === "running";
  button("stop").disabled =
    !connected || pending || current?.status !== "running";
  text($("status"), !connected ? "Reconnecting…" : "");
}

function render(state: State) {
  current = state;
  const scroll = $("activity-scroll");
  const newRun = lastRun !== state.run_id;
  const follow =
    newRun || scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight < 60;
  if (newRun) {
    entries.clear();
    $("activity").replaceChildren();
    lastRun = state.run_id;
  }
  game.setText(state.observation?.screen ?? "");
  repl.setSubmissions(state.submissions);
  const items = activityEntries(state);
  const retained = new Set(items.map((item) => item.id));
  for (const [id, element] of entries)
    if (!retained.has(id)) {
      element.remove();
      entries.delete(id);
    }
  let previous: HTMLElement | null = null;
  for (const item of items) {
    let element = entries.get(item.id);
    if (!element) {
      element = document.createElement("div");
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
          item.label === "execute_python" ? "Execute Python" : item.label;
        const code = document.createElement("pre");
        code.innerHTML = codeHtml(item.text, item.format);
        element.replaceChildren(title, code);
      } else if (item.format === "text") text(element, item.text);
      else
        element.innerHTML =
          item.format === "markdown"
            ? markdownHtml(item.text)
            : codeHtml(item.text, item.format);
      element.dataset.source = item.text;
      element.dataset.format = item.format;
    }
    const next: ChildNode | null = previous
      ? previous.nextSibling
      : $("activity").firstChild;
    if (next !== element) $("activity").insertBefore(element, next);
    previous = element;
  }
  $("error").hidden = !state.error;
  text($("error"), state.error ?? "");
  if (follow) scroll.scrollTop = scroll.scrollHeight;
  controls();
}

async function command(path: string) {
  pending = true;
  controls();
  try {
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
