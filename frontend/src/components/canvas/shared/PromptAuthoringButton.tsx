import { useTranslation } from "react-i18next";
import { PenLine } from "lucide-react";
import { promptAuthoringResourceId } from "@/actions/generation";
import { Button } from "@/components/ui/button";
import { useFactoryEpisodeSource } from "@/components/canvas/episode-page/useFactoryEpisodeSource";
import {
  usePromptAuthoringStore,
  type PromptAuthoringField,
  type PromptAuthoringScope,
} from "@/stores/prompt-authoring-store";
import { useActiveResourceIds } from "@/stores/tasks-store";

interface Props {
  projectName: string;
  episode: number;
  scope: PromptAuthoringScope;
  currentEntryId?: string | null;
  variant?: "outline" | "ghost" | "default";
  /** 按钮尺寸：默认 sm；分镜详情里与相邻的生成按钮对齐用 xs。 */
  size?: "xs" | "sm";
  /** 额外的布局类名（如与相邻按钮取同一最小宽度）。 */
  className?: string;
  defaultRewrite?: boolean;
  defaultFields?: PromptAuthoringField[];
  lockFields?: boolean;
  label?: string;
}

/** 打开「编写提示词」弹窗的入口；本集的提示词编写在跑时禁用。 */
export function PromptAuthoringButton({
  projectName,
  episode,
  scope,
  currentEntryId,
  variant = "outline",
  size = "sm",
  className,
  defaultRewrite,
  defaultFields,
  lockFields,
  label,
}: Props) {
  const { t } = useTranslation("dashboard");
  const open = usePromptAuthoringStore((s) => s.open);
  const busy = useActiveResourceIds("text_episode_script", projectName).has(promptAuthoringResourceId(episode));
  // 分镜提示词入口前置资产完整性检查（D-10 第 0 步）：缺登记 / 缺描述时置灰并提示去资产页补齐。
  const isImageAuthoring = defaultFields?.length === 1 && defaultFields[0] === "image_prompt";
  const sourceState = useFactoryEpisodeSource(projectName, episode, isImageAuthoring);
  const missingAssets = isImageAuthoring ? (sourceState.value?.assets_missing_description ?? []) : [];
  const assetsBlocked = missingAssets.length > 0;
  const disabled = busy || assetsBlocked;
  const title = busy
    ? t("prompt_authoring_busy")
    : assetsBlocked
      ? t("prompt_authoring_assets_blocked", {
          count: missingAssets.length,
          assets: missingAssets.map((asset) => asset.name).join("、"),
        })
      : undefined;
  return (
    <Button
      variant={variant}
      size={size}
      className={className}
      disabled={disabled}
      title={title}
      onClick={() => open({
        projectName, episode, scope, currentEntryId, defaultRewrite, defaultFields, lockFields,
      })}
    >
      <PenLine aria-hidden data-icon="inline-start" />
      {label ?? t("prompt_authoring_open")}
    </Button>
  );
}
