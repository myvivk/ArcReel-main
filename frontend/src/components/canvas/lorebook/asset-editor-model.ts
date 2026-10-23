import { API } from "@/api";
import type { AssetSheetType, Character } from "@/types";
import type { GalleryAssetSource } from "./gallery-model";

/**
 * 详情编辑器里进入未保存修改的字段。四类资产共用一个形状，按类型只用到其中几项：
 * 角色有声音风格，商品有品牌与卖点。改名、上传、增删别名是立即执行的动作，不在这里。
 */
export interface AssetFields {
  description: string;
  voiceStyle: string;
}

/** 新建资产时多一个名称。 */
export interface NewAssetFields extends AssetFields {
  name: string;
}

export const EMPTY_ASSET_FIELDS: AssetFields = { description: "", voiceStyle: "" };

export function assetFieldsOf(type: AssetSheetType, asset: GalleryAssetSource): AssetFields {
  return {
    description: asset.description ?? "",
    voiceStyle: type === "character" ? ((asset as Character).voice_style ?? "") : "",
  };
}

export function assetFieldsEqual(a: AssetFields, b: AssetFields): boolean {
  return a.description === b.description && a.voiceStyle === b.voiceStyle;
}

/** 只带改过的字段，键名是资产接口的字段名。 */
export function assetFieldsPatch(type: AssetSheetType, value: AssetFields, saved: AssetFields): Record<string, unknown> {
  const patch: Record<string, unknown> = {};
  if (value.description !== saved.description) patch.description = value.description;
  if (type === "character" && value.voiceStyle !== saved.voiceStyle) patch.voice_style = value.voiceStyle;
  return patch;
}

export function updateAssetFields(
  projectName: string,
  type: AssetSheetType,
  name: string,
  patch: Record<string, unknown>,
): Promise<unknown> {
  switch (type) {
    case "character":
      return API.updateCharacter(projectName, name, patch);
    case "scene":
      return API.updateProjectScene(projectName, name, patch);
    case "prop":
      return API.updateProjectProp(projectName, name, patch);
  }
}

export function createAsset(projectName: string, type: AssetSheetType, fields: NewAssetFields): Promise<unknown> {
  const name = fields.name.trim();
  switch (type) {
    case "character":
      return API.addCharacter(projectName, name, fields.description, fields.voiceStyle);
    case "scene":
      return API.addProjectScene(projectName, name, fields.description);
    case "prop":
      return API.addProjectProp(projectName, name, fields.description);
  }
}
