/** 弹窗清单里的一个条目：是否待编写、视觉层是否已有内容、视频提示词是否已写。 */
export interface PromptAuthoringEntry {
  id: string;
  pending: boolean;
  hasContent: boolean;
  /** 视频提示词已填（结构化取 `action`）：清单与预选按字段投影用（D-20）。 */
  videoPromptFilled: boolean;
}

/** 各条目形态的数组键、id 字段与视觉层字段；参考生视频单元的正文即视觉层。 */
const ENTRY_SHAPES: readonly { key: string; idField: string; fields: readonly string[] }[] = [
  { key: "scenes", idField: "scene_id", fields: ["image_prompt", "video_prompt"] },
];

function filled(value: unknown): boolean {
  if (typeof value === "string") return value.trim().length > 0;
  if (value && typeof value === "object") return Object.keys(value).length > 0;
  return Boolean(value);
}

/** 视频提示词正文：结构化取 `action`，文本形态取原串（与后端同一口径）。 */
function videoPromptFilled(value: unknown): boolean {
  if (typeof value === "string") return value.trim().length > 0;
  if (value && typeof value === "object") return filled((value as { action?: unknown }).action);
  return false;
}

/** 从正式脚本取出弹窗清单（剧本顺序）。 */
export function promptAuthoringEntries(script: unknown): PromptAuthoringEntry[] {
  if (!script || typeof script !== "object") return [];
  const record = script as Record<string, unknown>;
  const shape = ENTRY_SHAPES.find((candidate) => Array.isArray(record[candidate.key]));
  if (!shape) return [];
  const items = record[shape.key] as unknown[];
  return items.flatMap((raw) => {
    if (!raw || typeof raw !== "object") return [];
    const item = raw as Record<string, unknown>;
    const id = item[shape.idField];
    if (typeof id !== "string" || !id) return [];
    return [
      {
        id,
        pending: item.pending_authoring === true,
        hasContent: shape.fields.some((field) => filled(item[field])),
        videoPromptFilled: videoPromptFilled(item.video_prompt),
      },
    ];
  });
}
