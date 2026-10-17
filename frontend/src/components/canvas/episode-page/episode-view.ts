/**
 * 集页视图路由：当前视图记在地址的 `?view=` 上，取值与可用条件只在这里定义。
 *
 * | 取值 | 视图 | 何时出现 |
 * | --- | --- | --- |
 * | `setting` | 故事设定（广告项目的梗概、类型、主题与世界观） | 只在广告项目出现，排第一 |
 * | `plan` | 脚本规划（内容确认页；剧本未生成时是集原文确认） | 非广告项目，有脚本规划中间稿、集原文待确认，或参考生视频项目 |
 * | `grid` | 多宫格分镜图 | 分镜图生视频且开启宫格；集原文待确认时不可选 |
 * | `board` | 分镜（参考生视频项目称「视频单元」） | 恒有；有脚本规划却还没有正式剧本时不可选 |
 * | `edit` | 剪辑 | 有正式剧本的非演示项目 |
 * | `source` | 集原文（就地查看与编辑，改后需重新规划脚本） | 非广告、非演示，且集原文确认不在进行中（那时脚本规划视图已承担） |
 *
 * 缺省视图：有正式剧本时是 `board`，否则有脚本规划时是 `plan`。地址只在所选视图不是缺省视图时
 * 写 `view`，剧本生成后停在缺省视图上的页面因此自动落到分镜。取值无效或当前不可选时按缺省视图显示。
 */
import { EPISODE_VIEW_EDIT, EPISODE_VIEW_PARAM, EPISODE_VIEW_SOURCE } from "@/app-routes";
import type { GenerationRoute } from "@/utils/generation-mode";

export const EPISODE_VIEWS = ["setting", "plan", "grid", "board", EPISODE_VIEW_SOURCE, EPISODE_VIEW_EDIT] as const;
export type EpisodeView = (typeof EPISODE_VIEWS)[number];
/** 画布承担的视图；故事设定、集原文与剪辑视图替换整个画布。 */
export type CanvasView = Exclude<EpisodeView, "setting" | "edit" | "source">;

/** 决定集页有哪些视图的事实，由路由层按项目与这一集算出。 */
export interface EpisodeViewFacts {
  isAd: boolean;
  route: GenerationRoute;
  /** 分镜图生视频且开启了宫格分镜图。 */
  grid: boolean;
  /** 已有正式剧本。 */
  hasScript: boolean;
  /** 已有脚本规划中间稿（`script_status` 为 segmented 或 generated）。 */
  hasDraft: boolean;
  /** 剧本与中间稿都没有，脚本规划视图显示集原文确认。 */
  sourceReview: boolean;
  /** 演示项目：没有剪辑视图。 */
  demo: boolean;
  /** 本集正式脚本来自短剧工厂导入（顶层 short_drama_meta）：没有脚本规划时不给「脚本规划」页签。 */
  shortDrama?: boolean;
  /** The project can contain a factory episode Markdown artifact without a native script. */
  hasShortDramaSource?: boolean;
  /** 工作流计划里 script_plan 的产物态；取不到时视为没有（短剧工厂导入的集据此藏掉规划页签）。 */
  scriptPlanState?: string | null;
}

export interface EpisodeViewTab {
  view: EpisodeView;
  disabled: boolean;
}

function planAvailable(facts: EpisodeViewFacts): boolean {
  if (facts.isAd) return false;
  // 短剧工厂导入的集不走脚本规划（Markdown 直转正式脚本）：规划不存在时藏掉页签。
  // 之后真跑了原生规划，产物态变为 current/stale，页签自动回来（内容确认还要用它）。
  if (facts.shortDrama && facts.scriptPlanState !== "current" && facts.scriptPlanState !== "stale") return false;
  return facts.sourceReview || facts.hasDraft || (facts.route === "reference_video" && facts.hasScript);
}

/** 集页页头的视图 tab，按显示顺序排列：内容在前、产物在后（原文/剧本 → 分镜 → 剪辑）。 */
export function episodeViewTabs(facts: EpisodeViewFacts): EpisodeViewTab[] {
  const plan = planAvailable(facts);
  const tabs: EpisodeViewTab[] = [];
  if (facts.isAd) tabs.push({ view: "setting", disabled: false });
  if (plan) tabs.push({ view: "plan", disabled: false });
  // 集原文/剧本常驻可查可改，排在分镜之前——先读内容再看产物；脚本规划视图正在做集原文确认时
  // 不重复给（那已经是同一个编辑器）。
  if (
    !facts.isAd &&
    !facts.demo &&
    !facts.sourceReview &&
    (facts.hasShortDramaSource || facts.hasScript || facts.hasDraft)
  ) {
    tabs.push({ view: EPISODE_VIEW_SOURCE, disabled: false });
  }
  if (facts.grid) tabs.push({ view: "grid", disabled: facts.sourceReview });
  // 参考生视频的单元列表在中间稿阶段就可查看；分镜图生视频要等正式剧本。没有脚本规划时分镜是唯一视图。
  const boardEnabled =
    facts.hasScript || !plan || (facts.route === "reference_video" && !facts.sourceReview);
  tabs.push({ view: "board", disabled: !boardEnabled });
  if (facts.hasScript && !facts.demo) tabs.push({ view: EPISODE_VIEW_EDIT, disabled: false });
  return tabs;
}

export function defaultEpisodeView(facts: EpisodeViewFacts): EpisodeView {
  if (facts.hasScript) return "board";
  return planAvailable(facts) ? "plan" : "board";
}

/** 地址上的取值落到哪个视图：无效或不可选时按缺省视图。 */
export function resolveEpisodeView(requested: string | null, facts: EpisodeViewFacts): EpisodeView {
  const tab = episodeViewTabs(facts).find((entry) => entry.view === requested);
  return tab && !tab.disabled ? tab.view : defaultEpisodeView(facts);
}

/**
 * 切到 `next` 后的查询参数：缺省视图不写 `view`，其余参数原样保留。返回新对象、不改动传入的参数——
 * 跳转被离开拦截挡下时，当前地址的参数不能已经变了。
 */
export function withEpisodeView(params: URLSearchParams, next: EpisodeView, facts: EpisodeViewFacts): URLSearchParams {
  const result = new URLSearchParams(params);
  if (next === defaultEpisodeView(facts)) result.delete(EPISODE_VIEW_PARAM);
  else result.set(EPISODE_VIEW_PARAM, next);
  return result;
}

/**
 * 应用内跳转 `to` 之后是否仍停留在当前视图：同一集，且地址上的 `view` 按上面的规则落到同一个视图。
 *
 * 脚本规划、多宫格分镜图与分镜之间切换时画布实例不卸载，但画布里的编辑单元只挂在其中一个视图下
 * （分镜详情、视频单元正文只在分镜视图渲染）：画布实例不卸载不等于编辑单元不卸载。编辑单元把它作为
 * `allowNavigation`，只有不会卸载自己的跳转才不询问；去别的视图、剪辑视图或别的集都照常拦截。
 * `to` 与 `here` 都带嵌套路由的 base，`view` 缺省时按 `facts` 算出的缺省视图判断。
 */
export function staysInEpisodeView(to: string, here: string, facts: EpisodeViewFacts): boolean {
  const current = new URL(here, "http://episode-view.invalid");
  const target = new URL(to, current);
  if (target.pathname !== current.pathname) return false;
  return (
    resolveEpisodeView(target.searchParams.get(EPISODE_VIEW_PARAM), facts) ===
    resolveEpisodeView(current.searchParams.get(EPISODE_VIEW_PARAM), facts)
  );
}
