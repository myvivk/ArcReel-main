import { useEffect, useState } from "react";
import { Loader2 } from "lucide-react";
import { useTranslation } from "react-i18next";
import { API } from "@/api";
import { ShortDramaImportButton } from "@/components/canvas/shared/ShortDramaImportDialog";
import { errMsg } from "@/utils/async";

interface SourceResult {
  episode: number;
  path: string;
  markdown: string;
  revision: string | null;
  exists: boolean;
}

export function FactoryEpisodeSourceView({ projectName, episode }: { projectName: string; episode: number }) {
  const { t } = useTranslation("dashboard");
  const [result, setResult] = useState<{ key: string; value: SourceResult | null; error: string | null }>({
    key: "",
    value: null,
    error: null,
  });
  const requestKey = `${projectName}:${episode}`;

  useEffect(() => {
    const controller = new AbortController();
    API.getShortDramaSource(projectName, episode, { signal: controller.signal })
      .then((value) => {
        if (!controller.signal.aborted) setResult({ key: requestKey, value, error: null });
      })
      .catch((cause: unknown) => {
        if (!controller.signal.aborted) setResult({ key: requestKey, value: null, error: errMsg(cause) });
      });
    return () => controller.abort();
  }, [episode, projectName, requestKey]);

  if (result.key !== requestKey) {
    return <div className="flex min-h-48 items-center justify-center text-muted-foreground" aria-busy="true"><Loader2 className="size-5 animate-spin" /></div>;
  }
  if (result.error) {
    return (
      <section className="flex min-h-64 flex-col items-center justify-center gap-3 p-6 text-center">
        <p className="text-sm text-destructive">{result.error}</p>
        <ShortDramaImportButton projectName={projectName} episode={episode} prominent />
      </section>
    );
  }
  if (!result.value?.exists) {
    return (
      <section className="flex min-h-64 flex-col items-center justify-center gap-3 p-6 text-center">
        <p className="text-sm text-muted-foreground">{t("episode_workspace_source_missing")}</p>
        <ShortDramaImportButton projectName={projectName} episode={episode} prominent />
      </section>
    );
  }
  return (
    <article className="relative min-h-0 overflow-y-auto p-6 [scrollbar-gutter:stable]">
      <p className="mb-4 font-mono text-xs text-muted-foreground">{result.value.path}</p>
      <pre className="whitespace-pre-wrap break-words font-sans text-sm leading-7 text-foreground">{result.value.markdown}</pre>
    </article>
  );
}
