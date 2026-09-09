import { readFileSync, writeFileSync, renameSync } from "node:fs";
import type { Config } from "./types";
import { reasoningEfforts } from "./types";

export type Settings = Pick<Config, "model" | "reasoning_effort" | "max_turns">;

export function validateDefaults(value: unknown): Settings {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("Invalid defaults");
  const fields = value as Record<string, unknown>;
  if (Object.keys(fields).some(key => !["model", "reasoning_effort", "max_turns"].includes(key)) ||
      !["gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol"].includes(fields.model as string) ||
      fields.reasoning_effort === "default" ||
      !reasoningEfforts.includes(fields.reasoning_effort as Config["reasoning_effort"]) ||
      !Number.isSafeInteger(fields.max_turns) || (fields.max_turns as number) < 0)
    throw new Error("Choose a model, reasoning strength, and nonnegative turn limit");
  return { model: fields.model as string, reasoning_effort: fields.reasoning_effort as Config["reasoning_effort"],
    max_turns: fields.max_turns as number };
}

export function loadDefaults(path: string): Settings | undefined {
  try { return validateDefaults(JSON.parse(readFileSync(path, "utf8"))); }
  catch (error) {
    if ((error as NodeJS.ErrnoException).code !== "ENOENT") console.warn("Ignoring invalid dashboard defaults.");
    return undefined;
  }
}

export function saveDefaults(path: string, value: unknown): Settings {
  const settings = validateDefaults(value);
  writeFileSync(path + ".tmp", JSON.stringify(settings) + "\n", { mode: 0o600 });
  renameSync(path + ".tmp", path);
  return settings;
}
