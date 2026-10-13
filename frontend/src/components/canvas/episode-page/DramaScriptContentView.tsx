import { useCallback, useEffect, useRef, useState } from "react";
import { AlertTriangle, Loader2, RefreshCw, Save } from "lucide-react";
import { useTranslation } from "react-i18next";

import { reimportShortDramaSource, type ShortDramaSource } from "@/actions/shortDrama";
import { API } from "@/api";
import { refreshAfterWrite } from "@/components/canvas/shared/refreshAfterWrite";
import { useEditUnit } from "@/components/shared/edit-unit/useEditUnit";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogBody,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { Button } from "@/components/ui/button";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Textarea } from "@/components/ui/textarea";
import type { DramaEpisodeScript } from "@/types/script";

interface SourceValue {
  markdown: string;
  revision: string | null;
}

export function DramaScriptContentView({
  script,
  projectName,
  episode,
  readOnly,
  hasArtifacts,
}: {
  script: DramaEpisodeScript;
  projectName: string;
  episode: number;
  readOnly: boolean;
  hasArtifacts: boolean;
}) {
  const { t } = useTranslation("dashboard");
  const requestKey = `${projectName}:${episode}`;
  const [loaded, setLoaded] = useState<{
    key: string;
    source: ShortDramaSource | null;
    error: string | null;
  }>({ key: "", source: null, error: null });

  useEffect(() => {
    const controller = new AbortController();
    void API.getShortDramaSource(projectName, episode, { signal: controller.signal })
      .then((result) => {
        setLoaded({ key: requestKey, source: result, error: null });
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted) return;
        setLoaded({
          key: requestKey,
          source: null,
          error: error instanceof Error ? error.message : String(error),
        });
      });
    return () => controller.abort();
  }, [projectName, episode, requestKey]);

  if (loaded.key === requestKey && loaded.error) {
    return <p className="p-6 text-sm text-destructive">{loaded.error}</p>;
  }
  if (loaded.key !== requestKey || loaded.source === null) {
    return (
      <div className="flex min-h-48 items-center justify-center text-muted-foreground">
        <Loader2 className="size-5 animate-spin" aria-label={t("short_drama_source_loading")} />
      </div>
    );
  }
  const source = loaded.source;

  return (
    <ShortDramaSourceWorkspace
      key={`${projectName}:${episode}`}
      script={script}
      projectName={projectName}
      episode={episode}
      source={source}
      onSourceChange={(next) => setLoaded({ key: requestKey, source: next, error: null })}
      readOnly={readOnly}
      hasArtifacts={hasArtifacts}
    />
  );
}

