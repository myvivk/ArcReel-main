import { useCallback, useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { ChevronDown, ChevronUp } from "lucide-react";
import { useConfirmLeave } from "@/components/shared/edit-unit/LeaveGuard";
import { EditUnitRetentionContext, RetainedEditUnit } from "@/components/shared/edit-unit/RetainedEditUnit";
import { Button } from "@/components/ui/button";
import { Sheet, SheetContent } from "@/components/ui/sheet";
import type { AssetSheetStatusRow, AssetSheetType } from "@/types";
import { AssetDetailEditor } from "./AssetDetailEditor";
import type { GalleryAssetSource } from "./gallery-model";

export interface AssetEditorSheetProps {
  projectName: string;
  assetType: AssetSheetType;
  assets: Record<string, GalleryAssetSource>;
  /** 正在看的资产名；为 null 时关闭。 */
  name: string | null;
  /** 切换、关闭与改名都经这里改当前资产；有未保存修改时已先经离开拦截放行。 */
  onNameChange: (name: string | null) => void;
  /** 上一个与下一个的顺序，如画廊筛选后的资产名；当前资产不在其中时按全部资产的顺序。 */
  order?: readonly string[];
  statusByName: ReadonlyMap<string, AssetSheetStatusRow>;
  generatingNames?: ReadonlySet<string>;
  /** 卡片上本地上传等写入，与详情里的入口互斥。 */
  writingNames?: ReadonlySet<string>;
  readOnly: boolean;
  onGenerate: (name: string) => unknown;
}

/**
 * 资产详情 Sheet：右侧约 560px、覆盖全视口的模态弹层，可以盖住 Agent 面板。头部有上一个与下一个，
 * 切换资产与关闭 Sheet 时有未保存修改先经离开拦截询问。
 *
 * 正在编辑的资产被外部删除或改名（如 Agent 的改动）时，有未保存修改就继续显示原来的内容，
 * 在底部提示条上说明；保存或放弃之后才关闭。
 */
export function AssetEditorSheet({
  projectName,
  assetType,
  assets,
  name,
  onNameChange,
  order,
  statusByName,
  generatingNames,
  writingNames,
  readOnly,
  onGenerate,
}: AssetEditorSheetProps) {
  const { t } = useTranslation(["assets", "common"]);
  const confirmLeave = useConfirmLeave();
  // 自己刚改的名：项目数据刷新之前新名称还不在 assets 里，先沿用旧名下的内容
  const [renamed, setRenamed] = useState<{ from: string; to: string } | null>(null);
  // 编辑器的会话：打开或切换资产时换一个新编辑器；自己改名不换，未保存修改不丢
  const [session, setSession] = useState({ name, id: 0 });
  if (session.name !== name) {
    const selfRename = renamed !== null && name === renamed.to && session.name === renamed.from;
    // 关闭时不换：关闭动画期间仍是原来的编辑器
    setSession({ name, id: selfRename || name === null ? session.id : session.id + 1 });
    if (!selfRename && renamed !== null) setRenamed(null);
  }
  // 新名称已进项目数据：桥接结束，之后旧名下出现的是别的资产
  if (renamed !== null && session.name === renamed.to && assets[renamed.to]) setRenamed(null);

  const live = name === null ? undefined : (assets[name] ?? (renamed?.to === name ? assets[renamed.from] : undefined));
  // 外部删除或改名后 live 为空，保留期间与关闭动画期间沿用最后一次的内容
  const [last, setLast] = useState<{ name: string; asset: GalleryAssetSource } | null>(null);
  if (name !== null && live && (last?.name !== name || last.asset !== live)) setLast({ name, asset: live });
  const [held, setHeld] = useState(false);
  // 只有 RetainedEditUnit 一个登记方
  const retention = useMemo(() => ({ protect: (_key: string, value: boolean) => setHeld(value) }), []);

  const present = live !== undefined;
  const open = name !== null && (present || held);
  useEffect(() => {
    if (name !== null && !present && !held) onNameChange(null);
  }, [name, present, held, onNameChange]);

  const names = Object.keys(assets);
  const sequence = name !== null && order?.includes(name) ? order : names;
  const index = name === null ? -1 : sequence.indexOf(name);
  const previous = index > 0 ? sequence[index - 1] : undefined;
  const next = index >= 0 && index < sequence.length - 1 ? sequence[index + 1] : undefined;
  const switchTo = (to: string) =>
    confirmLeave(() => onNameChange(to), { saveLabel: t("common:save_and_switch") });

  const handleRenamed = useCallback(
    (from: string, to: string) => {
      setRenamed({ from, to });
      onNameChange(to);
    },
    [onNameChange],
  );
  const close = useCallback(() => onNameChange(null), [onNameChange]);

  const navigation =
    names.length > 1 && index >= 0 ? (
      <div className="flex shrink-0 items-center">
        <Button
          variant="ghost"
          size="icon-sm"
          disabled={previous === undefined}
          onClick={() => previous !== undefined && switchTo(previous)}
          aria-label={t("assets:editor_previous")}
        >
          <ChevronUp aria-hidden />
        </Button>
        <Button
          variant="ghost"
          size="icon-sm"
          disabled={next === undefined}
          onClick={() => next !== undefined && switchTo(next)}
          aria-label={t("assets:editor_next")}
        >
          <ChevronDown aria-hidden />
        </Button>
      </div>
    ) : null;

  const shownName = name ?? last?.name ?? null;
  const shownAsset = live ?? (shownName !== null && last?.name === shownName ? last.asset : undefined);

  return (
    <Sheet
      open={open}
      onOpenChange={(nextOpen) => {
        if (nextOpen) return;
        confirmLeave(close);
      }}
    >
      <SheetContent side="right" className="data-[side=right]:w-140">
        {shownName !== null && shownAsset ? (
          <EditUnitRetentionContext.Provider value={retention}>
            <RetainedEditUnit
              identity={live ? `${session.id}` : `${session.id}:removed`}
              value={{ name: shownName, asset: shownAsset }}
              message={t("assets:editor_asset_removed")}
            >
              {(shown) => (
                <AssetDetailEditor
                  key={session.id}
                  projectName={projectName}
                  assetType={assetType}
                  name={shown.name}
                  asset={shown.asset}
                  sheetStatus={statusByName.get(shown.name)}
                  generating={generatingNames?.has(shown.name) ?? false}
                  writing={writingNames?.has(shown.name) ?? false}
                  readOnly={readOnly}
                  onGenerate={onGenerate}
                  onRenamed={handleRenamed}
                  navigation={navigation}
                />
              )}
            </RetainedEditUnit>
          </EditUnitRetentionContext.Provider>
        ) : null}
      </SheetContent>
    </Sheet>
  );
}
