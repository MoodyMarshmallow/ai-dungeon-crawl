import type { State, Submission } from "./types";
import { pythonAnsi, terminalText } from "./rendering";

/** Render submitted scripts and their live output as a read-only Python transcript. */
export function replTranscript(
  submissions: Submission[],
  highlighted = false,
): string {
  let value = "";
  for (const submission of submissions) {
    value +=
      (highlighted
        ? pythonAnsi(submission.code)
        : terminalText(submission.code)
      )
        .replace(/\r\n/g, "\n")
        .split("\n")
        .map((line, index) => `${index ? "... " : ">>> "}${line}`)
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
  if (submissions.at(-1)?.status !== "running") value += ">>> ";
  return value;
}

/** Interleave model responses and execution returns without visible turn metadata. */
export function activityEntries(state: Pick<State, "models" | "submissions">) {
  const result: {
    id: string;
    className: string;
    label: string;
    text: string;
    format: "python" | "json" | "markdown" | "text";
  }[] = [];
  const turns = new Set([
    ...state.models.map((model) => model.turn),
    ...state.submissions.map((submission) => submission.id),
  ]);
  for (const turn of [...turns].sort((a, b) => a - b)) {
    for (const request of state.models.filter((model) => model.turn === turn)) {
      for (const [index, part] of Object.entries(request.parts)) {
        let content = part.text;
        let format: "python" | "json" | "markdown" =
          part.kind === "tool" ? "json" : "markdown";
        if (part.kind === "tool") {
          try {
            const args = JSON.parse(content);
            if (typeof args.code === "string") {
              content = args.code;
              format = "python";
            }
          } catch {
            /* Partial arguments remain visible while streaming. */
          }
        }
        if (part.truncated) content += "\n[Preview truncated]";
        if (content)
          result.push({
            id: `model:${request.id}:${index}`,
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
          className: "result",
          label: "Execution result",
          text: content,
          format: "text",
        });
    }
  }
  return result;
}