function ShortDramaSourceWorkspace({
  script,
  projectName,
  episode,
  source,
  onSourceChange,
  readOnly,
  hasArtifacts,
}: {
  script: DramaEpisodeScript;
  projectName: string;
  episode: number;
  source: ShortDramaSource;
  onSourceChange: (source: ShortDramaSource) => void;
  readOnly: boolean;
  hasArtifacts: boolean;
}) {
  const { t } = useTranslation("dashboard");
  const revisionRef = useRef(source.revision);
  const [reimporting, setReimporting] = useState(false);
  const [confirmOpen, setConfirmOpen] = useState(false);

  const submit = useCallback(
    async (value: SourceValue, saved: SourceValue) => {
      if (value.markdown.trim() === "") throw new Error(t("short_drama_source_blank"));
      const updated = await API.updateShortDramaSource(
        projectName,
        episode,
        value.markdown,
        saved.revision,
      );
      revisionRef.current = updated.revision;
      onSourceChange(updated);
      return { markdown: updated.markdown, revision: updated.revision };
    },
    [projectName, episode, onSourceChange, t],
  );

  const unit = useEditUnit<SourceValue>({
    source: { markdown: source.markdown, revision: source.revision },
    save: submit,
    leaveTitle: t("short_drama_source_leave_title"),
  });

  const reimport = useCallback(
    async (confirmed: boolean) => {
      setReimporting(true);
      try {
        if (!(await unit.save())) return;
        const revision = revisionRef.current;
        if (!revision) throw new Error(t("short_drama_source_save_first"));
        await reimportShortDramaSource(projectName, episode, revision, confirmed);
        await refreshAfterWrite(projectName, t);
        setConfirmOpen(false);
      } finally {
        setReimporting(false);
      }
    },
    [unit, projectName, episode, t],
  );

  const requestReimport = () => {
    if (hasArtifacts) setConfirmOpen(true);
    else void reimport(false);
  };

  return (
    <>
      <div className="relative min-h-0 flex-1 overflow-y-auto">
        <div className="mx-auto w-full max-w-4xl px-6 py-6">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div className="min-w-0">
              <h2 className="truncate text-lg font-semibold">
                {script.title || t("drama_script_title_fallback")}
              </h2>
              <p className="mt-1 truncate font-mono text-xs text-muted-foreground">{source.path}</p>
            </div>
            {!readOnly ? (
              <div className="flex items-center gap-2">
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => void unit.save()}
                  disabled={!unit.dirty || unit.status === "saving" || reimporting}
                >
                  {unit.status === "saving" ? (
                    <Loader2 aria-hidden data-icon="inline-start" className="animate-spin" />
                  ) : (
                    <Save aria-hidden data-icon="inline-start" />
                  )}
                  {t("short_drama_source_save")}
                </Button>
                <Button
                  size="sm"
                  onClick={requestReimport}
                  disabled={reimporting || unit.status === "saving" || unit.value.markdown.trim() === ""}
                >
                  {reimporting ? (
                    <Loader2 aria-hidden data-icon="inline-start" className="animate-spin" />
                  ) : (
                    <RefreshCw aria-hidden data-icon="inline-start" />
                  )}
                  {t("short_drama_source_reimport")}
                </Button>
              </div>
            ) : null}
          </div>

          <Tabs defaultValue="source" className="mt-5">
            <TabsList aria-label={t("short_drama_source_tabs_aria")}>
              <TabsTrigger value="source">{t("short_drama_source_tab")}</TabsTrigger>
              <TabsTrigger value="parsed">{t("short_drama_parsed_tab")}</TabsTrigger>
            </TabsList>
            <TabsContent value="source">
              <div className="mt-3">
                {!source.exists ? (
                <div className="mb-3 flex items-start gap-2 rounded-md border border-warn/40 bg-warn/5 px-3 py-2 text-sm text-warn">
                  <AlertTriangle className="mt-0.5 size-4 shrink-0" aria-hidden />
                  <span>{t("short_drama_source_legacy")}</span>
                </div>
                ) : null}
                {hasArtifacts ? (
                <p className="mb-3 rounded-md border border-warn/40 bg-warn/5 px-3 py-2 text-sm text-warn">
                  {t("short_drama_source_artifacts_hint")}
                </p>
                ) : null}
                <Textarea
                  className="min-h-[56vh] max-h-none"
                  mono
                  value={unit.value.markdown}
                  onChange={(event) =>
                    unit.setValue((current) => ({ ...current, markdown: event.target.value }))
                  }
                  placeholder={t("short_drama_source_placeholder")}
                  readOnly={readOnly}
                  aria-label={t("short_drama_source_editor_aria")}
                />
                <div className="mt-2 min-h-5 text-sm">
                  {unit.error ? <span className="text-destructive">{unit.error}</span> : null}
                  {unit.status === "saved" ? (
                    <span className="text-good">{t("short_drama_source_saved")}</span>
                  ) : null}
                </div>
              </div>
            </TabsContent>
            <TabsContent value="parsed">
              <div className="mt-3">
                <ParsedScript script={script} />
              </div>
            </TabsContent>
          </Tabs>
        </div>
      </div>

      <AlertDialog
        open={confirmOpen}
        onOpenChange={(open) => {
          if (!open && !reimporting) setConfirmOpen(false);
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t("short_drama_reimport_confirm_title")}</AlertDialogTitle>
          </AlertDialogHeader>
          <AlertDialogBody>
            <AlertDialogDescription>
              {t("short_drama_reimport_confirm_description")}
            </AlertDialogDescription>
          </AlertDialogBody>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={reimporting}>{t("common:cancel")}</AlertDialogCancel>
            <AlertDialogAction
              variant="destructive"
              disabled={reimporting}
              onClick={() => void reimport(true)}
            >
              {reimporting ? (
                <Loader2 aria-hidden data-icon="inline-start" className="animate-spin" />
              ) : (
                <RefreshCw aria-hidden data-icon="inline-start" />
              )}
              {t("short_drama_reimport_confirm")}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  );
}

function ParsedScript({ script }: { script: DramaEpisodeScript }) {
  const { t } = useTranslation("dashboard");
  if (script.scenes.length === 0) {
    return <p className="text-sm text-muted-foreground">{t("drama_script_empty")}</p>;
  }
  return (
    <div className="space-y-4">
      {script.scenes.map((scene) => (
        <article key={scene.scene_id} className="rounded-md border border-border p-4">
          <header className="flex flex-wrap items-center gap-x-3 gap-y-1 text-sm text-muted-foreground">
            <span className="font-mono font-medium text-foreground">{scene.scene_id}</span>
            <span>{t("drama_script_scene_seconds", { seconds: scene.duration_seconds })}</span>
            {(scene.scenes ?? []).map((location) => <span key={location}>{location}</span>)}
            {(scene.characters_in_scene ?? []).length > 0 ? (
              <span>{scene.characters_in_scene.join("、")}</span>
            ) : null}
          </header>
          {scene.scene_description ? (
            <p className="mt-3 whitespace-pre-wrap text-sm leading-6">{scene.scene_description}</p>
          ) : null}
          {scene.utterances && scene.utterances.length > 0 ? (
            <ul className="mt-3 space-y-2 border-t border-border pt-3 text-sm">
              {scene.utterances.map((line, index) => (
                <li key={`${scene.scene_id}-${index}`} className="flex gap-2">
                  <span className="shrink-0 font-medium">
                    {line.kind === "dialogue" ? line.speaker : t("drama_script_voiceover")}
                  </span>
                  <span className="text-muted-foreground">{line.text}</span>
                </li>
              ))}
            </ul>
          ) : null}
        </article>
      ))}
    </div>
  );
}
