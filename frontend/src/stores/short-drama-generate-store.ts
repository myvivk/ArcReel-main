import { create } from "zustand";

export interface ShortDramaGenerateOpenRequest {
  projectName: string;
  episode: number;
}

interface ShortDramaGenerateState {
  request: ShortDramaGenerateOpenRequest | null;
  open: (request: ShortDramaGenerateOpenRequest) => void;
  close: () => void;
}

/**
 * 剧情演绎「AI 原创剧本」弹窗的开关：集页上唯一的宿主按请求渲染。
 * 弹窗只负责把创意简报固化成一段指令、预填给助手（Agent 持有 short-drama-factory 技能与导入工具）。
 */
export const useShortDramaGenerateStore = create<ShortDramaGenerateState>((set) => ({
  request: null,
  open: (request) => set({ request }),
  close: () => set({ request: null }),
}));
