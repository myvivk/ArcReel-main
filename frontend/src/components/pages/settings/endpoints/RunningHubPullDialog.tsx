import { useMemo, useState } from "react";
import { Cloud, Loader2 } from "lucide-react";
import { useTranslation } from "react-i18next";
import { API } from "@/api";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import type { ComfyuiEndpointDefinition, ComfyuiInferResponse, CustomProviderInfo } from "@/types";
import { errMsg } from "@/utils/async";

interface RunningHubPullDialogProps {
  open: boolean;
  /** 已保存的全部自定义供应商；组件自己筛 RunningHub。 */
  providers: CustomProviderInfo[];
  /** 刷新已有端点时预填其 workflowId；新建时为空。 */
  initialWorkflowId?: string;
  onClose: () => void;
  onPulled: (definition: ComfyuiEndpointDefinition, inference: ComfyuiInferResponse) => void;
}

/**
 * 按 workflowId 从 RunningHub 拉取 API workflow。
 *
 * 只读：不提交任务、不扣币。拉取成功后把定义与绑定推断交给上层直接进绑定编辑器，不在这里落盘。
 */
export function RunningHubPullDialog({
  open,
  providers,
  initialWorkflowId = "",
  onClose,
  onPulled,
}: RunningHubPullDialogProps) {
  const { t } = useTranslation(["dashboard", "common"]);
  const runninghub = useMemo(
    () => providers.filter((provider) => provider.discovery_format === "runninghub"),
    [providers],
  );
  const [mode, setMode] = useState<"provider" | "inline">(runninghub.length > 0 ? "provider" : "inline");
  const [providerId, setProviderId] = useState(() => (runninghub[0] ? String(runninghub[0].id) : ""));
  const [baseUrl, setBaseUrl] = useState("https://www.runninghub.cn");
  const [apiKey, setApiKey] = useState("");
  const [workflowId, setWorkflowId] = useState(initialWorkflowId);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async () => {
    const id = workflowId.trim();
    if (!id) {
      setError(t("ce_rh_pull_workflow_id_required"));
      return;
    }
    if (mode === "provider" && !providerId) {
      setError(t("ce_rh_pull_provider_required"));
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const result = await API.pullRunningHubWorkflow(
        mode === "provider"
          ? { workflow_id: id, provider_id: `custom-${providerId}` }
          : { workflow_id: id, base_url: baseUrl.trim(), api_key: apiKey },
      );
      onPulled(result.definition, result.inference);
    } catch (e) {
      setError(errMsg(e, t("ce_rh_pull_failed")));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={(next) => !next && onClose()}>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle>{t("ce_rh_pull_title")}</DialogTitle>
          <DialogDescription>{t("ce_rh_pull_desc")}</DialogDescription>
        </DialogHeader>
        <DialogBody>
          <div className="flex flex-col gap-4">
          <div className="flex flex-col gap-1.5">
            <span className="text-sm font-medium">{t("ce_rh_pull_credentials")}</span>
            <Select value={mode} onValueChange={(next) => setMode(next as "provider" | "inline")}>
              <SelectTrigger className="w-full">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="provider" disabled={runninghub.length === 0}>
                  {t("ce_rh_pull_provider")}
                </SelectItem>
                <SelectItem value="inline">{t("ce_rh_pull_inline")}</SelectItem>
              </SelectContent>
            </Select>
          </div>

          {mode === "provider" ? (
            <div className="flex flex-col gap-1.5">
              <span className="text-sm font-medium">{t("ce_trial_provider")}</span>
              <Select value={providerId} onValueChange={(value) => setProviderId(value ?? "")}>
                <SelectTrigger className="w-full">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {runninghub.map((provider) => (
                    <SelectItem key={provider.id} value={String(provider.id)}>
                      {provider.display_name}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          ) : (
            <>
              <div className="flex flex-col gap-1.5">
                <span className="text-sm font-medium">{t("base_url")}</span>
                <Input
                  mono
                  value={baseUrl}
                  autoComplete="off"
                  onChange={(e) => setBaseUrl(e.target.value)}
                  placeholder="https://www.runninghub.cn"
                />
              </div>
              <div className="flex flex-col gap-1.5">
                <span className="text-sm font-medium">{t("credential_secret_label")}</span>
                <Input
                  mono
                  type="password"
                  autoComplete="off"
                  value={apiKey}
                  onChange={(e) => setApiKey(e.target.value)}
                />
              </div>
            </>
          )}

          <div className="flex flex-col gap-1.5">
            <span className="text-sm font-medium">{t("ce_rh_workflow_id_label")}</span>
            <Input
              mono
              autoComplete="off"
              value={workflowId}
              onChange={(e) => setWorkflowId(e.target.value)}
              placeholder={t("ce_rh_workflow_id_placeholder")}
            />
            <p className="text-xs text-muted-foreground">{t("ce_rh_workflow_id_hint")}</p>
          </div>

          {error && (
            <Alert variant="destructive">
              <AlertDescription>{error}</AlertDescription>
            </Alert>
          )}
          </div>
        </DialogBody>
        <DialogFooter>
          <Button variant="ghost" onClick={onClose} disabled={busy}>
            {t("common:cancel")}
          </Button>
          <Button onClick={() => void submit()} disabled={busy}>
            {busy ? <Loader2 aria-hidden data-icon="inline-start" className="animate-spin" /> : <Cloud aria-hidden data-icon="inline-start" />}
            {t("ce_rh_pull_action")}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
