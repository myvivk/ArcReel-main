import { useEffect, useId, useRef, useState, type ChangeEvent } from "react";
import { useTranslation } from "react-i18next";
import { FolderOpen, Loader2 } from "lucide-react";
import { importShortDramaArtifacts } from "@/actions/shortDrama";
import { Button } from "@/components/ui/button";
import {
  Dialog, DialogBody, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle,
} from "@/components/ui/dialog";
import { Label } from "@/components/ui/label";
import { refreshAfterWrite } from "@/components/canvas/shared/refreshAfterWrite";
import { useShortDramaImportStore, type ShortDramaImportOpenRequest } from "@/stores/short-drama-import-store";

const ARTIFACT_PATH_RE = /^(?:character-bible\.md|series-outline\.md|ledger\.md|episode-plan\/episode_\d+\.plan\.md|episodes\/episode_\d+\.md|reports\/episode_\d+\.machine\.md|compliance\/episode_\d+\.compliance\.md)$/;

function normalizeArtifactPath(path: string): string {
  const parts = path.replaceAll("\\", "/").split("/");
  const start = parts.findIndex((part) => [
    "character-bible.md", "series-outline.md", "ledger.md", "episode-plan", "episodes", "reports", "compliance",
  ].includes(part));
  return start < 0 ? parts.join("/") : parts.slice(start).join("/");
}

function encodeBase64(bytes: Uint8Array): string {
  let binary = "";
  for (let offset = 0; offset < bytes.length; offset += 0x8000) {
    binary += String.fromCharCode(...bytes.subarray(offset, offset + 0x8000));
  }
  return btoa(binary);
}

/** 工件导入可从概览或集页打开；切换项目、剧集时关闭。 */
export function ShortDramaImportHost({ projectName, episode }: { projectName: string; episode?: number }) {
  const request = useShortDramaImportStore((s) => s.request);
  const close = useShortDramaImportStore((s) => s.close);
  useEffect(() => close, [close, projectName, episode]);
  if (!request || request.projectName !== projectName) return null;
  return <ShortDramaImportDialog request={request} onClose={close} />;
}

/** 七类工件按原始字节入库；校验和集账本登记由服务端事务完成。 */
function ShortDramaImportDialog({
  request, onClose,
}: { request: ShortDramaImportOpenRequest; onClose: () => void }) {
  const { t } = useTranslation("dashboard");
  const fieldId = useId();
  const [artifactFiles, setArtifactFiles] = useState<Array<{ path: string; content_base64: string }>>([]);
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const fileGeneration = useRef(0);
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      fileGeneration.current += 1;
    };
  }, []);

  const readArtifactFiles = async (event: ChangeEvent<HTMLInputElement>) => {
    const generation = ++fileGeneration.current;
    const files = Array.from(event.target.files ?? []);
    setArtifactFiles([]);
    setError(null);
    try {
      const loaded = await Promise.all(files.map(async (file) => ({
        path: normalizeArtifactPath(file.webkitRelativePath || file.name),
        content_base64: encodeBase64(new Uint8Array(await file.arrayBuffer())),
      })));
      if (!mounted.current || fileGeneration.current !== generation) return;
      const invalid = loaded.filter((item) => !ARTIFACT_PATH_RE.test(item.path));
      const accepted = loaded.filter((item) => ARTIFACT_PATH_RE.test(item.path));
      const duplicates = accepted.filter((item, index) =>
        accepted.findIndex((other) => other.path === item.path) !== index,
      );
      setArtifactFiles(accepted);
      setError(invalid.length || duplicates.length
        ? t("short_drama_import_artifacts_invalid", { invalid: invalid.length, duplicates: duplicates.length })
        : null);
    } catch {
      if (mounted.current && fileGeneration.current === generation) {
        setError(t("short_drama_import_artifacts_read_failed"));
      }
    }
  };

  const submit = async () => {
    if (submitting || !artifactFiles.length || error) return;
    setSubmitting(true);
    try {
      await importShortDramaArtifacts(request.projectName, artifactFiles);
      await refreshAfterWrite(request.projectName, t);
      if (mounted.current) onClose();
    } catch {
      // 动作层已显示错误；保留文件清单供重试。
    } finally {
      if (mounted.current) setSubmitting(false);
    }
  };

  return (
    <Dialog open onOpenChange={(open) => { if (!open && !submitting) onClose(); }}>
      <DialogContent showCloseButton={!submitting}>
        <DialogHeader>
          <DialogTitle>{t("short_drama_import_mode_artifacts")}</DialogTitle>
          <DialogDescription>{t("short_drama_import_artifacts_hint")}</DialogDescription>
        </DialogHeader>
        <DialogBody>
          <div className="flex flex-col gap-3">
            <Label htmlFor={`${fieldId}-artifacts`}>{t("short_drama_import_mode_artifacts")}</Label>
            <input
              id={`${fieldId}-artifacts`}
              type="file"
              multiple
              {...{ webkitdirectory: "" }}
              accept=".md,text/markdown"
              disabled={submitting}
              onChange={(event) => { void readArtifactFiles(event); }}
            />
            {error && <p role="alert" className="text-sm text-destructive">{error}</p>}
            {artifactFiles.length > 0 && (
              <div className="rounded-md border border-input p-3">
                {artifactFiles.map((file) => <p key={file.path} className="break-all font-mono text-xs">{file.path}</p>)}
              </div>
            )}
          </div>
        </DialogBody>
        <DialogFooter>
          <Button onClick={() => { void submit(); }} disabled={submitting || !artifactFiles.length || error !== null}>
            {submitting
              ? <Loader2 aria-hidden data-icon="inline-start" className="animate-spin" />
              : <FolderOpen aria-hidden data-icon="inline-start" />}
            {t("short_drama_import_submit_artifacts")}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export function ShortDramaImportButton({
  projectName, episode, prominent = false,
}: { projectName: string; episode?: number; prominent?: boolean }) {
  const { t } = useTranslation("dashboard");
  const open = useShortDramaImportStore((s) => s.open);
  return (
    <Button
      variant={prominent ? "default" : "outline"}
      size={prominent ? "default" : "sm"}
      onClick={() => open({ projectName, episode })}
    >
      <FolderOpen aria-hidden data-icon="inline-start" />
      {t("short_drama_import_mode_artifacts")}
    </Button>
  );
}
