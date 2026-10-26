import { create } from "zustand";

import type { ShotGridPlan } from "@/types/shot-grid";
import type { ReferenceNameSets } from "@/utils/reference-mentions";

export interface ShotGridDialogRequest {
  projectName: string;
  scriptFile: string;
  episode: number;
  shotId: string;
  /** 打开时这一分镜已有的宫格记录；没有则为 null（首次生成）。 */
  plan: ShotGridPlan | null;
  /** 本分镜的出场资产清单：逐格提示词里的 @ 候选只从这份清单里取。 */
  referenceNames?: ReferenceNameSets;
}

interface ShotGridDialogState {
  request: ShotGridDialogRequest | null;
  open: (request: ShotGridDialogRequest) => void;
  close: () => void;
}

/**
 * 「参考素材」弹窗的开关：分镜详情只登记要打开哪个分镜、带上它当前的宫格记录，
 * 弹窗由分镜详情上唯一的宿主渲染。规格与逐格文本都在弹窗里定；AI 生成返回与关窗都会把
 * 逐格文本存成草稿，「确定」才落盘并入队（ADR 0099 单个分镜参考素材生成）。
 */
export const useShotGridDialogStore = create<ShotGridDialogState>((set) => ({
  request: null,
  open: (request) => set({ request }),
  close: () => set({ request: null }),
}));
