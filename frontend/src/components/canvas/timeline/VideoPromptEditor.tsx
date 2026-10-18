import { useMemo } from "react";
import { useTranslation } from "react-i18next";
import { Textarea } from "@/components/ui/textarea";

/**
 * 视频提示词的逐段编辑（11.8）。
 *
 * 规范要求按宫格写「K0→Kn 怎么动」，编辑器就把 `action` 里的 `0-2秒：…` 切成与宫格同数的段，
 * 改哪段只改那段。切不出段（存量整段文本）就退化成单个文本框，并说明视频生成仍用整段——
 * 不强转、不清空。
 */

interface Segment {
  start: number;
  end: number;
  text: string;
}

const SEGMENT_MARK = /^[ \t]*(\d+(?:\.\d+)?)\s*[-–]\s*(\d+(?:\.\d+)?)\s*秒\s*[：:][ \t]*/;

function formatSeconds(value: number): string {
  const rounded = Math.round(value * 100) / 100;
  return Number.isInteger(rounded) ? String(rounded) : String(rounded).replace(/0+$/, "").replace(/\.$/, "");
}

function windows(duration: number, count: number): { start: number; end: number }[] {
  if (count <= 0) return [];
  const step = duration / count;
  return Array.from({ length: count }, (_, index) => ({
    start: Math.round(index * step * 100) / 100,
    end: Math.round((index === count - 1 ? duration : (index + 1) * step) * 100) / 100,
  }));
}

/** 与后端 `split_action_segments` 同一口径：段数对齐格数，多余并进末段，不足留空。 */
export function splitSegments(action: string, duration: number, count: number): Segment[] | null {
  if (count <= 0) return null;
  const text = action.trim();
  if (!text) return null;
  const marks = [...text.matchAll(new RegExp(SEGMENT_MARK.source, "gm"))];
  if (marks.length === 0) return null;
  const bodies = marks.map((mark, index) => {
    const bodyStart = (mark.index ?? 0) + mark[0].length;
    const bodyEnd = index + 1 < marks.length ? (marks[index + 1].index ?? text.length) : text.length;
    return text
      .slice(bodyStart, bodyEnd)
      .split("\n")
      .map((line) => line.trim())
      .filter(Boolean)
      .join("\n");
  });
  const merged =
    bodies.length > count
      ? [...bodies.slice(0, count - 1), bodies.slice(count - 1).filter(Boolean).join("\n")]
      : [...bodies, ...Array.from({ length: count - bodies.length }, () => "")];
  return windows(duration, count).map((span, index) => ({ ...span, text: merged[index] }));
}

/** 时间按当前时长重排、正文不动；来回一遍不漂移。 */
export function joinSegments(segments: Segment[], duration: number): string {
  const spans = duration > 0 ? windows(duration, segments.length) : segments;
  return segments
    .map((segment, index) => {
      const span = spans[index] ?? segment;
      return `${formatSeconds(span.start)}-${formatSeconds(span.end)}秒：${segment.text.trim()}`;
    })
    .join("\n");
}

interface Props {
  value: string;
  onChange: (value: string) => void;
  /** 当前宫格已确认的格数；0 表示没有可分的段。 */
  cellCount: number;
  durationSeconds: number;
  readOnly: boolean;
  ariaLabel: string;
  placeholder: string;
}

export function VideoPromptEditor({
  value,
  onChange,
  cellCount,
  durationSeconds,
  readOnly,
  ariaLabel,
  placeholder,
}: Props) {
  const { t } = useTranslation("dashboard");
  const segments = useMemo(
    () => splitSegments(value, durationSeconds, cellCount),
    [value, durationSeconds, cellCount],
  );

  if (segments === null) {
    return (
      <div className="flex flex-col gap-1">
        <Textarea
          aria-label={ariaLabel}
          value={value}
          onChange={(event) => onChange(event.target.value)}
          readOnly={readOnly}
          placeholder={placeholder}
          className="max-h-none min-h-22"
        />
        {cellCount > 0 && value.trim() ? (
          <p className="text-xs text-muted-foreground">{t("video_prompt_fallback_hint")}</p>
        ) : null}
      </div>
    );
  }

  const update = (index: number, text: string) => {
    const next = segments.map((segment, position) => (position === index ? { ...segment, text } : segment));
    onChange(joinSegments(next, durationSeconds));
  };

  return (
    <div className="flex flex-col gap-2">
      <p className="text-xs text-muted-foreground">{t("video_prompt_segments_hint", { count: cellCount })}</p>
      {segments.map((segment, index) => (
        <label key={index} className="flex flex-col gap-1">
          <span className="font-mono text-xs text-muted-foreground">
            K{index} · {formatSeconds(segment.start)}-{formatSeconds(segment.end)}s
          </span>
          <Textarea
            aria-label={`${ariaLabel} K${index}`}
            value={segment.text}
            onChange={(event) => update(index, event.target.value)}
            readOnly={readOnly}
            placeholder={t("video_prompt_segment_placeholder")}
            className="max-h-none min-h-16"
          />
        </label>
      ))}
    </div>
  );
}
