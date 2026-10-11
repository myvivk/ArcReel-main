import { create } from "zustand";

interface ShortDramaVisualState {
  projectName: string | null;
  episode: number | null;
  open: (projectName: string, episode: number) => void;
  close: () => void;
}

/** drama 集「AI 生成剧本（补视觉层）」弹窗开关：只补缺 image/video 提示词，不重写、不弹范围选择。 */
export const useShortDramaVisualStore = create<ShortDramaVisualState>((set) => ({
  projectName: null,
  episode: null,
  open: (projectName, episode) => set({ projectName, episode }),
  close: () => set({ projectName: null, episode: null }),
}));
