import { Loader2, Sparkles } from "lucide-react";
import { useTranslation } from "react-i18next";
import { Button } from "@/components/ui/button";

import { getProjectDisplayName } from "@/utils/project-display";

/**
 * 空项目欢迎页：AI 原创剧本。
 * 不强制用户先建集——原创剧本在提交时由前端透明建好目标集再导入，用户全程无感。
 */
export function WelcomeCanvas({
  projectTitle,
  onOriginalScript,
  originalScriptBusy = false,
}: {
  projectTitle: string | undefined;
  /** 剧情演绎项目：从空白项目直接开启「AI 原创剧本」创建模式（提交时透明建集）。 */
  onOriginalScript?: () => void;
  /** 提交（建集）期间禁用按钮并显示加载态。 */
  originalScriptBusy?: boolean;
}) {
  const { t } = useTranslation("dashboard");

  return (
    <div className="mx-auto flex max-w-140 flex-col items-center gap-8 pt-20 text-center">
      <header className="flex flex-col items-center gap-2">
        <h1 className="display-serif text-2xl font-semibold tracking-tight break-words text-foreground">
          {t("welcome_title", { title: getProjectDisplayName(projectTitle, t("untitled_project")) })}
        </h1>
        <p className="text-sm leading-relaxed text-subtle-foreground">{t("welcome_desc")}</p>
      </header>

      {onOriginalScript && (
        <div className="flex flex-col items-center gap-3">
          <Button size="lg" onClick={onOriginalScript} disabled={originalScriptBusy}>
            {originalScriptBusy ? (
              <Loader2 aria-hidden data-icon="inline-start" className="animate-spin" />
            ) : (
              <Sparkles aria-hidden data-icon="inline-start" />
            )}
            {t("short_drama_generate_open")}
          </Button>
          <p className="max-w-md text-xs leading-relaxed text-muted-foreground">{t("welcome_original_hint")}</p>
        </div>
      )}
    </div>
  );
}
