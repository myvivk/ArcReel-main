import { useCallback, useEffect, useRef, useState } from "react";
import { Link } from "wouter";
import { useTranslation } from "react-i18next";
import { BookOpen, Download, Loader2, Plus, Check, Trash2 } from "lucide-react";
import { API } from "@/api";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Textarea } from "@/components/ui/textarea";
import { Input } from "@/components/ui/input";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Empty, EmptyHeader, EmptyTitle, EmptyDescription } from "@/components/ui/empty";
import { useEditUnit } from "@/components/shared/edit-unit/useEditUnit";
import { UnsavedChangesBar } from "@/components/shared/edit-unit/UnsavedChangesBar";
import { useProjectsStore } from "@/stores/projects-store";
import { useAppStore } from "@/stores/app-store";
import { errMsg } from "@/utils/async";
import type { StoryRecord, StorySnapshot } from "@/types/story-planning";

const categories = ["relationship", "event", "fact"] as const;

export function StoryPlanningCanvas({ projectName }: { projectName: string }) {
  const { t } = useTranslation("dashboard");
  const [source, setSource] = useState<StorySnapshot | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);
  const controller = useRef<AbortController | null>(null);
  const projectData = useProjectsStore((s) => s.currentProjectData);
  useEffect(() => {
    const abort = new AbortController();
    controller.current = abort;
    API.getStoryPlanning(projectName, { signal: abort.signal }).then((result) => {
      if (!abort.signal.aborted) { setSource(result); setError(null); }
    }).catch((err: unknown) => {
      if (!abort.signal.aborted) setError(errMsg(err));
    });
    return () => abort.abort();
  }, [projectName, projectData, attempt]);
  if (error && !source) return (
    <div className="flex min-h-0 flex-1 flex-col gap-4 p-6">
      <Alert variant="destructive"><AlertTitle>{t("story_load_failed")}</AlertTitle><AlertDescription>{error}</AlertDescription></Alert>
      <Button variant="outline" onClick={() => setAttempt((n) => n + 1)}>{t("common:retry")}</Button>
    </div>
  );
  if (!source) return <div className="flex min-h-0 flex-1 items-center justify-center" aria-busy="true"><Loader2 aria-hidden className="size-5 animate-spin" /></div>;
  return <StoryEditor projectName={projectName} source={source} onSource={setSource} />;
}

