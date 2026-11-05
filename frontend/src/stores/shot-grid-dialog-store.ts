import { create } from "zustand";

import type { ShotGridLayout, ShotGridPlan } from "@/types/shot-grid";

export interface ShotGridDialogRequest {
  projectName: string;
  scriptFile: string;
  episode: number;
  shotId: string;
  /** 打开时这一分镜已有的宫格记录；没有则为 null（首次生成）。 */
  plan: ShotGridPlan | null;
  layout?: ShotGridLayout;
  /** 已确认宫格走「重新生成」时打开：允许再次生成，规格仍锁定。 */
  allowConfirmedRegenerate?: boolean;
}

interface ShotGridDialogState {
  request: ShotGridDialogRequest | null;
  open: (request: ShotGridDialogRequest) => void;
  close: () => void;
}

/**
 * 「参考素材」弹窗的开关：分镜详情只登记要打开哪个分镜、带上它当前的宫格记录，
 * 弹窗由分镜详情上唯一的宿主渲染。弹窗只设置规格；画面内容从当前分镜提示词解析，确定后入队。
 */
export const useShotGridDialogStore = create<ShotGridDialogState>((set) => ({
  request: null,
  open: (request) => set({ request }),
  close: () => set({ request: null }),
}));
