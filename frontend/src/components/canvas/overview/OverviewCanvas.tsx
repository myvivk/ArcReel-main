import { useEffect } from "react";
import { Link } from "wouter";
import { useTranslation } from "react-i18next";
import { ShortDramaGenerateButton, ShortDramaGenerateHost } from "@/components/canvas/shared/ShortDramaGenerateDialog";
import { Button } from "@/components/ui/button";
import { SourceImportEntry } from "@/components/canvas/episode-page/SourceImportEntry";
import { useCostStore } from "@/stores/cost-store";
import { useShortDramaGenerateStore } from "@/stores/short-drama-generate-store";
import { ONBOARDING_ANCHORS } from "@/onboarding/anchors";
import type { ProjectData } from "@/types";
import { isSourceImport } from "@/utils/creation-mode";
import { AssetProgressLine } from "./AssetProgressLine";
import { CostLine } from "./CostLine";
import { OverviewHeader } from "./OverviewHeader";
import { WelcomeCanvas } from "./WelcomeCanvas";

/** 工厂项目概览：项目管理、制作进度和工厂内容入口。 */
export function OverviewCanvas({
  projectName, projectData, readOnly = false,
}: { projectName: string; projectData: ProjectData | null; readOnly?: boolean }) {
  const { t } = useTranslation("dashboard");
  const debouncedFetch = useCostStore((s) => s.debouncedFetch);
  useEffect(() => {
    debouncedFetch(projectName);
  }, [projectName, projectData?.episodes, debouncedFetch]);

  if (!projectData) return <div className="relative flex min-h-0 flex-1" aria-busy="true" />;
  const emptyProject = (projectData.episodes?.length ?? 0) === 0;
  // 导入原文项目没有故事规划，也没有 AI 原创剧本：概览只保留一个「导入原文」入口。
  const sourceImport = isSourceImport(projectData);
  const firstEpisode = projectData.episodes?.[0]?.episode ?? null;
  return (
    <div data-onboarding={ONBOARDING_ANCHORS.workbenchOverview} className="relative flex min-h-0 flex-1 flex-col overflow-y-auto p-6 [scrollbar-gutter:stable] @3xl/canvas:p-8">
      {emptyProject && !readOnly ? (
        <WelcomeCanvas
          projectTitle={projectData.title}
          onOriginalScript={sourceImport ? undefined : () => useShortDramaGenerateStore.getState().open({ projectName, episode: 0 })}
          importSourceAction={sourceImport ? <SourceImportEntry projectName={projectName} episode={null} prominent /> : undefined}
        />
      ) : (
        <div className="flex w-full shrink-0 flex-col gap-6">
          <div className="flex flex-col gap-3">
            <OverviewHeader projectName={projectName} data={projectData} readOnly={readOnly} />
            <div className="flex flex-col gap-1.5">
              <AssetProgressLine data={projectData} />
              <CostLine projectName={projectName} readOnly={readOnly} />
            </div>
          </div>
          {!readOnly && (
            <div className="flex flex-wrap gap-2">
              {sourceImport ? (
                <SourceImportEntry projectName={projectName} episode={firstEpisode} prominent />
              ) : (
                <>
                  <ShortDramaGenerateButton projectName={projectName} episode={0} />
                  <Button variant="outline" nativeButton={false} render={<Link href="/story" />}>{t("story_title")}</Button>
                </>
              )}
            </div>
          )}
        </div>
      )}
      {!readOnly && <ShortDramaGenerateHost projectName={projectName} episode={0} />}
    </div>
  );
}
