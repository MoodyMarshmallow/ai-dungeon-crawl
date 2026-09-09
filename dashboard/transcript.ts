import type { State, Submission } from "./types";
import { shellAnsi, terminalText } from "./rendering";

/** Use the first available reasoning line without including later thoughts or tool calls. */
export function turnSummary(state: Pick<State, "models">, turn: number): string {
  for (const model of state.models.filter(model => model.turn === turn)) {
    for (const part of Object.values(model.parts)) {
      if (part.kind !== "reasoning") continue;
      const first = part.text.split(/\r?\n/).find(line => line.trim());
      if (!first) continue;
      const heading = first.trim().replace(/^#{1,6}\s+/, "")
        .replace(/\s+#+$/, "").replace(/[*_`]/g, "").trim();
      if (heading) return `Turn ${turn + 1}: ${heading}`;
    }
  }
  return `Turn ${turn + 1}`;
}

/** Render submitted shell commands and their live output as a read-only transcript. */
export function shellTranscript(
  submissions: Submission[],
  highlighted = false,
): string {
  let value = "";
  for (const submission of submissions) {
    value +=
      (highlighted
        ? shellAnsi(submission.code)
        : terminalText(submission.code)
      )
        .replace(/\r\n/g, "\n")
        .split("\n")
        .map((line, index) => `${index ? "> " : "$ "}${line}`)
        .join("\n") + "\n";
    value += highlighted
      ? `\x1b[38;2;147;187;216m${terminalText(submission.output)}\x1b[39m`
      : terminalText(submission.output);
    if (submission.output && !submission.output.endsWith("\n")) value += "\n";
    if (submission.error) value += terminalText(submission.error) + "\n";
    if (submission.output_truncated) value += "[Output truncated]\n";
    if (submission.status === "interrupted" && !submission.error)
      value += "[Interrupted]\n";
  }
  if (submissions.at(-1)?.status !== "running") value += "$ ";
  return value;
}

/** Interleave model responses and execution returns within their agent turns. */
export function activityEntries(state: Pick<State, "models" | "submissions">) {
  const result: {
    id: string;
    turn: number;
    className: string;
    label: string;
    text: string;
    format: "bash" | "python" | "json" | "markdown" | "text";
  }[] = [];
  const turns = new Set([
    ...state.models.map((model) => model.turn),
    ...state.submissions.map((submission) => submission.id),
  ]);
  for (const turn of [...turns].sort((a, b) => a - b)) {
    for (const request of state.models.filter((model) => model.turn === turn)) {
      for (const [index, part] of Object.entries(request.parts)) {
        let content = part.text;
        let format: "bash" | "python" | "json" | "markdown" =
          part.kind === "tool" ? "json" : "markdown";
        if (part.kind === "tool") {
          try {
            const args = JSON.parse(content);
            if (typeof args.code === "string") {
              content = args.code;
              format = part.name === "execute_python" ? "python" : "bash";
            }
          } catch {
            /* Partial arguments remain visible while streaming. */
          }
        }
        if (part.truncated) content += "\n[Preview truncated]";
        if (content)
          result.push({
            id: `model:${request.id}:${index}`,
            turn,
            className: `model-part ${part.kind}`,
            label:
              part.kind === "tool"
                ? part.name || "Tool call"
                : "Model response",
            text: content,
            format,
          });
      }
    }
    for (const submission of state.submissions.filter(
      (item) => item.id === turn,
    )) {
      let content = submission.output;
      if (submission.error)
        content +=
          (content.endsWith("\n") || !content ? "" : "\n") + submission.error;
      if (submission.output_truncated) content += "\n[Output truncated]";
      if (!content && submission.status !== "running")
        content = submission.status === "ok" ? "No output" : submission.status;
      if (content)
        result.push({
          id: `result:${submission.id}`,
          turn,
          className: "result",
          label: "Execution result",
          text: content,
          format: "text",
        });
    }
  }
  return result;
}
