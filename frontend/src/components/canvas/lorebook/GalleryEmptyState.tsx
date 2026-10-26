import { useTranslation } from "react-i18next";
import { Empty, EmptyDescription, EmptyHeader, EmptyMedia, EmptyTitle } from "@/components/ui/empty";
import type { AssetSheetType } from "@/types";
import { ASSET_TYPE_ICON } from "./AssetBrowseCard";

/**
 * 画廊还没有资产时的空状态：说明这类资产的来源。
 * 项目资产由各集生成后汇总到这里，全局页不提供任何新增、导入、入库或合并入口，所以只说明来源、不给按钮。
 */
export function GalleryEmptyState({ assetType }: { assetType: AssetSheetType }) {
  const { t } = useTranslation("assets");
  const Icon = ASSET_TYPE_ICON[assetType];
  return (
    <Empty>
      <EmptyHeader>
        <EmptyMedia variant="icon">
          <Icon aria-hidden />
        </EmptyMedia>
        <EmptyTitle>{t(`gallery_empty_title.${assetType}`)}</EmptyTitle>
        <EmptyDescription>{t("gallery_empty_hint")}</EmptyDescription>
      </EmptyHeader>
    </Empty>
  );
}
