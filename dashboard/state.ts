import type { Config, HarnessEvent, State } from "./types";

export function emptyState(config: Config, run_id = 0): State {
  return {
    config,
    status: "idle",
    phase: "Ready to run",
    run_id,
    started_at: null,
    finished_at: null,
    observation: null,
    actions: 0,
    turn: null,
    requests: 0,
    input_tokens: 0,
    output_tokens: 0,
    models: [],
    submissions: [],
    error: null,
    stop_reason: null,
  };
}

/** Bounded display state, independent of the complete episode records in Python. */
export function applyEvent(state: State, message: HarnessEvent): void {
  const model = state.models.at(-1);
  const submission = state.submissions.at(-1);
  switch (message.event) {
    case "game.observation":
      state.observation = message.data;
      break;
    case "game.step":
      state.observation = message.data.observation;
      state.actions = message.data.count;
      if (submission) {
        submission.keys.push(message.data.key);
        submission.keys = submission.keys.slice(-100);
      }
      break;
    case "turn.started":
      state.turn = message.data.id;
      state.phase = "Waiting for model";
      break;
    case "model.started":
      if (model && model.status === "received")
        model.status = "repair requested";
      state.requests++;
      state.phase = "Model streaming";
      state.models.push({
        id: state.requests,
        turn: state.turn ?? 0,
        status: "streaming",
        parts: {},
        tokens: null,
      });
      state.models = state.models.slice(-30);
      break;
    case "model.part": {
      if (!model) break;
      const data = message.data;
      const index = String(data.index);
      if (!model.parts[index] && Object.keys(model.parts).length >= 16) break;
      if (data.replace || !model.parts[index])
        model.parts[index] = {
          kind: data.kind,
          name: "",
          text: "",
          truncated: false,
        };
      const part = model.parts[index];
      part.name = (part.name + (data.name ?? "")).slice(0, 200);
      let content = data.text;
      if (typeof content !== "string") {
        let previous = {};
        try {
          const parsed = JSON.parse(part.text || "{}");
          if (parsed && !Array.isArray(parsed) && typeof parsed === "object")
            previous = parsed;
        } catch {}
        content = JSON.stringify({ ...previous, ...content });
        part.text = "";
      }
      const combined = part.text + content;
      part.text = combined.slice(0, 65536);
      part.truncated ||= combined.length > 65536;
      break;
    }
    case "model.finished":
      if (model) {
        model.status = "received";
        model.tokens = message.data.output_tokens;
      }
      state.input_tokens += message.data.input_tokens;
      state.output_tokens += message.data.output_tokens;
      state.phase = "Validating output tool";
      break;
    case "repl.submitted":
      state.phase = "Executing Python";
      state.submissions.push({
        ...message.data,
        output: "",
        error: null,
        status: "running",
        output_truncated: false,
        keys: [],
      });
      state.submissions = state.submissions.slice(-30);
      if (model) model.status = "accepted";
      break;
    case "repl.output":
      if (submission)
        submission.output = (submission.output + message.data.text).slice(
          0,
          65536,
        );
      break;
    case "repl.finished":
      if (submission) {
        const { output, error, status, output_truncated } = message.data;
        Object.assign(submission, { output, error, status, output_truncated });
      }
      break;
    case "episode.finished":
      state.status = message.data.status;
      state.phase =
        state.status === "completed"
          ? "Episode finished"
          : state.status === "stopped"
            ? "Stopped by you"
            : "Episode failed";
      state.stop_reason = message.data.stop_reason ?? null;
      state.finished_at = new Date().toISOString();
      if (state.status === "error")
        state.error = `${message.data.error ?? "Harness error"}: run failed. Check local authentication, model support and sandbox availability. No automatic retry.`;
      for (const item of state.submissions)
        if (item.status === "running") item.status = "interrupted";
      for (const item of state.models)
        if (item.status === "streaming") item.status = "interrupted";
      break;
  }
}
