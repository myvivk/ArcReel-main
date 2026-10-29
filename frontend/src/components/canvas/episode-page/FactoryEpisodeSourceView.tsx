import { useCallback, useState } from "react";
import { Loader2, TriangleAlert } from "lucide-react";
import { useTranslation } from "react-i18next";

import type { SourceImportQualityWarning, SourceImportResult } from "@/api";
import { ShortDramaGenerateButton } from "@/components/canvas/shared/ShortDramaGenerateDialog";
import { refreshAfterWrite } from "@/components/canvas/shared/refreshAfterWrite";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import { voidCall } from "@/utils/async";
import { EpisodeHeaderActions } from "./EpisodeHeaderActions";
import { SourceImportPanel } from "./SourceImportPanel";
import type { useFactoryEpisodeSource } from "./useFactoryEpisodeSource";

/** 导入原文剧本页的两个视图：只读剧本 / 可编辑原文。 */
type SourceImportView = "script" | "original";

function QualityWarnings({ warnings }: { warnings: SourceImportQualityWarning[] }) {
  const { t } = useTranslation("dashboard");
  return (
    <Alert className="mb-4">
      <TriangleAlert aria-hidden />
      <AlertTitle>{t("quality_warnings_title")}</AlertTitle>
      <AlertDescription>
        <div className="flex flex-col gap-2">
          <p>{t("quality_warnings_hint")}</p>
          <Collapsible>
            <CollapsibleTrigger render={<Button variant="ghost" size="xs" className="-ml-2" />}>
              {t("quality_warnings_toggle", { issues: warnings.length })}
            </CollapsibleTrigger>
            <CollapsibleContent>
              <ul className="flex list-disc flex-col gap-1 pl-5">
                {warnings.map((warning, index) => (
                  <li key={`${warning.code}:${warning.line ?? 0}:${index}`}>
                    <span className="num text-subtle-foreground">
                      {warning.line == null ? warning.code : `${warning.code} L${warning.line}`}
                    </span>
                    <span className="ml-1.5">{warning.message}</span>
                  </li>
                ))}
              </ul>
            </CollapsibleContent>
          </Collapsible>
        </div>
      </AlertDescription>
    </Alert>
  );
}

/**
 * 集页「剧本」视图。AI 原创项目保持只读源稿 + 「AI 原创剧本」入口；
 * 导入原文项目提供「剧本 / 原文」切换：原文视图可编辑保存、可重新导入，
 * 无源稿时直接展示内联导入面板。机检告警只在本次生成后展示（服务端不落盘告警）。
 */
