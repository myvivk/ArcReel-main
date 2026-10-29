import { createContext, useCallback, useContext, useMemo, useState, useSyncExternalStore, type ReactNode } from "react";
import { createPortal } from "react-dom";

/** 动作落不到具体步骤时（如该步骤在当前项目里不存在）的兜底插槽：制作进度弹层顶部。 */
export const FALLBACK_ACTION_SLOT = "__fallback__";

// 页头常驻插槽的节点：页头先挂载，视图随后按这个节点把动作 portal 过去。
// 用模块级订阅而不是 context，避免热更新/多副本时 context 失配导致动作退化成整行内联。
let headerSlotNode: HTMLElement | null = null;
const headerSlotListeners = new Set<() => void>();

function subscribeHeaderSlot(listener: () => void): () => void {
  headerSlotListeners.add(listener);
  return () => {
    headerSlotListeners.delete(listener);
  };
}

function readHeaderSlot(): HTMLElement | null {
  return headerSlotNode;
}

function setHeaderSlotNode(node: HTMLElement | null): void {
  if (headerSlotNode === node) return;
  headerSlotNode = node;
  for (const listener of headerSlotListeners) listener();
}

/** 集页持有的一份动作插槽表：步骤 key → 弹层里那一行挂上的容器。 */
export interface EpisodeActionSlots {
  register: (key: string, node: HTMLElement | null) => void;
  slots: ReadonlyMap<string, HTMLElement>;
}

const EpisodeHeaderSlotContext = createContext<EpisodeActionSlots | null>(null);

/**
 * 集页的动作插槽注册表：视图按步骤 key 把动作投放进去，弹层里的步骤行注册自己的容器。
 * `undefined` 表示不在集页里（如画布单独渲染的测试），动作就地渲染。
 */
export function useEpisodeActionSlots(): EpisodeActionSlots {
  const [slots, setSlots] = useState<ReadonlyMap<string, HTMLElement>>(() => new Map());
  const register = useCallback((key: string, node: HTMLElement | null) => {
    setSlots((current) => {
      if (node) {
        if (current.get(key) === node) return current;
        const next = new Map(current);
        next.set(key, node);
        return next;
      }
      if (!current.has(key)) return current;
      const next = new Map(current);
      next.delete(key);
      return next;
    });
  }, []);
  return useMemo(() => ({ register, slots }), [register, slots]);
}

export const EpisodeHeaderSlotProvider = EpisodeHeaderSlotContext.Provider;

/** 页头右上角的容器：登记节点，视图动作经 {@link EpisodeHeaderActions} portal 到这里。 */
export function EpisodeHeaderActionSlot({ className }: { className?: string }) {
  return <div ref={setHeaderSlotNode} className={className} />;
}

/** 弹层里的步骤行用它注册自己的容器。 */
export function useEpisodeActionSlotRegister(): EpisodeActionSlots["register"] | undefined {
  return useContext(EpisodeHeaderSlotContext)?.register;
}

/**
 * 各视图往集页「制作进度」弹层里放当前视图的动作（批量补齐、编写提示词、出片等）。
 * `target` 是步骤 key（见 step-list 的 StepRowKey），动作渲染到弹层里对应步骤行内；
 * 那一行不存在时落到弹层顶部。子元素经 portal 渲染，状态与事件仍留在调用方的组件树里。
 */
export function EpisodeHeaderActions({
  target,
  children,
}: {
  /** 步骤 key；数组表示按顺序找第一个存在的步骤行。 */
  target?: string | string[];
  children: ReactNode;
}) {
  const registry = useContext(EpisodeHeaderSlotContext);
  const headerNode = useSyncExternalStore(subscribeHeaderSlot, readHeaderSlot, readHeaderSlot);

  if (headerNode) return createPortal(children, headerNode);
  // 集页里页头插槽还没挂上时先不渲染，避免动作以整行宽度闪现在正文里。
  if (registry) {
    const keys = [
      ...(target === undefined ? [] : Array.isArray(target) ? target : [target]),
      FALLBACK_ACTION_SLOT,
    ];
    const node = keys.map((key) => registry.slots.get(key)).find((element) => element !== undefined) ?? null;
    return node ? createPortal(children, node) : null;
  }
  // 不在集页里（如画布单独渲染的测试）：就地渲染。
  return <>{children}</>;
}
