export const reasoningEfforts = ["default", "none", "minimal", "low", "medium", "high", "xhigh"] as const;
export type ReasoningEffort = (typeof reasoningEfforts)[number];
export interface Config {
  backend: "codex" | "pydantic";
  model: string | null;
  reasoning_effort: ReasoningEffort;
  max_turns?: number;
  action_turn_limit?: number;
  review_turn_limit?: number;
  episode_limit?: number;
  crawl_path?: string;
  manual_path?: string;
  reasoning_summary: boolean;
}
export interface Observation {
  id: number;
  screen: string;
  ended: boolean;
  width?: number;
  height?: number;
  styles?: ScreenStyle[];
  cursor?: [number, number] | null;
}
export interface ScreenStyle {
  row: number;
  col: number;
  length: number;
  fg: string;
  bg: string;
  bold: boolean;
  italics: boolean;
  underline: boolean;
  reverse: boolean;
  blink: boolean;
}
export interface ModelPart {
  kind: "text" | "reasoning" | "tool";
  name: string;
  text: string;
  truncated: boolean;
}
export interface ModelRequest {
  mode?: "action" | "review";
  episode?: number;
  id: number;
  turn: number;
  status: string;
  tokens: number | null;
  parts: Record<string, ModelPart>;
}
export interface Submission {
  mode?: "action" | "review";
  episode?: number;
  observation?: Observation;
  id: number;
  code: string;
  model_requests: number | null;
  status: string;
  output: string;
  error: string | null;
  output_truncated: boolean;
  keys: string[];
}
export interface State {
  mode: "action" | "review";
  episode: number;
  config: Config;
  status: "idle" | "running" | "completed" | "stopped" | "error";
  phase: string;
  run_id: number;
  started_at: string | null;
  finished_at: string | null;
  observation: Observation | null;
  actions: number;
  turn: number | null;
  requests: number;
  input_tokens: number;
  output_tokens: number;
  models: ModelRequest[];
  submissions: Submission[];
  error: string | null;
  stop_reason: string | null;
}
export type HarnessEvent =
  | { event: "mode.changed"; data: { mode: "action" | "review"; episode: number } }
  | { event: "game.tiles"; data: { messages: Record<string, unknown>[] } }
  | { event: "game.observation"; data: Observation }
  | {
      event: "game.step";
      data: { key: string; count: number; observation: Observation };
    }
  | { event: "turn.started"; data: { id: number } }
  | { event: "model.started"; data: Record<string, never> }
  | {
      event: "model.part";
      data: {
        index: number;
        kind: ModelPart["kind"];
        name?: string;
        text: string | Record<string, unknown>;
        replace: boolean;
      };
    }
  | {
      event: "model.finished";
      data: {
        finish_reason: string | null;
        input_tokens: number;
        output_tokens: number;
      };
    }
  | {
      event: "execution.submitted";
      data: { id: number; code: string; model_requests: number | null };
    }
  | { event: "execution.output"; data: { text: string } }
  | {
      event: "execution.finished";
      data: {
        observation?: Observation;
        id: number;
        output: string;
        error: string | null;
        status: string;
        output_truncated: boolean;
      };
    }
  | {
      event: "episode.finished";
      data: {
        status: "completed" | "stopped" | "error";
        stop_reason?: string;
        error?: string;
      };
    };
