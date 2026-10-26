import type { ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import type { GalleryFilter } from "./gallery-model";

const FILTERS: GalleryFilter[] = ["all", "pending", "stale"];

/**
 * 画廊工具栏：标题与数量、按资产图状态筛选与批量生成。
 * 吸在画廊滚动区顶部；画布变窄时控件换行。
 */
export function GalleryToolbar({
  title,
  count,
  filter,
  onFilterChange,
  filterCounts,
  children,
}: {
  title: string;
  count: number;
  filter: GalleryFilter;
  onFilterChange: (filter: GalleryFilter) => void;
  /** 各筛选项命中的数量，「全部」即 `count`。 */
  filterCounts: Record<Exclude<GalleryFilter, "all">, number>;
  /** 批量生成入口。 */
  children?: ReactNode;
}) {
  const { t } = useTranslation("assets");
  const counts: Record<GalleryFilter, number> = { all: count, ...filterCounts };
  return (
    <div className="sticky top-0 z-sticky flex shrink-0 flex-wrap items-center gap-x-3 gap-y-2 border-b border-border bg-background px-5 py-2.5">
      <h2 className="flex items-baseline gap-2 text-base font-semibold">
        {title}
        <span className="text-sm font-normal text-muted-foreground tabular-nums">{count}</span>
      </h2>
      <ToggleGroup
        aria-label={t("sheet_filter_label")}
        variant="outline"
        size="sm"
        value={[filter]}
        onValueChange={(next: string[]) => {
          // 再点已选中的一项不取消选择，筛选总有一项生效
          if (next[0]) onFilterChange(next[0] as GalleryFilter);
        }}
      >
        {FILTERS.map((value) => (
          <ToggleGroupItem key={value} value={value}>
            {t(`sheet_filter_${value}`)}
            {value !== "all" && <span className="text-muted-foreground tabular-nums">{counts[value]}</span>}
          </ToggleGroupItem>
        ))}
      </ToggleGroup>
      <div className="flex flex-1 flex-wrap items-center justify-end gap-2">{children}</div>
    </div>
  );
}