function StoryEditor({ projectName, source, onSource }: {
  projectName: string; source: StorySnapshot; onSource: (next: StorySnapshot) => void;
}) {
  const { t } = useTranslation("dashboard");
  const project = useProjectsStore((s) => s.currentProjectData);
  const [extracting, setExtracting] = useState(false);
  const lifetime = useRef(new AbortController());
  useEffect(() => {
    lifetime.current = new AbortController();
    return () => lifetime.current.abort();
  }, []);
  const save = useCallback(async (value: StorySnapshot, saved: StorySnapshot) => {
    const result = await API.saveStoryPlanning(projectName, value.plan, saved.revision, { signal: lifetime.current.signal });
    lifetime.current.signal.throwIfAborted();
    onSource(result);
    return result;
  }, [projectName, onSource]);
  const unit = useEditUnit({ source, save });
  const records = unit.value.plan.records;
  const stale = new Set(source.stale_ids);
  const issues = records.filter((r) => r.status === "pending" || stale.has(r.id));
  const busy = extracting || unit.status === "saving";
  const patch = (id: string, change: Partial<StoryRecord>) => unit.setValue((value) => ({
    ...value, plan: { ...value.plan, records: value.plan.records.map((r) => r.id === id ? { ...r, ...change } : r) },
  }));
  const add = (kind: StoryRecord["kind"]) => unit.setValue((value) => ({
    ...value, plan: { ...value.plan, records: [...value.plan.records, {
      id: crypto.randomUUID(), kind, text: "", characters: [], status: "pending",
      episode: kind === "relationship" ? null : project?.episodes?.[0]?.episode ?? null,
      source_file: null, source_id: null, source_revision: null,
    }] },
  }));
  const extract = async () => {
    if (busy || unit.dirty) return;
    setExtracting(true);
    try {
      const result = await API.extractStoryPlanning(projectName, unit.savedValue.revision, { signal: lifetime.current.signal });
      lifetime.current.signal.throwIfAborted();
      onSource(result);
    } catch (err) {
      if (!lifetime.current.signal.aborted) useAppStore.getState().pushToast(errMsg(err), "error");
    } finally {
      if (!lifetime.current.signal.aborted) setExtracting(false);
    }
  };
  const renderRecords = (items: StoryRecord[]) => items.length ? (
    <div className="flex flex-col gap-5">
      {items.map((record) => (
        <article key={record.id} className="min-w-0 rounded-lg border border-border bg-card p-4">
          <div className="mb-4 flex flex-wrap items-center gap-2">
            <Badge variant="outline">{t(`story_${record.kind}`)}</Badge>
            <Badge variant={record.status === "confirmed" && !stale.has(record.id) ? "secondary" : "outline"}>
              {t(stale.has(record.id) ? "story_stale" : record.status === "confirmed" ? "story_confirmed" : "story_pending")}
            </Badge>
            {record.source_file ? <Link className="text-sm text-primary underline" href={`/episodes/${record.episode}`}>{t("story_source")}</Link> : null}
            <div className="flex-1" />
            <Button variant="ghost" size="sm" disabled={busy || stale.has(record.id) || !record.text.trim()}
              onClick={() => patch(record.id, { status: record.status === "confirmed" ? "pending" : "confirmed" })}>
              <Check aria-hidden data-icon="inline-start" />{t(record.status === "confirmed" ? "story_unconfirm" : "story_confirm")}
            </Button>
            <Button variant="ghost" size="icon-sm" disabled={busy} aria-label={t("story_remove")}
              onClick={() => unit.setValue((value) => ({ ...value, plan: { ...value.plan, records: value.plan.records.filter((r) => r.id !== record.id) } }))}>
              <Trash2 aria-hidden />
            </Button>
          </div>
          <label className="mb-2 block text-sm text-subtle-foreground" htmlFor={`text-${record.id}`}>{t("story_text")}</label>
          <Textarea id={`text-${record.id}`} variant="plain" className="max-h-none" value={record.text} disabled={busy || !!record.source_file}
            onChange={(e) => patch(record.id, { text: e.target.value })} />
          <div className="mt-4 flex flex-wrap items-center gap-4">
            <label className="flex items-center gap-2 text-sm text-subtle-foreground">
              {t("story_episode")}
              <select className="focus-ring max-w-48 rounded-md border border-input bg-background p-2 text-foreground" value={record.episode ?? ""}
                disabled={busy || !!record.source_file} onChange={(e) => patch(record.id, { episode: e.target.value ? Number(e.target.value) : null })}>
                {record.kind === "relationship" ? <option value="">{t("story_global")}</option> : null}
                {project?.episodes?.map((ep, i) => <option value={ep.episode} key={ep.episode}>{i + 1}. {ep.title}</option>)}
              </select>
            </label>
            <fieldset className="flex min-w-0 flex-wrap gap-3">
              <legend className="mb-2 text-sm text-subtle-foreground">{t("story_characters")}</legend>
              {Object.keys(project?.characters ?? {}).map((name) => (
                <label key={name} className="flex items-center gap-1.5 break-all text-sm">
                  <input type="checkbox" checked={record.characters.includes(name)} disabled={busy || !!record.source_file}
                    onChange={(e) => patch(record.id, { characters: e.target.checked ? [...record.characters, name] : record.characters.filter((c) => c !== name) })} />
                  {name}
                </label>
              ))}
            </fieldset>
          </div>
        </article>
      ))}
    </div>
  ) : <Empty><EmptyHeader><EmptyTitle>{t("story_empty")}</EmptyTitle><EmptyDescription>{t("story_empty_hint")}</EmptyDescription></EmptyHeader></Empty>;
  return (
    <div className="flex min-h-0 min-w-0 flex-1 flex-col">
      <header className="flex shrink-0 flex-wrap items-start gap-4 border-b border-border px-6 py-5">
        <BookOpen aria-hidden className="mt-1 size-6 text-primary" />
        <div className="min-w-0 flex-1"><h1 className="text-xl font-semibold">{t("story_title")}</h1><p className="mt-1 text-sm text-muted-foreground">{t("story_description")}</p></div>
        <Button variant="outline" disabled={busy || unit.dirty} onClick={() => void extract()}>
          {extracting ? <Loader2 aria-hidden data-icon="inline-start" className="animate-spin" /> : <Download aria-hidden data-icon="inline-start" />}{t("story_extract")}
        </Button>
      </header>
      <div className="relative min-h-0 flex-1 overflow-y-auto p-6 [scrollbar-gutter:stable]">
        <p className="mb-4 text-sm text-muted-foreground">{t("story_count", { confirmed: records.filter((r) => r.status === "confirmed" && !stale.has(r.id)).length, total: records.length })}</p>
        {unit.dirty ? <p className="mb-3 text-sm text-warn">{t("story_save_first")}</p> : null}
        <Tabs defaultValue="outline">
          <div className="relative overflow-x-auto scroll-fade-x">
            <TabsList variant="line" aria-label={t("story_title")}>
              <TabsTrigger value="outline">{t("story_outline")}</TabsTrigger>
              {categories.map((kind) => <TabsTrigger key={kind} value={kind}>{t(`story_${kind}`)}</TabsTrigger>)}
              <TabsTrigger value="issues">{t("story_issues")} ({issues.length})</TabsTrigger>
            </TabsList>
          </div>
          <TabsContent value="outline">
            <div className="flex flex-col gap-5 py-5">
              <label htmlFor="story-premise" className="text-base font-medium">{t("story_premise")}</label>
              <Textarea id="story-premise" variant="plain" className="max-h-none" value={unit.value.plan.premise} disabled={busy}
                placeholder={t("story_premise_hint")} onChange={(e) => unit.setValue((value) => ({ ...value, plan: { ...value.plan, premise: e.target.value } }))} />
              <Alert role="note"><AlertTitle>{t("story_authoring")}</AlertTitle><AlertDescription>{t("story_scope_hint")}</AlertDescription></Alert>
              <label htmlFor="story-budget" className="text-sm font-medium">{t("story_budget")}</label>
              <Input id="story-budget" type="number" min={400} max={6000} value={unit.value.plan.context_budget} disabled={busy}
                onChange={(e) => unit.setValue((value) => ({ ...value, plan: { ...value.plan, context_budget: Number(e.target.value) } }))} />
              <p className="text-sm text-muted-foreground">{t("story_budget_hint")}</p>
              <p className="text-sm text-muted-foreground">{t("story_extract_hint")}</p>
            </div>
          </TabsContent>
          {categories.map((kind) => <TabsContent key={kind} value={kind}>
            <div className="flex flex-col gap-4 py-5">
              <div><Button variant="outline" size="sm" disabled={busy || (kind !== "relationship" && !project?.episodes?.length)} onClick={() => add(kind)}><Plus aria-hidden data-icon="inline-start" />{t("story_add")}</Button></div>
              {renderRecords(records.filter((r) => r.kind === kind))}
            </div>
          </TabsContent>)}
          <TabsContent value="issues"><div className="flex flex-col gap-4 py-5"><p className="text-sm text-muted-foreground">{t("story_issue_hint")}</p>{issues.length ? renderRecords(issues) : <p>{t("story_all_clear")}</p>}</div></TabsContent>
        </Tabs>
      </div>
      <div className="shrink-0 px-6 pb-4"><UnsavedChangesBar unit={unit} /></div>
    </div>
  );
}
