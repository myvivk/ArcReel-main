import { useEffect, useState } from "react";
import { BookOpen, Loader2 } from "lucide-react";
import { useTranslation } from "react-i18next";
import { API } from "@/api";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { errMsg } from "@/utils/async";
import { useProjectsStore } from "@/stores/projects-store";
import type { ShortDramaArtifactView, StoryPlanningArtifacts } from "@/types/story-planning";

const emptyArtifacts: StoryPlanningArtifacts = {
  character_bible: null,
  series_outline: null,
  ledger: null,
  episode_plans: [],
};

export function StoryPlanningCanvas({ projectName }: { projectName: string }) {
  const { t } = useTranslation("dashboard");
  const revision = useProjectsStore((s) => s.projectSnapshotRevisions[projectName] ?? 0);
  const [artifacts, setArtifacts] = useState<StoryPlanningArtifacts | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);

  // 换项目或重试时先清空：在渲染期对齐请求键，避免在 effect 里同步 setState 触发级联渲染。
  const requestKey = `${projectName}#${attempt}`;
  const [syncedKey, setSyncedKey] = useState(requestKey);
  if (syncedKey !== requestKey) {
    setSyncedKey(requestKey);
    setArtifacts(null);
    setError(null);
  }

  useEffect(() => {
    const controller = new AbortController();
    API.getStoryPlanning(projectName, { signal: controller.signal })
      .then((result) => {
        if (!controller.signal.aborted) {
          setArtifacts(result);
          setError(null);
        }
      })
      .catch((cause: unknown) => {
        if (!controller.signal.aborted) setError(errMsg(cause));
      });
    return () => controller.abort();
  }, [projectName, attempt, revision]);

  if (error && !artifacts) {
    return (
      <div className="flex min-h-0 flex-1 flex-col gap-4 p-6">
        <Alert variant="destructive">
          <AlertTitle>{t("factory_story_load_failed")}</AlertTitle>
          <AlertDescription>{error}</AlertDescription>
        </Alert>
        <Button variant="outline" onClick={() => setAttempt((value) => value + 1)}>{t("common:retry")}</Button>
      </div>
    );
  }
  if (!artifacts) return <div className="flex min-h-0 flex-1 items-center justify-center" aria-busy="true"><Loader2 aria-hidden className="size-5 animate-spin" /></div>;
  return (
    <>
      {error && (
        <div className="p-4">
          <Alert variant="destructive">
            <AlertTitle>{t("factory_story_load_failed")}</AlertTitle>
            <AlertDescription>{error}</AlertDescription>
          </Alert>
          <Button variant="outline" onClick={() => setAttempt((value) => value + 1)}>{t("common:retry")}</Button>
        </div>
      )}
      <ArtifactReader artifacts={{ ...emptyArtifacts, ...artifacts }} />
    </>
  );
}

function ArtifactReader({ artifacts }: { artifacts: StoryPlanningArtifacts }) {
  const { t } = useTranslation("dashboard");
  const firstPlan = artifacts.episode_plans[0];
  return (
    <div className="flex min-h-0 min-w-0 flex-1 flex-col">
      <header className="flex shrink-0 items-start gap-4 border-b border-border px-6 py-5">
        <BookOpen aria-hidden className="mt-1 size-6 text-primary" />
        <div className="min-w-0 flex-1">
          <h1 className="text-xl font-semibold">{t("factory_story_title")}</h1>
          <p className="mt-1 text-sm text-muted-foreground">{t("factory_story_description")}</p>
        </div>
      </header>
      <div className="relative min-h-0 flex-1 overflow-y-auto p-6 [scrollbar-gutter:stable]">
        <Tabs defaultValue="bible">
          <div className="relative overflow-x-auto scroll-fade-x">
            <TabsList variant="line" aria-label={t("factory_story_title")}>
              <TabsTrigger value="bible">{t("factory_story_bible")}</TabsTrigger>
              <TabsTrigger value="outline">{t("factory_story_outline")}</TabsTrigger>
              <TabsTrigger value="ledger">{t("factory_story_ledger")}</TabsTrigger>
              <TabsTrigger value="plans">{t("factory_story_plans")}</TabsTrigger>
            </TabsList>
          </div>
          <TabsContent value="bible"><MarkdownPanel artifact={artifacts.character_bible} /></TabsContent>
          <TabsContent value="outline"><MarkdownPanel artifact={artifacts.series_outline} /></TabsContent>
          <TabsContent value="ledger"><MarkdownPanel artifact={artifacts.ledger} /></TabsContent>
          <TabsContent value="plans"><EpisodePlans plans={artifacts.episode_plans} initial={firstPlan} /></TabsContent>
        </Tabs>
      </div>
    </div>
  );
}

function MarkdownPanel({ artifact }: { artifact: ShortDramaArtifactView | null }) {
  const { t } = useTranslation("dashboard");
  if (!artifact) return <p className="py-8 text-sm text-muted-foreground">{t("factory_story_empty")}</p>;
  return (
    <article className="mt-5 min-h-0 min-w-0 rounded-lg border border-border bg-card p-5">
      <p className="mb-4 break-all font-mono text-xs text-muted-foreground">{artifact.path}</p>
      <pre className="whitespace-pre-wrap break-words font-sans text-sm leading-7 text-foreground">{artifact.content}</pre>
    </article>
  );
}

function EpisodePlans({ plans, initial }: { plans: ShortDramaArtifactView[]; initial?: ShortDramaArtifactView }) {
  const { t } = useTranslation("dashboard");
  const [selected, setSelected] = useState(initial?.path ?? "");
  const current = plans.find((plan) => plan.path === selected) ?? initial;
  return (
    <div className="mt-5 grid min-h-0 grid-cols-[minmax(9rem,24%)_minmax(0,1fr)] gap-4">
      <div className="relative min-h-0 overflow-y-auto rounded-lg border border-border p-2">
        {plans.length ? plans.map((plan) => (
          <button key={plan.path} type="button" className={`focus-ring mb-1 w-full rounded-md px-3 py-2 text-left text-sm ${plan.path === current?.path ? "bg-primary/10 text-primary" : "hover:bg-muted"}`} onClick={() => setSelected(plan.path)}>
            {t("factory_story_episode", { episode: plan.episode })}
          </button>
        )) : <p className="p-3 text-sm text-muted-foreground">{t("factory_story_plans_empty")}</p>}
      </div>
      <MarkdownPanel artifact={current ?? null} />
    </div>
  );
}
