import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { assetColor } from "@/components/canvas/reference/asset-colors";
import { Popover, PopoverContent } from "@/components/ui/popover";
import { API } from "@/api";
import { useProjectsStore } from "@/stores/projects-store";
import { formatReferenceName, normalizeAssetName, splitDerivativeReference } from "@/utils/reference-mentions";
import type { AssetKind } from "@/types/reference-video";

/** 候选资产：引用名 + 资产表图路径（相对项目根，可为空）。 */
interface MentionCandidate {
  name: string;
  imagePath: string | null;
}

/** 按资产类型分组的候选；缺少的类型按空组处理。 */
export type MentionCandidateGroups = Partial<Record<AssetKind, MentionCandidate[]>>;

const MENTION_PICKER_DEFAULT_ID = "mention-picker";

type TabKey = "all" | AssetKind;

const ALL_KINDS: AssetKind[] = ["product", "character", "scene", "prop"];
const GROUP_ORDER: AssetKind[] = ALL_KINDS;

export interface MentionPickerProps {
  open: boolean;
  /** `@` 之后已输入的过滤词；空串列出全部候选。 */
  query: string;
  candidates: MentionCandidateGroups;
  /** 参与候选与页签的资产类型；分镜提示词只让选角色 / 场景 / 道具。 */
  kinds?: AssetKind[];
  onSelect: (ref: { type: AssetKind; name: string }) => void;
  onClose: () => void;
  /** 用于拼缩略图 URL。缺省时只显示类型圆点。 */
  projectName?: string;
  className?: string;
  /** 列表的 DOM id；textarea 用它写 aria-controls。 */
  listboxId?: string;
  /** 键盘高亮项变化时回调其 DOM id（null 表示没有候选），供 textarea 写 aria-activedescendant。 */
  onActiveChange?: (optionId: string | null) => void;
  /** 弹层定位锚点（编辑器里的光标占位元素）。弹层经 Portal 渲染，祖先的 overflow 裁不到它。 */
  anchorElement?: HTMLElement | null;
}

function optionId(kind: AssetKind, name: string): string {
  // 安全化：把 CSS 不友好字符替换，避免选择器查询出错。
  const safe = name.replace(/[^A-Za-z0-9_一-鿿-]/g, "_");
  return `${MENTION_PICKER_DEFAULT_ID}-option-${kind}-${safe}`;
}

interface FlatItem {
  type: AssetKind;
  name: string;
  imagePath: string | null;
  globalIndex: number;
}

/**
 * 编辑器里输入 `@` 后弹出的引用资产候选。
 *
 * 焦点留在编辑器：方向键、回车与 Tab 由本组件的键盘监听处理，候选用 `aria-activedescendant`
 * 指向；Esc 与外部点击由 Popover 统一接管。候选按资产类型分组，也可按类型切页签。
 */
