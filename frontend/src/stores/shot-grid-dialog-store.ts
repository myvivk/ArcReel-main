import { create } from "zustand";

import type { ShotGridPlan } from "@/types/shot-grid";

export interface ShotGridDialogRequest {
  projectName: string;
  scriptFile: string;
  episode: number;
  shotId: string;
  /** 打开时这一分镜已有的宫格记录；没有则为 null（首次生成）。 */
  plan: ShotGridPlan | null;
}

interface ShotGridDialogState {
  request: ShotGridDialogRequest | null;
  open: (request: ShotGridDialogRequest) => void;
  close: () => void;
}

/**
 * 「参考素材」弹窗的开关：分镜详情只登记要打开哪个分镜、带上它当前的宫格记录，
 * 弹窗由分镜详情上唯一的宿主渲染。规格与逐格文本都在弹窗里定，「确定」才落盘入队，
 * 「取消」不写入任何东西（ADR 0099 单个分镜参考素材生成）。
 */
export const useShotGridDialogStore = create<ShotGridDialogState>((set) => ({
  request: null,
  open: (request) => set({ request }),
  close: () => set({ request: null }),
}));
