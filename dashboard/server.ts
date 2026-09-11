import { existsSync } from "node:fs";
import { resolve } from "node:path";
import { parseArgs } from "node:util";
import type { Subprocess } from "bun";
import type { Config, HarnessEvent } from "./types";
import { reasoningEfforts } from "./types";
import { loadDefaults, saveDefaults, limits, type Settings } from "./defaults";
import { applyEvent, emptyState } from "./state";
import { TileJournal, tileAssets, tileEvents, tilePage, tileScript, tileStyle } from "./tiles";

const root = resolve(import.meta.dir, "..");
const defaultSettingsPath = resolve(root, ".dashboard-defaults.json");

export class Dashboard {
  readonly tiles = new TileJournal();
  state;
  revision = 0;
  child: Subprocess<"ignore", "pipe", "ignore"> | null = null;
  completion: Promise<void> = Promise.resolve();
  private stopping = false;

  constructor(public config: Config) {
    this.state = emptyState(config);
  }

  get active(): boolean {
    return this.child !== null || this.state.status === "running";
  }

  updateConfig(value: unknown): void {
    if (!value || typeof value !== "object" || Array.isArray(value))
      throw new Error("Settings must be a JSON object");
    const fields = value as Record<string, unknown>;
    if (Object.keys(fields).some(key => !["model", "reasoning_effort", "max_turns", "action_turn_limit", "review_turn_limit", "episode_limit"].includes(key)))
      throw new Error("Unknown setting");
    const { model, reasoning_effort } = fields;
    const selectedLimits = limits(fields);
    if (model !== null && (typeof model !== "string" || !model.trim() || model.length > 200 || /[\x00-\x1f\x7f]/.test(model)))
      throw new Error("Enter a model name (at most 200 characters)");
    if (!reasoningEfforts.includes(reasoning_effort as Config["reasoning_effort"]))
      throw new Error("Choose a supported reasoning strength");
    const selectedModel = model === null ? (this.config.backend === "codex" ? "gpt-5.6-luna" : null) : (model as string).trim();
    if (!selectedModel) throw new Error("This provider requires a model name");
    if (this.config.backend === "pydantic" && reasoning_effort !== "default" && !/^openai(?:-responses|-chat)?:/.test(selectedModel))
      throw new Error("Reasoning strength requires a Codex or OpenAI model; choose Provider default for other providers");
    if (this.active) throw new Error("An episode is already running");
    this.config = { ...this.config, model: selectedModel, reasoning_effort: reasoning_effort as Config["reasoning_effort"], ...selectedLimits, max_turns: selectedLimits.action_turn_limit };
    this.state.config = this.config;
    this.revision++;
  }

  /** Start one fresh Python episode. Browser input never becomes a shell command. */
  start(): boolean {
    if (this.child) return false;
    this.stopping = false;
    this.state = emptyState(this.config, this.state.run_id + 1);
    this.tiles.reset(this.tiles.run + 1);
    Object.assign(this.state, {
      status: "running",
      phase: "Starting game",
      started_at: new Date().toISOString(),
    });
    this.revision++;
    const python = resolve(root, ".venv/bin/python");
    if (!existsSync(python)) {
      this.accept({
        event: "episode.finished",
        data: { status: "error", error: "Run uv sync in the repository first" },
      });
      return true;
    }
    const args = [
      python,
      "-B",
      "-u",
      "-m",
      "ai_dungeon_crawl.dashboard_bridge",
      "--policy",
      this.config.backend,
      "--reasoning-effort",
      this.config.reasoning_effort,
      "--action-turn-limit",
      String(this.config.action_turn_limit ?? this.config.max_turns ?? 3),
      "--review-turn-limit",
      String(this.config.review_turn_limit ?? 3),
      "--episode-limit",
      String(this.config.episode_limit ?? 1),
    ];
    if (this.config.crawl_path) args.push("--crawl-path", this.config.crawl_path);
    if (this.config.manual_path) args.push("--manual-path", this.config.manual_path);
    if (this.config.model) args.push("--model", this.config.model);
    if (this.config.reasoning_summary) args.push("--reasoning-summary");
    try {
      this.child = Bun.spawn(args, {
        cwd: root,
        stdin: "ignore",
        stdout: "pipe",
        stderr: "ignore",
      });
      this.completion = this.consume(this.child);
    } catch {
      this.accept({
        event: "episode.finished",
        data: { status: "error", error: "Python could not start" },
      });
    }
    return true;
  }

