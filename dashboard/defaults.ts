import { readFileSync, writeFileSync, renameSync } from "node:fs";
import type { Config } from "./types";
import { reasoningEfforts } from "./types";

export type Settings = Pick<Config, "model" | "reasoning_effort" | "max_turns" | "action_turn_limit" | "review_turn_limit" | "episode_limit">;

export function limits(fields: Record<string, unknown>) {
  const action_turn_limit = fields.action_turn_limit ?? fields.max_turns;
  const review_turn_limit = fields.review_turn_limit ?? 3;
  const episode_limit = fields.episode_limit ?? 1;
  if (![action_turn_limit, review_turn_limit].every(value => typeof value === "number" && Number.isSafeInteger(value) && value >= 0) ||
      typeof episode_limit !== "number" || !Number.isSafeInteger(episode_limit) || episode_limit < 1 ||
      (fields.max_turns !== undefined && (typeof fields.max_turns !== "number" || !Number.isSafeInteger(fields.max_turns) || fields.max_turns < 0)))
    throw new Error("Turn limits must be nonnegative whole numbers; episode limit must be positive");
  return { action_turn_limit: action_turn_limit as number, review_turn_limit: review_turn_limit as number, episode_limit };
}

export function validateDefaults(value: unknown): Settings {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("Invalid defaults");
  const fields = value as Record<string, unknown>;
  if (Object.keys(fields).some(key => !["model", "reasoning_effort", "max_turns", "action_turn_limit", "review_turn_limit", "episode_limit"].includes(key)) ||
      !["gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol"].includes(fields.model as string) ||
      fields.reasoning_effort === "default" ||
      !reasoningEfforts.includes(fields.reasoning_effort as Config["reasoning_effort"]))
    throw new Error("Choose a model, reasoning strength, and nonnegative turn limit");
  return { model: fields.model as string, reasoning_effort: fields.reasoning_effort as Config["reasoning_effort"],
    ...limits(fields) };
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
