import type { MoaModelSlot } from "./api";

export function selectMoaAggregatorModel(
  current: MoaModelSlot,
  provider: string,
  model: string,
): MoaModelSlot {
  if (
    provider.trim().toLowerCase() === "anthropic" &&
    current.runtime
  ) {
    return { provider, model, runtime: current.runtime };
  }
  return { provider, model };
}

export function setMoaAggregatorRuntime(
  current: MoaModelSlot,
  runtime: "hermes" | "claude_agent_sdk",
): MoaModelSlot {
  if (runtime === "claude_agent_sdk") {
    return { ...current, runtime };
  }
  const nativeSlot = { ...current };
  delete nativeSlot.runtime;
  return nativeSlot;
}