  accept(message: HarnessEvent) {
    if (message.event === "mode.changed" && message.data.mode === "action" && message.data.episode !== this.state.episode)
      this.tiles.reset(this.tiles.run + 1);
    if (message.event === "game.tiles") {
      this.tiles.append(message.data.messages);
      return;
    }
    applyEvent(this.state, message);
    this.revision++;
  }

  private async consume(child: Subprocess<"ignore", "pipe", "ignore">) {
    let pending = "";
    const decoder = new TextDecoder();
    try {
      for await (const chunk of child.stdout) {
        pending += decoder.decode(chunk, { stream: true });
        if (pending.length > 2_000_000) throw new Error("Oversized event");
        let newline;
        while ((newline = pending.indexOf("\n")) >= 0) {
          const line = pending.slice(0, newline);
          pending = pending.slice(newline + 1);
          if (line) this.accept(JSON.parse(line) as HarnessEvent);
        }
      }
      await child.exited;
      if (this.state.status === "running") {
        this.accept({
          event: "episode.finished",
          data: this.stopping
            ? { status: "stopped", stop_reason: "cancelled" }
            : {
                status: "error",
                error: "Harness exited before reporting completion",
              },
        });
      }
    } catch {
      child.kill("SIGTERM");
      await child.exited;
      this.accept({
        event: "episode.finished",
        data: { status: "error", error: "Harness event stream failed" },
      });
    } finally {
      this.child = null;
    }
  }

  stop() {
    if (this.child && !this.stopping) {
      this.stopping = true;
      this.state.phase = "Stopping and closing sandbox";
      this.revision++;
      this.child.kill("SIGTERM");
    }
  }
}

