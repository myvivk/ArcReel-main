import { Loader2 } from "lucide-react";
import { useTranslation } from "react-i18next";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { ShortDramaGenerateButton } from "@/components/canvas/shared/ShortDramaGenerateDialog";
import { EpisodeHeaderActions } from "./EpisodeHeaderActions";
import type { useFactoryEpisodeSource } from "./useFactoryEpisodeSource";

export function FactoryEpisodeSourceView({
  source, projectName, episode,
}: { source: ReturnType<typeof useFactoryEpisodeSource>; projectName: string; episode: number }) {
  const { t } = useTranslation("dashboard");
  if (source.loading) {
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
  const actions = <ShortDramaGenerateButton projectName={projectName} episode={episode} />;
  return (
    <>
      <EpisodeHeaderActions>{actions}</EpisodeHeaderActions>
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
