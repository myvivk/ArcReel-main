import { useAppStore } from "@/stores/app-store";
import { useAssistantStore } from "@/stores/assistant-store";

/** 将任务说明放入 Agent 输入框，交由用户发送。 */
export function prefillAssistant(text: string): void {
  useAssistantStore.getState().setInput(text);
  useAppStore.getState().setAssistantPanelOpen(true);
}