export async function startServer(dashboard: Dashboard, port = 8765, defaultsPath = defaultSettingsPath) {
  const build = await Bun.build({
    entrypoints: [resolve(import.meta.dir, "app.ts")],
    target: "browser",
  });
  if (!build.success) throw new Error("Dashboard TypeScript build failed");
  const script = await build.outputs[0].text();
  const assets = tileAssets(dashboard.config.crawl_path ?? resolve(root, "../crawl/crawl-ref/source/crawl-web-harness"));
  let viewers = 0;
  let tileViewers = 0;
  const headers = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy":
      "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'",
  };
  const reply = (body: BodyInit | null, status = 200, type = "text/plain") =>
    new Response(body, {
      status,
      headers: { ...headers, "Content-Type": type },
    });
  return Bun.serve({
    hostname: "127.0.0.1",
    port,
    idleTimeout: 30,
    maxRequestBodySize: 1024,
    async fetch(request, server) {
      const url = new URL(request.url);
      const host = request.headers.get("Host");
      const allowed = new Set([
        `127.0.0.1:${server.port}`,
        `localhost:${server.port}`,
      ]);
      if (!host || !allowed.has(host))
        return reply("Loopback host required", 403);
      const origin = request.headers.get("Origin");
      if (origin && origin !== `http://${host}`)
        return reply("Same-origin requests only", 403);
      if (request.method === "POST") {
        if (request.headers.get("X-Dashboard-Request") !== "1")
          return reply("Use dashboard controls", 403);
        if (url.pathname === "/defaults") {
          if (dashboard.active) return reply("An episode is already running", 409);
          try {
            const settings = saveDefaults(defaultsPath, await request.json());
            dashboard.updateConfig(settings);
            return reply(JSON.stringify(dashboard.config), 200, "application/json");
          } catch (error) {
            return reply(error instanceof Error ? error.message : "Unable to save defaults", 400);
          }
        }
        if (url.pathname === "/config") {
          if (dashboard.active)
            return reply("An episode is already running", 409);
          try {
            dashboard.updateConfig(await request.json());
            return reply(JSON.stringify(dashboard.config), 200, "application/json");
          } catch (error) {
            return reply(error instanceof Error ? error.message : "Invalid settings",
              dashboard.active ? 409 : 400);
          }
        }
        if (url.pathname === "/run")
          return dashboard.start()
            ? reply("Started", 202)
            : reply("An episode is already running", 409);
        if (url.pathname === "/stop") {
          dashboard.stop();
          return reply("Stopping", 202);
        }
      }
      if (request.method !== "GET") return reply("Not found", 404);
      if (url.pathname === "/tiles/") {
        return new Response(tilePage(assets.markup, assets.ready), {
          headers: { ...headers, "Content-Type": "text/html", "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'self'; base-uri 'none'; form-action 'none'" },
        });
      }
      if (url.pathname === "/tiles/viewer.js") return reply(tileScript, 200, "text/javascript");
      if (url.pathname === "/tiles/viewer.css") return reply(tileStyle, 200, "text/css");
      if (url.pathname === "/tiles/events") {
        if (tileViewers >= 8) return reply("Too many viewers", 503);
        server.timeout(request, 0);
        tileViewers++;
        return reply(tileEvents(dashboard.tiles, request, () => tileViewers--), 200, "text/event-stream");
      }
      const asset = assets.files.get(url.pathname);
      if (asset) return reply(Bun.file(asset), 200, Bun.file(asset).type);
      // xterm generates scoped CSS at runtime. Inline scripts
      // remain forbidden; all fonts and library assets are served locally.
      const fonts: Record<string, string> = {
        "/assets/sans-400.woff2":
          "ibm-plex-sans/files/ibm-plex-sans-latin-400-normal.woff2",
        "/assets/sans-500.woff2":
          "ibm-plex-sans/files/ibm-plex-sans-latin-500-normal.woff2",
        "/assets/sans-600.woff2":
          "ibm-plex-sans/files/ibm-plex-sans-latin-600-normal.woff2",
        "/assets/mono-400.woff2":
          "ibm-plex-mono/files/ibm-plex-mono-latin-400-normal.woff2",
      };
      if (fonts[url.pathname])
        return reply(
          Bun.file(
            resolve(
              import.meta.dir,
              "node_modules/@fontsource",
              fonts[url.pathname],
            ),
          ),
          200,
          "font/woff2",
        );
      if (url.pathname === "/assets/xterm.css")
        return reply(
          Bun.file(
            resolve(import.meta.dir, "node_modules/@xterm/xterm/css/xterm.css"),
          ),
          200,
          "text/css",
        );
      if (url.pathname === "/")
        return reply(
          Bun.file(resolve(import.meta.dir, "index.html")),
          200,
          "text/html",
        );
      if (url.pathname === "/assets/style.css")
        return reply(
          Bun.file(resolve(import.meta.dir, "style.css")),
          200,
          "text/css",
        );
      if (url.pathname === "/assets/app.js")
        return reply(script, 200, "text/javascript");
      if (url.pathname === "/state")
        return reply(JSON.stringify(dashboard.state), 200, "application/json");
      if (url.pathname === "/events") {
        if (viewers >= 8) return reply("Too many viewers", 503);
        server.timeout(request, 0);
        viewers++;
        let timer: ReturnType<typeof setInterval>;
        let disposed = false;
        const dispose = () => {
          if (!disposed) {
            disposed = true;
            clearInterval(timer);
            viewers--;
          }
        };
        const stream = new ReadableStream<Uint8Array>({
          start(controller) {
            let revision = -1;
            let sent = 0;
            const tick = () => {
              if (disposed || (controller.desiredSize ?? 0) <= 0) return;
              if (revision !== dashboard.revision) {
                controller.enqueue(
                  new TextEncoder().encode(
                    `data: ${JSON.stringify(dashboard.state)}\n\n`,
                  ),
                );
                revision = dashboard.revision;
                sent = Date.now();
              } else if (Date.now() - sent > 15000) {
                controller.enqueue(new TextEncoder().encode(": keepalive\n\n"));
                sent = Date.now();
              }
            };
            timer = setInterval(tick, 50);
            tick();
            request.signal.addEventListener(
              "abort",
              () => {
                dispose();
                try {
                  controller.close();
                } catch {}
              },
              { once: true },
            );
          },
          cancel: dispose,
        });
        return reply(stream, 200, "text/event-stream");
      }
      return reply("Not found", 404);
    },
  });
}

