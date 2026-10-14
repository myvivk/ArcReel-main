import { useState } from "react";
import { useTranslation } from "react-i18next";
import { Sparkles } from "lucide-react";
import { Button } from "@/components/ui/button";
import { API } from "@/api";
import { AssetSheetBatchDialog } from "./AssetSheetBatchDialog";
import { pendingSheetCounts } from "./useAssetSheetStatus";
import { useActiveResourceIds } from "@/stores/tasks-store";
import type { AssetSheetStatusRow, AssetSheetType } from "@/types";

/**
 * 画廊工具栏上的「生成待生成的 X（N）」批量入口；旁边注明另有几个因缺描述不会生成。
 */
export function AssetSheetBatchControls({
  projectName,
  assetType,
  rows,
}: {
  projectName: string;
  assetType: AssetSheetType;
  rows: AssetSheetStatusRow[];
}) {
  const { t } = useTranslation("assets");
  const [open, setOpen] = useState(false);
  const [filling, setFilling] = useState(false);
  const [result, setResult] = useState<{ filled: number; skipped: number; failed: number } | null>(null);
  const [errored, setErrored] = useState(false);
  const activeOwners = useActiveResourceIds(assetType, projectName);
  const activeDerivatives = useActiveResourceIds("character_derivative", projectName);
  const { generatable, missingDescription } = pendingSheetCounts(rows, assetType, (row) =>
    row.derivative === null ? activeOwners.has(row.name) : activeDerivatives.has(`${row.name}/${row.derivative}`),
  );

  // 缺描述是资产图生成的硬前置：在此就地给「一键补全」入口，省去逐个打开资产详情再点 AI 补全。
  async function fillMissing() {
    setFilling(true);
    setResult(null);
    setErrored(false);
    try {
      const res = await API.fillMissingAssetDescriptions(projectName, assetType);
      setResult({ filled: res.filled.length, skipped: res.skipped.length, failed: res.failed.length });
    } catch {
      setErrored(true);
    } finally {
      setFilling(false);
    }
  }

  return (
    <>
      {missingDescription > 0 && (
        <span className="flex items-center gap-2 text-xs text-muted-foreground">
          <span>{t("sheet_batch_missing_description", { count: missingDescription })}</span>
          <Button variant="ghost" size="xs" onClick={() => void fillMissing()} disabled={filling}>
            {filling ? "…" : t("batch_ai_fill")}
          </Button>
          {errored && <span className="text-destructive">{t("ai_fill_failed")}</span>}
          {result && <span className="text-foreground">{t("batch_ai_fill_done", result)}</span>}
        </span>
      )}
      {generatable > 0 && (
        <Button variant="outline" size="sm" onClick={() => setOpen(true)}>
          <Sparkles aria-hidden data-icon="inline-start" />
          {t(`sheet_batch_button.${assetType}`, { count: generatable })}
        </Button>
      )}
      {open && (
        <AssetSheetBatchDialog
          projectName={projectName}
          scope={{ asset_type: assetType }}
          onClose={() => setOpen(false)}
        />
      )}
    </>
  );
}
