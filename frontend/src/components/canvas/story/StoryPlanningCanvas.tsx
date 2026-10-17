import { useEffect, useState } from "react";
import { BookOpen, Loader2 } from "lucide-react";
import { API } from "@/api";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { errMsg } from "@/utils/async";
import type { ShortDramaArtifactView, StoryPlanningArtifacts } from "@/types/story-planning";

const emptyArtifacts: StoryPlanningArtifacts = {
  character_bible: null,
  series_outline: null,
  ledger: null,
  episode_plans: [],
};

export function StoryPlanningCanvas({ projectName }: { projectName: string }) {
  const [artifacts, setArtifacts] = useState<StoryPlanningArtifacts | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    setArtifacts(null);
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
  }, [projectName, attempt]);

  if (error && !artifacts) {
    return (
      <div className="flex min-h-0 flex-1 flex-col gap-4 p-6">
        <Alert variant="destructive">
          <AlertTitle>故事规划加载失败</AlertTitle>
          <AlertDescription>{error}</AlertDescription>
        </Alert>
        <Button variant="outline" onClick={() => setAttempt((value) => value + 1)}>重试</Button>
      </div>
    );
  }
  if (!artifacts) return <div className="flex min-h-0 flex-1 items-center justify-center" aria-busy="true"><Loader2 aria-hidden className="size-5 animate-spin" /></div>;
  return <ArtifactReader artifacts={{ ...emptyArtifacts, ...artifacts }} />;
}

function ArtifactReader({ artifacts }: { artifacts: StoryPlanningArtifacts }) {
  const firstPlan = artifacts.episode_plans[0];
  return (
    <div className="flex min-h-0 min-w-0 flex-1 flex-col">
      <header className="flex shrink-0 items-start gap-4 border-b border-border px-6 py-5">
        <BookOpen aria-hidden className="mt-1 size-6 text-primary" />
        <div className="min-w-0 flex-1">
          <h1 className="text-xl font-semibold">故事规划</h1>
          <p className="mt-1 text-sm text-muted-foreground">短剧工厂原文阅读器</p>
        </div>
      </header>
      <div className="min-h-0 flex-1 overflow-y-auto p-6 [scrollbar-gutter:stable]">
        <Tabs defaultValue="bible">
          <TabsList variant="line" aria-label="故事规划内容">
            <TabsTrigger value="bible">人物圣经</TabsTrigger>
            <TabsTrigger value="outline">全剧大纲</TabsTrigger>
            <TabsTrigger value="ledger">连续性台账</TabsTrigger>
            <TabsTrigger value="plans">分集规划</TabsTrigger>
          </TabsList>
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
  if (!artifact) return <p className="py-8 text-sm text-muted-foreground">暂无工厂原文</p>;
  return (
    <article className="mt-5 min-h-0 rounded-lg border border-border bg-card p-5">
      <p className="mb-4 font-mono text-xs text-muted-foreground">{artifact.path}</p>
      <pre className="whitespace-pre-wrap break-words font-sans text-sm leading-7 text-foreground">{artifact.content}</pre>
    </article>
  );
}

function EpisodePlans({ plans, initial }: { plans: ShortDramaArtifactView[]; initial?: ShortDramaArtifactView }) {
  const [selected, setSelected] = useState(initial?.path ?? "");
  const current = plans.find((plan) => plan.path === selected) ?? initial;
  return (
    <div className="mt-5 grid min-h-0 grid-cols-[minmax(9rem,24%)_minmax(0,1fr)] gap-4">
      <div className="min-h-0 overflow-y-auto rounded-lg border border-border p-2">
        {plans.length ? plans.map((plan) => (
          <button key={plan.path} type="button" className={`focus-ring mb-1 w-full rounded-md px-3 py-2 text-left text-sm ${plan.path === current?.path ? "bg-primary/10 text-primary" : "hover:bg-muted"}`} onClick={() => setSelected(plan.path)}>
            第 {plan.episode} 集
          </button>
        )) : <p className="p-3 text-sm text-muted-foreground">暂无分集规划</p>}
      </div>
      <MarkdownPanel artifact={current ?? null} />
    </div>
  );
}
