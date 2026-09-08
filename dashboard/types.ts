export interface Config {
  backend: "codex" | "pydantic";
  model: string | null;
  max_steps: number;
  max_turns: number;
  game: string;
  reasoning_summary: boolean;
}
export interface Observation {
  id: number;
  screen: string;
  ended: boolean;
}
export interface ModelPart {
  kind: "text" | "reasoning" | "tool";
  name: string;
  text: string;
  truncated: boolean;
}
export interface ModelRequest {
  id: number;
  turn: number;
  status: string;
  tokens: number | null;
  parts: Record<string, ModelPart>;
}
export interface Submission {
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
      event: "repl.submitted";
      data: { id: number; code: string; model_requests: number | null };
    }
  | { event: "repl.output"; data: { text: string } }
  | {
      event: "repl.finished";
      data: {
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
