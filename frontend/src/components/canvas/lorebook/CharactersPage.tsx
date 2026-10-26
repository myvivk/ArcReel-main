import { useTranslation } from "react-i18next";
import type { Character } from "@/types";
import { AssetGallery } from "./AssetGallery";

interface Props {
  projectName: string;
  characters: Record<string, Character>;
  onGenerateCharacter: (name: string) => void;
  onRestoreCharacterVersion?: () => Promise<unknown> | void;
  onRefreshProject?: () => Promise<unknown> | void;
  generatingCharacterNames?: Set<string>;
  /** 只读展示（引导演示项目）：不渲染生成、上传、删除等改写入口。 */
  readOnly?: boolean;
}

export function CharactersPage({
  projectName,
  characters,
  onGenerateCharacter,
  onRestoreCharacterVersion,
  onRefreshProject,
  generatingCharacterNames,
  readOnly = false,
}: Props) {
  const { t } = useTranslation("dashboard");
  return (
    <AssetGallery
      projectName={projectName}
      assetType="character"
      title={t("characters")}
      assets={characters}
      generatingNames={generatingCharacterNames}
      readOnly={readOnly}
      onGenerate={onGenerateCharacter}
      onRestoreVersion={onRestoreCharacterVersion}
      onReload={onRefreshProject}
    />
  );
}
