import { WORKSPACE_ROUTE_EPISODES } from "@/app-routes";

/** 「分集」视图的查询参数：`upload` 打开上传对话框并预选方式，`episode` 选中一集，`create` 打开新建一集对话框。 */
export const EPISODES_VIEW_EPISODE_PARAM = "episode";
export const EPISODES_VIEW_CREATE_PARAM = "create";


/** 「分集」视图的地址（工作区内的相对路由）。 */
export function episodesViewPath(
  options: { episode?: number; create?: boolean } = {},
): string {
  const params = new URLSearchParams();
  if (options.create) params.set(EPISODES_VIEW_CREATE_PARAM, "1");
  if (options.episode !== undefined) params.set(EPISODES_VIEW_EPISODE_PARAM, String(options.episode));
  const query = params.toString();
  return `/${WORKSPACE_ROUTE_EPISODES}${query ? `?${query}` : ""}`;
}