export function FactoryEpisodeSourceView({
  source,
  projectName,
  episode,
  sourceImport,
  hasMediaArtifacts,
}: {
  source: ReturnType<typeof useFactoryEpisodeSource>;
  projectName: string;
  episode: number;
  /** 导入原文项目：提供双视图与导入面板，不渲染 AI 原创剧本入口。 */
  sourceImport: boolean;
  /** 本集已有分镜图或视频：重新导入需二次确认。 */
  hasMediaArtifacts: boolean;
}) {
  const { t } = useTranslation("dashboard");
  // 由用户显式选择的视图；未选过时按「有源稿看剧本、没有看原文」取默认，
  // 不在源稿加载完成后回填 state（加载期间默认值会先落在原文上）。
  const [view, setView] = useState<SourceImportView | null>(null);
  const [warnings, setWarnings] = useState<SourceImportQualityWarning[]>([]);
  const [notices, setNotices] = useState<string[]>([]);
  const [stale, setStale] = useState(false);
  // 生成成功后让原文面板重读 revision/stale（面板在切到剧本视图时仍挂载，见下方 hidden）。
  const [originalToken, setOriginalToken] = useState(0);
  const onStaleChange = useCallback((next: boolean) => setStale(next), []);

  // 导入原文项目已有内容时不在刷新期间卸载视图：生成请求在途时项目事件也会触发刷新，
  // 卸载会连带取消生成（原文面板的卸载清理）。
  if (source.loading && (!sourceImport || !source.value)) {
    return <div className="flex min-h-48 items-center justify-center text-muted-foreground" aria-busy="true" aria-label={t("factory_source_loading")}><Loader2 aria-hidden className="size-5 animate-spin" /></div>;
  }
  if (source.error) {
    return (
      <section className="flex flex-col items-start gap-3 p-6">
        <Alert variant="destructive">
          <AlertTitle>{t("factory_source_load_failed")}</AlertTitle>
          <AlertDescription>{source.error}</AlertDescription>
        </Alert>
        <Button variant="outline" onClick={source.reload}>{t("common:retry")}</Button>
      </section>
    );
  }

  if (!sourceImport) {
    return (
      <>
        <EpisodeHeaderActions target="source">
          <ShortDramaGenerateButton projectName={projectName} episode={episode} />
        </EpisodeHeaderActions>
        {!source.exists || !source.value ? (
          <section className="flex min-h-48 flex-1 flex-col items-center justify-center gap-4 p-6">
            <p className="text-sm text-muted-foreground">{t("timeline_script_not_ready")}</p>
          </section>
        ) : (
          <article className="relative min-h-0 min-w-0 flex-1 overflow-y-auto p-6 [scrollbar-gutter:stable]">
            <p className="mb-4 break-all font-mono text-xs text-muted-foreground">{source.value.path}</p>
            <pre className="whitespace-pre-wrap break-words font-sans text-sm leading-7 text-foreground">{source.value.markdown}</pre>
          </article>
        )}
      </>
    );
  }

  const onImported = (result: SourceImportResult) => {
    setWarnings(result.quality_warnings ?? []);
    setNotices(result.warnings ?? []);
    setStale(false);
    setView("script");
    setOriginalToken((value) => value + 1);
    source.reload();
    voidCall(refreshAfterWrite(projectName, t));
  };

  const effectiveView: SourceImportView = view ?? (source.exists ? "script" : "original");
  const showScript = source.exists && effectiveView === "script" && Boolean(source.value);

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      {source.exists && (
        <div className="flex shrink-0 flex-wrap items-center gap-3 border-b border-border px-6 py-2">
          <ToggleGroup
            aria-label={t("source_import_view_aria")}
            size="sm"
            value={[effectiveView]}
            onValueChange={(next: string[]) => {
              const picked = next.find((value): value is SourceImportView => value === "script" || value === "original");
              if (picked) setView(picked);
            }}
          >
            <ToggleGroupItem value="original">{t("source_import_view_original")}</ToggleGroupItem>
            <ToggleGroupItem value="script">{t("source_import_view_script")}</ToggleGroupItem>
          </ToggleGroup>
          {stale && <p className="text-xs text-warn">{t("source_import_stale_notice")}</p>}
        </div>
      )}
      {showScript && source.value && (
        <article className="relative min-h-0 min-w-0 flex-1 overflow-y-auto p-6 [scrollbar-gutter:stable]">
          <p className="mb-4 break-all font-mono text-xs text-muted-foreground">{source.value.path}</p>
          {notices.length > 0 && (
            <ul className="mb-4 flex list-disc flex-col gap-1 pl-5 text-xs text-muted-foreground">
              {notices.map((notice) => <li key={notice}>{notice}</li>)}
            </ul>
          )}
          {warnings.length > 0 && <QualityWarnings warnings={warnings} />}
          <pre className="whitespace-pre-wrap break-words font-sans text-sm leading-7 text-foreground">{source.value.markdown}</pre>
        </article>
      )}
      {/* 原文面板常挂载：切到剧本视图只隐藏，未保存的原文草稿不会因切换视图丢失；
          生成成功后由 reloadToken 重读服务端原文与 stale 状态。 */}
      <div className={showScript ? "hidden" : "relative flex min-h-0 flex-1 flex-col overflow-y-auto p-6 [scrollbar-gutter:stable]"}>
        <SourceImportPanel
          projectName={projectName}
          episode={episode}
          hasScript={source.exists}
          sourceRevision={source.value?.revision ?? null}
          hasMediaArtifacts={hasMediaArtifacts}
          reloadToken={originalToken}
          onImported={onImported}
          onShowOriginal={() => setView("original")}
          onStaleChange={onStaleChange}
        />
      </div>
    </div>
  );
}