export function MentionPicker({
  open,
  query,
  candidates,
  kinds = ALL_KINDS,
  onSelect,
  onClose,
  projectName,
  className,
  listboxId,
  onActiveChange,
  anchorElement,
}: MentionPickerProps) {
  const { t } = useTranslation("dashboard");
  // 缩略图地址带资产指纹：重新生成后换地址，未变化时命中浏览器的长缓存
  const fingerprints = useProjectsStore((s) => s.assetFingerprints);
  const [activeIndex, setActiveIndex] = useState(0);
  const [activeTab, setActiveTab] = useState<TabKey>("all");
  // 过滤词或页签变化时把键盘高亮复位到第一项——渲染期同步 state，不用 effect。
  const [syncedQuery, setSyncedQuery] = useState(query);
  const [syncedTab, setSyncedTab] = useState<TabKey>(activeTab);
  if (syncedQuery !== query || syncedTab !== activeTab) {
    setSyncedQuery(query);
    setSyncedTab(activeTab);
    setActiveIndex(0);
  }

  const tabs: TabKey[] = useMemo(() => ["all", ...kinds], [kinds]);

  // 比较侧统一 NFC：候选携带资产原始名（落盘可能是 NFD），query 来自输入法，编码不保证一致；
  // 只归一比较用的副本，展示与选中回传仍用原始 name。
  const filteredByQuery = useMemo(() => {
    const q = normalizeAssetName(query.trim()).toLowerCase();
    const result: Record<AssetKind, MentionCandidate[]> = { product: [], character: [], scene: [], prop: [] };
    for (const kind of GROUP_ORDER) {
      const arr = candidates[kind] ?? [];
      result[kind] = q.length === 0 ? arr : arr.filter((c) => normalizeAssetName(c.name).toLowerCase().includes(q));
    }
    return result;
  }, [candidates, query]);

  const filtered = useMemo(() => {
    if (activeTab === "all") return filteredByQuery;
    // 单页签：保留选中类型，其余置空数组（下游读取契约不变）。
    return {
      product: activeTab === "product" ? filteredByQuery.product : [],
      character: activeTab === "character" ? filteredByQuery.character : [],
      scene: activeTab === "scene" ? filteredByQuery.scene : [],
      prop: activeTab === "prop" ? filteredByQuery.prop : [],
    } satisfies Record<AssetKind, MentionCandidate[]>;
  }, [filteredByQuery, activeTab]);

  const totalsByKind = useMemo<Record<AssetKind, number>>(
    () => ({
      product: filteredByQuery.product.length,
      character: filteredByQuery.character.length,
      scene: filteredByQuery.scene.length,
      prop: filteredByQuery.prop.length,
    }),
    [filteredByQuery],
  );

  const flat: FlatItem[] = useMemo(() => {
    const out: FlatItem[] = [];
    let idx = 0;
    for (const kind of kinds) {
      for (const item of filtered[kind]) {
        out.push({ type: kind, name: item.name, imagePath: item.imagePath, globalIndex: idx });
        idx += 1;
      }
    }
    return out;
  }, [filtered, kinds]);

  const indexByKey = useMemo(() => {
    const m = new Map<string, number>();
    for (const f of flat) m.set(`${f.type}:${f.name}`, f.globalIndex);
    return m;
  }, [flat]);

  // 先夹住高亮下标：候选在同一次渲染里变短时，击键不会落到 undefined 上。
  const clampedActive = Math.min(activeIndex, Math.max(0, flat.length - 1));

  const flatRef = useRef(flat);
  const clampedRef = useRef(clampedActive);
  // 真实鼠标坐标：列表滚动（键盘选中触发）会让元素移到静止光标下并补发 mouseenter，
  // 只有坐标变化才算用户主动移动。
  const lastPointerXY = useRef<{ x: number; y: number }>({ x: -1, y: -1 });

  useLayoutEffect(() => {
    flatRef.current = flat;
    clampedRef.current = clampedActive;
  });

  // 只处理导航与补全键；Esc 与外部点击由 Popover 的 onOpenChange 统一接管。
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      // 输入法组合中回车是在确认候选词，不能当成选中资产。
      if (e.isComposing) return;
      const current = flatRef.current;
      const active = clampedRef.current;
      if (e.key === "ArrowDown") {
        e.preventDefault();
        setActiveIndex(Math.min(current.length - 1, active + 1));
      } else if (e.key === "ArrowUp") {
        e.preventDefault();
        setActiveIndex(Math.max(0, active - 1));
      } else if (e.key === "Enter" || (e.key === "Tab" && !e.shiftKey)) {
        // Tab 补全（仅正向）：与 Enter 同义，阻止默认 tab-out。Shift+Tab 保留原生反向焦点切换。
        e.preventDefault();
        const item = current[active];
        if (item) onSelect({ type: item.type, name: item.name });
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onSelect]);

  useEffect(() => {
    if (!onActiveChange) return;
    const current = flat[clampedActive];
    onActiveChange(current ? optionId(current.type, current.name) : null);
  }, [flat, clampedActive, onActiveChange]);

  const empty = flat.length === 0;
  const noCandidates = kinds.every((kind) => (candidates[kind] ?? []).length === 0);
  const emptyText = noCandidates ? t("reference_picker_no_candidates") : t("reference_picker_empty");

  return (
    <Popover
      open={open}
      onOpenChange={(next) => {
        if (!next) onClose();
      }}
    >
      {/* 焦点留在编辑器里：方向键与回车由上面的键盘监听处理，选项用 aria-activedescendant 指向。 */}
      <PopoverContent
        anchor={anchorElement ?? undefined}
        align="start"
        sideOffset={4}
        initialFocus={false}
        finalFocus={false}
        className="w-64"
      >
        <div role="tablist" aria-label={t("reference_picker_title")} className="flex border-b border-border">
          {tabs.map((tab) => {
            const count =
              tab === "all"
                ? kinds.reduce((sum, kind) => sum + totalsByKind[kind], 0)
                : totalsByKind[tab];
            const isActive = tab === activeTab;
            return (
              <button
                key={tab}
                type="button"
                role="tab"
                aria-selected={isActive}
                onMouseDown={(e) => e.preventDefault()}
                onClick={() => setActiveTab(tab)}
                className={`focus-ring flex items-center gap-1 border-b-2 px-1.5 py-1.5 text-xs transition-colors duration-fast ${
                  isActive
                    ? "border-primary font-medium text-foreground"
                    : "border-transparent text-muted-foreground hover:text-foreground"
                }`}
              >
                <span>{t(`reference_picker_tab_${tab}`)}</span>
                <span className="tabular-nums">{count}</span>
              </button>
            );
          })}
        </div>
        <div
          id={listboxId ?? MENTION_PICKER_DEFAULT_ID}
          role="listbox"
          aria-label={t("reference_picker_title")}
          className={`relative max-h-60 overflow-y-auto ${className ?? ""}`}
        >
          {empty && <p className="px-3 py-4 text-center text-xs text-muted-foreground">{emptyText}</p>}
          {!empty &&
            kinds.map((kind) => {
              const items = filtered[kind];
              if (items.length === 0) return null;
              const palette = assetColor(kind);
              // 「全部」页签保留分组小标题；单页签时去掉，避免视觉重复。
              const showGroupHeader = activeTab === "all";
              const groupLabel = t(`reference_picker_group_${kind}`);
              return (
                <div key={kind} role="group" aria-label={groupLabel}>
                  {showGroupHeader && (
                    <div
                      aria-hidden="true"
                      data-testid={`mention-picker-group-${kind}`}
                      className={`px-2 py-1 text-xs font-medium ${palette.textClass}`}
                    >
                      {groupLabel}
                    </div>
                  )}
                  {items.map((item) => {
                    const globalIndex = indexByKey.get(`${kind}:${item.name}`) ?? -1;
                    const active = globalIndex === clampedActive;
                    const thumbUrl =
                      item.imagePath && projectName
                        ? API.getFileUrl(projectName, item.imagePath, fingerprints[item.imagePath] ?? null, {
                            width: 160,
                          })
                        : null;
                    return (
                      <button
                        key={`${kind}:${item.name}`}
                        id={optionId(kind, item.name)}
                        type="button"
                        role="option"
                        aria-selected={active}
                        tabIndex={-1}
                        onMouseMove={(e) => {
                          lastPointerXY.current = { x: e.clientX, y: e.clientY };
                          if (clampedActive !== globalIndex) setActiveIndex(globalIndex);
                        }}
                        onMouseEnter={(e) => {
                          const last = lastPointerXY.current;
                          if (last.x === e.clientX && last.y === e.clientY) return;
                          lastPointerXY.current = { x: e.clientX, y: e.clientY };
                          if (clampedActive !== globalIndex) setActiveIndex(globalIndex);
                        }}
                        onMouseDown={(e) => e.preventDefault()}
                        onClick={() => onSelect({ type: kind, name: item.name })}
                        className={`flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-sm transition-colors duration-fast ${
                          active ? "bg-primary/15 text-foreground" : "text-subtle-foreground hover:bg-muted"
                        }`}
                      >
                        {thumbUrl ? (
                          <img
                            src={thumbUrl}
                            alt=""
                            aria-hidden="true"
                            loading="lazy"
                            decoding="async"
                            className={`size-7 shrink-0 rounded-sm border object-cover ${palette.borderClass}`}
                          />
                        ) : (
                          <span
                            aria-hidden="true"
                            className={`flex size-7 shrink-0 items-center justify-center rounded-sm border ${palette.bgClass} ${palette.borderClass}`}
                          >
                            <span className={`size-2 rounded-full ${palette.dotClass}`} />
                          </span>
                        )}
                        <span className="truncate">{formatReferenceName(item.name)}</span>
                        {splitDerivativeReference(item.name)[1] && (
                          <span className="shrink-0 rounded-sm bg-primary/15 px-1 py-0.5 text-xs font-medium text-primary">
                            {t("reference_picker_derivative_tag")}
                          </span>
                        )}
                      </button>
                    );
                  })}
                </div>
              );
            })}
        </div>
      </PopoverContent>
    </Popover>
  );
}
