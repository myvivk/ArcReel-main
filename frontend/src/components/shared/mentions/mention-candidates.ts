import type { ProjectData } from "@/types";
import type { AssetKind } from "@/types/reference-video";
import type { ReferenceNameSets } from "@/utils/reference-mentions";
import { resolveCharacterForm } from "@/utils/reference-mentions";
import type { MentionCandidateGroups } from "./MentionPicker";

/** 分镜类提示词只引用角色 / 场景 / 道具：商品不在分镜的出场资产清单里。 */
export const SHOT_MENTION_KINDS: AssetKind[] = ["character", "scene", "prop"];

/**
 * 一处引用范围内的可插入候选。
 *
 * 只列引用范围里的名字：token 落在参考图列表内，出图时才指认得动对应的「图N」；范围外的
 * 已登记资产写进正文只会被格式闸判不合格，因此不进候选。
 *
 * 缩略图取该资产自己的资产表图。衍生取衍生那条的图——它才是这套外观；没有图时留空，
 * 由候选列表画类型色点。
 */
export function buildMentionCandidates(
  project: ProjectData | null | undefined,
  names: ReferenceNameSets,
): MentionCandidateGroups {
  return {
    character: names.characters.map((name) => ({
      name,
      imagePath: resolveCharacterForm(project?.characters, name)?.asset.character_sheet ?? null,
    })),
    scene: names.scenes.map((name) => ({
      name,
      imagePath: project?.scenes?.[name]?.scene_sheet ?? null,
    })),
    prop: names.props.map((name) => ({
      name,
      imagePath: project?.props?.[name]?.prop_sheet ?? null,
    })),
  };
}
