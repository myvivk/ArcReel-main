import { useCallback, useId, useRef, useState, type CSSProperties, type KeyboardEvent, type PointerEvent, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { cn } from "cn";

const DEFAULT_MEDIA_PERCENT = 30;
const MIN_MEDIA_PERCENT = 30;
const MAX_MEDIA_PERCENT = 80;
const MIN_EDITOR_PERCENT = 20;
const MEDIA_PERCENT_STEP = 2;
const MEDIA_PERCENT_STORAGE_KEY = "arcreel_shot_detail_media_percent";

function readMediaPercent(): number {
  try {
    const value = Number(localStorage.getItem(MEDIA_PERCENT_STORAGE_KEY));
    return Number.isFinite(value) && value >= MIN_MEDIA_PERCENT && value <= MAX_MEDIA_PERCENT
      ? value
      : DEFAULT_MEDIA_PERCENT;
  } catch {
    return DEFAULT_MEDIA_PERCENT;
  }
}

function persistMediaPercent(value: number) {
  try {
    localStorage.setItem(MEDIA_PERCENT_STORAGE_KEY, String(value));
  } catch {
    // The layout can still be used when storage is unavailable.
  }
}

/**
 * 分镜详情的栏位：页头在上，中间是「编辑栏 + 媒体栏」两栏，各自滚动，未保存提示条在最下方。
 * 宽屏默认编辑栏 70%、媒体栏 30%，中间的虚线手柄可拖动调整比例，媒体栏不会小于 30%。
 * 窄屏仍上下排成一栏，避免编辑与媒体内容被压缩到不可用宽度。
 */
export function ShotDetailLayout({
  header,
  footer,
  main,
  media,
}: {
  header: ReactNode;
  /** 两栏下方横跨整宽的未保存提示条：出现时不推动正在编辑的字段。 */
  footer?: ReactNode;
  main: ReactNode;
  media: ReactNode;
}) {
  const { t } = useTranslation("dashboard");
  const [mediaPercent, setMediaPercent] = useState(readMediaPercent);
  const [resizing, setResizing] = useState(false);
  const detailRef = useRef<HTMLDivElement>(null);
  const resizeState = useRef<{ startX: number; startMediaPercent: number } | null>(null);

  const stopResize = useCallback(() => {
    resizeState.current = null;
    setResizing(false);
  }, []);

  const updateMediaPercent = useCallback((next: number) => {
    const clamped = Math.min(100 - MIN_EDITOR_PERCENT, Math.max(MIN_MEDIA_PERCENT, next));
    setMediaPercent(clamped);
    persistMediaPercent(clamped);
  }, []);

  const handlePointerMove = useCallback((event: PointerEvent<HTMLButtonElement>) => {
    const state = resizeState.current;
    const detail = detailRef.current;
    if (!state || !detail) return;
    const width = detail.getBoundingClientRect().width;
    if (width <= 0) return;
    const deltaPercent = ((state.startX - event.clientX) / width) * 100;
    updateMediaPercent(state.startMediaPercent + deltaPercent);
  }, [updateMediaPercent]);

  const handlePointerDown = useCallback((event: PointerEvent<HTMLButtonElement>) => {
    if (event.button !== 0) return;
    event.currentTarget.setPointerCapture(event.pointerId);
    resizeState.current = { startX: event.clientX, startMediaPercent: mediaPercent };
    setResizing(true);
  }, [mediaPercent]);

  const handleSeparatorDoubleClick = useCallback(() => {
    setMediaPercent(DEFAULT_MEDIA_PERCENT);
    persistMediaPercent(DEFAULT_MEDIA_PERCENT);
  }, []);

  const handleSeparatorKeyDown = useCallback((event: KeyboardEvent<HTMLButtonElement>) => {
    if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
    event.preventDefault();
    const delta = event.key === "ArrowLeft" ? -MEDIA_PERCENT_STEP : MEDIA_PERCENT_STEP;
    updateMediaPercent(mediaPercent + delta);
  }, [mediaPercent, updateMediaPercent]);

  return (
    <div className="flex min-h-0 min-w-0 flex-col bg-background">
      {header}
      <div ref={detailRef} className="@container/shot-detail flex min-h-0 flex-1 flex-col">
        <div
          className="relative grid min-h-0 flex-1 auto-rows-max grid-cols-[minmax(0,1fr)] content-start overflow-y-auto bg-background [scrollbar-gutter:stable] @min-[480px]/shot-detail:grid-cols-[minmax(0,1fr)_300px] @min-[480px]/shot-detail:grid-rows-[minmax(0,1fr)] @min-[480px]/shot-detail:content-stretch @min-[480px]/shot-detail:overflow-visible @min-[860px]/shot-detail:grid-cols-[minmax(0,1fr)_minmax(505px,var(--shot-media-percent))]"
          style={{
            "--shot-media-percent": `${mediaPercent}%`,
          } as CSSProperties}
        >
          <div className="relative min-h-0 @min-[480px]/shot-detail:overflow-y-auto @min-[480px]/shot-detail:[scrollbar-gutter:stable]">
            <div className="flex flex-col gap-5 px-5 pt-5 pb-8">{main}</div>
          </div>
          {/* 分隔条：按钮承载拖动与键盘调整，role=separator 让读屏识别为可调大小的分隔条。
              react-resizable-panels 的 Separator 在 jsdom 下不渲染，这里手写同等语义的按钮。 */}
          <button
            type="button"
            role="separator"
            aria-orientation="vertical"
            aria-label={t("resize_shot_detail")}
            aria-valuemin={MIN_MEDIA_PERCENT}
            aria-valuemax={100 - MIN_EDITOR_PERCENT}
            aria-valuenow={Math.round(mediaPercent)}
            className={cn(
              "group absolute top-0 bottom-0 hidden w-3 -translate-x-1/2 cursor-col-resize touch-none items-center justify-center border-0 bg-transparent p-0 @min-[860px]/shot-detail:flex",
              resizing && "cursor-col-resize",
            )}
            style={{ left: `calc(100% - ${mediaPercent}%)` }}
            onDoubleClick={handleSeparatorDoubleClick}
            onPointerDown={handlePointerDown}
            onPointerMove={handlePointerMove}
            onPointerUp={stopResize}
            onPointerCancel={stopResize}
            onKeyDown={handleSeparatorKeyDown}
          >
            <span
              aria-hidden="true"
              className={cn(
                "h-full w-px border-l border-dashed border-border/80 transition-colors duration-fast group-hover:border-primary group-hover:shadow-[0_0_0_3px_color-mix(in_oklab,var(--primary)_18%,transparent)]",
                resizing && "border-primary shadow-[0_0_0_3px_color-mix(in_oklab,var(--primary)_24%,transparent)]",
              )}
            />
          </button>
          <div className="@container/shot-media relative min-h-0 border-t border-border/50 bg-muted/20 px-5 pt-5 pb-8 @min-[480px]/shot-detail:overflow-y-auto @min-[480px]/shot-detail:border-t-0 @min-[480px]/shot-detail:border-l">
            {media}
          </div>
        </div>
      </div>
      {footer}
    </div>
  );
}

/** 编辑栏里的一组内容；组与组之间用分隔线隔开。 */
export function ShotGroup({ children }: { children: ReactNode }) {
  return (
    <div className="flex flex-col gap-4 border-t border-border/50 pt-5 first:border-t-0 first:pt-0">{children}</div>
  );
}

/**
 * 编辑栏里的一段：小标题加内容。给了 `htmlFor` 时标题是对应输入框的 label，
 * 否则是一个三级标题；`actions` 放在标题行右侧。
 */
export function ShotSection({
  title,
  icon,
  htmlFor,
  actions,
  children,
}: {
  title: string;
  icon?: ReactNode;
  htmlFor?: string;
  actions?: ReactNode;
  children: ReactNode;
}) {
  const titleId = useId();
  const heading = htmlFor ? (
    <label id={titleId} htmlFor={htmlFor} className="text-xs font-medium text-muted-foreground">
      {title}
    </label>
  ) : (
    <h3 id={titleId} className="text-xs font-medium text-muted-foreground">
      {title}
    </h3>
  );
  return (
    <section aria-labelledby={titleId} className="flex min-w-0 flex-col gap-2">
      <div className="flex min-h-7 items-center gap-1.5">
        {icon ? <span className="flex text-muted-foreground">{icon}</span> : null}
        {heading}
        {actions ? <div className="ml-auto flex items-center gap-1">{actions}</div> : null}
      </div>
      {children}
    </section>
  );
}
