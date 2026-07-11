import { describe, expect, it } from "vitest";

import {
  selectMoaAggregatorModel,
  setMoaAggregatorRuntime,
} from "./moa";

describe("MoA aggregator runtime editing", () => {
  const maxBacked = {
    provider: "anthropic",
    model: "claude-fable-5",
    runtime: "claude_agent_sdk",
  };

  it("preserves Claude Max runtime across Anthropic model edits", () => {
    expect(
      selectMoaAggregatorModel(maxBacked, "anthropic", "claude-opus-4-6"),
    ).toEqual({
      provider: "anthropic",
      model: "claude-opus-4-6",
      runtime: "claude_agent_sdk",
    });
  });

  it("removes Claude runtime when switching to another provider", () => {
    expect(
      selectMoaAggregatorModel(maxBacked, "openrouter", "openai/gpt-5"),
    ).toEqual({ provider: "openrouter", model: "openai/gpt-5" });
  });

  it("requires an explicit native-runtime selection to remove Claude Max", () => {
    expect(setMoaAggregatorRuntime(maxBacked, "hermes")).toEqual({
      provider: "anthropic",
      model: "claude-fable-5",
    });
  });
});