export function dashboardOptions(args: string[], defaults?: Settings) {
  const { values } = parseArgs({
    args,
    options: {
      policy: { type: "string", default: "codex" },
      model: { type: "string" },
      port: { type: "string", default: "8765" },
      "reasoning-effort": { type: "string" },
      "max-turns": { type: "string" },
      "action-turn-limit": { type: "string" },
      "review-turn-limit": { type: "string" },
      "episode-limit": { type: "string" },
      "reasoning-summary": { type: "boolean" },
      "crawl-path": { type: "string" },
      "manual-path": { type: "string" },
    },
  });
  const backend = values.policy as Config["backend"];
  const saved = backend === "codex" ? defaults : undefined;
  const model = values.model ?? saved?.model ?? (backend === "codex" ? "gpt-5.6-luna" : null);
  const port = Number(values.port),
    max_turns = Number(values["action-turn-limit"] ?? values["max-turns"] ?? saved?.action_turn_limit ?? saved?.max_turns ?? 3);
  const selectedLimits = limits({ action_turn_limit: max_turns,
    review_turn_limit: Number(values["review-turn-limit"] ?? saved?.review_turn_limit ?? 3),
    episode_limit: Number(values["episode-limit"] ?? saved?.episode_limit ?? 1) });
  const reasoning_effort = (values["reasoning-effort"] ?? saved?.reasoning_effort ??
    (backend === "codex" ? "low" : "default")) as Config["reasoning_effort"];
  if (
    !["codex", "pydantic"].includes(backend) ||
    !Number.isInteger(port) ||
    port < 1 ||
    port > 65535 ||
    !reasoningEfforts.includes(reasoning_effort) ||
    !Number.isSafeInteger(max_turns) ||
    max_turns < 0 ||
    !model
  ) {
    throw new Error(
      "Use --policy codex, or --policy pydantic --model <name>; limits must be nonnegative integers.",
    );
  }
  const config: Config = {
    backend,
    model,
    reasoning_effort,
    max_turns,
    ...selectedLimits,
    crawl_path: values["crawl-path"] ? resolve(values["crawl-path"]) : undefined,
    manual_path: values["manual-path"] ? resolve(values["manual-path"]) : undefined,
    reasoning_summary: values["reasoning-summary"] ?? backend === "codex",
  };
  return { config, port };
}

if (import.meta.main) {
  const { config, port } = dashboardOptions(Bun.argv.slice(2), loadDefaults(defaultSettingsPath));
  const dashboard = new Dashboard(config);
  const server = await startServer(dashboard, port);
  console.log(
    `Dashboard: http://127.0.0.1:${server.port} — click Start to run.`,
  );
  let shuttingDown = false;
  const shutdown = async () => {
    if (shuttingDown) return;
    shuttingDown = true;
    dashboard.stop();
    await dashboard.completion;
    await server.stop(true);
    process.exit(0);
  };
  process.on("SIGINT", shutdown);
  process.on("SIGTERM", shutdown);
}
