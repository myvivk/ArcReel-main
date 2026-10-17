import type { ReactNode } from "react";
import { ChevronDown, ChevronLeft, ChevronRight, ChevronUp } from "lucide-react";
import { useTranslation } from "react-i18next";
import { Badge } from "@/components/ui/badge";
import { TooltipIconButton } from "./TooltipIconButton";
import { itemIdWithinEpisode } from "@/utils/episode-display";

interface ShotDetailHeaderProps {
  segmentId: string;
  index: number;
  total: number;
  /** 分镜号后面的属性：时长、状态、切分点开关等。 */
  meta: ReactNode;
  /** 前移、后移一位；缺省时不显示。 */
  onMoveEarlier?: () => void;
  onMoveLater?: () => void;
  /** 「在此后插入」「移除分镜」。 */
  structureActions?: ReactNode;
  onPrev: () => void;
  onNext: () => void;
  /** 切换分镜、移动与增删共用的禁用条件及原因。 */
  navDisabled: boolean;
  navDisabledHint?: string;
  /** 行尾的备注入口。 */
  notes?: ReactNode;
}

/** 分镜详情页头：左侧分镜号与属性，右侧序号、移动、增删、上一镜 / 下一镜与备注。 */
export function ShotDetailHeader({
  segmentId,
  index,
  total,
  meta,
  onMoveEarlier,
  onMoveLater,
  structureActions,
  onPrev,
  onNext,
  navDisabled,
  navDisabledHint,
  notes,
}: ShotDetailHeaderProps) {
  const { t } = useTranslation("dashboard");
  return (
    <div className="flex shrink-0 items-center gap-x-3 border-b border-border/60 bg-background px-5 py-3">
      <Badge translate="no">
        <span className="num">{itemIdWithinEpisode(segmentId)}</span>
      </Badge>
      <div className="flex min-w-0 flex-1 flex-wrap items-center gap-2">{meta}</div>
      <div className="flex flex-none items-center justify-end gap-0.5">
        <span className="num mr-1.5 rounded-md bg-muted px-2 py-1 text-xs text-subtle-foreground">
          {t("shot_detail_count", { current: index + 1, total })}
        </span>
        {onMoveEarlier && onMoveLater ? (
          <>
            <TooltipIconButton
              label={t("shot_move_earlier")}
              hint={navDisabledHint}
              disabled={navDisabled || index === 0}
              onClick={onMoveEarlier}
            >
              <ChevronUp aria-hidden />
            </TooltipIconButton>
            <TooltipIconButton
              label={t("shot_move_later")}
              hint={navDisabledHint}
              disabled={navDisabled || index === total - 1}
              onClick={onMoveLater}
            >
              <ChevronDown aria-hidden />
            </TooltipIconButton>
          </>
        ) : null}
        {structureActions}
        <TooltipIconButton
          label={t("shot_detail_prev")}
          hint={navDisabledHint}
          disabled={navDisabled}
          onClick={onPrev}
          shortcut="K"
        >
          <ChevronLeft aria-hidden />
        </TooltipIconButton>
        <TooltipIconButton
          label={t("shot_detail_next")}
          hint={navDisabledHint}
          disabled={navDisabled}
          onClick={onNext}
          shortcut="J"
        >
          <ChevronRight aria-hidden />
        </TooltipIconButton>
        {notes}
      </div>
    </div>
  );
}
