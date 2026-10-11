import { create } from "zustand";

export interface ShortDramaImportOpenRequest {
  projectName: string;
  /** 预选的集（集 ID）；缺省时弹窗内自选。 */
  episode?: number;
}

interface ShortDramaImportState {
  request: ShortDramaImportOpenRequest | null;
  open: (request: ShortDramaImportOpenRequest) => void;
  close: () => void;
}

/**
 * 短剧工厂「导入剧本」弹窗的开关：登记要打开的项目与可选的预选集，弹窗由集页上唯一的宿主渲染。
 */
export const useShortDramaImportStore = create<ShortDramaImportState>((set) => ({
  request: null,
  open: (request) => set({ request }),
  close: () => set({ request: null }),
}));
