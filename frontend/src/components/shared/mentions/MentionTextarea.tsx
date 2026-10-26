import { Fragment, useCallback, useId, useMemo, useRef, useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { assetColor } from "@/components/canvas/reference/asset-colors";
import { Textarea } from "@/components/ui/textarea";
import { MENTION_RE, storyboardMentionToken } from "@/utils/reference-mentions";
import { tokenizePrompt, type MentionLookup, type Token } from "@/utils/script-highlight";
import type { AssetKind } from "@/types/reference-video";
import { MentionPicker, type MentionCandidateGroups } from "./MentionPicker";

/**
 * 编辑「分镜 / 视频提示词」里的 `名字@[登记名]` 引用。
 *
 * 正文仍是一段纯文本（后端按 `@[…]` 解析），输入框因此保持原样；`Textarea` 的文字透明，
 * 后面叠一层字符级镜像 `<pre>`：镜像只画样式、不接收指针事件，光标与选区由输入框负责。
 * 字符逐字对齐，所以镜像能把每个 token 画成链接样式（按资产类型着色 + 虚线下划线），
 * 未登记的引用画成红波浪线。
 *
 * 输入 `@` 时在光标处弹出候选。候选来自本分镜的出场资产清单——token 只有落在清单内，
 * 出图时才能指认到对应的参考图；清单外的已登记资产不该被写成引用。
 */
export interface MentionTextareaProps {
  value: string;
  onChange: (next: string) => void;
  /** 资产名 → 类型，决定 token 配色；未命中按「未登记」显示。 */
  lookup: MentionLookup;
  /** `@` 之后列出的候选，按资产类型分组。 */
  candidates: MentionCandidateGroups;
  /** 候选只保留这些类型；缺省四类全开。 */
  kinds?: AssetKind[];
  /** 拼候选缩略图 URL 用；缺省只显示类型圆点。 */
  projectName?: string;
  /**
   * 选中候选后写进正文的形态：`prefixed` 写标准分镜形态 `名字@[登记名]`（分镜 / 视频提示词）；
   * `wrapped` 只写 `@[登记名]`（参考素材逐格提示词——那里裸名字会被自动链接，重复名字会出两张图）。
   */
  insertForm?: "prefixed" | "wrapped";
  ariaLabel: string;
  placeholder?: string;
  id?: string;
  readOnly?: boolean;
  disabled?: boolean;
  maxLength?: number;
  /** 只用于外层尺寸类（如 `min-h-31`）。 */
  className?: string;
}

/** 触发符后允许参与过滤的字符：词字符、CJK，以及衍生名分隔符与常见间隔号。 */
const TRIGGER_QUERY_RE = /^[\w\u4e00-\u9fff/·・]*$/;

/** 引用 token 的链接样式：只加颜色、圆角与虚线下划线，不改变盒模型宽度（镜像才能逐字对齐）。 */
const MENTION_SPAN_CLASS = "rounded-sm underline decoration-dotted underline-offset-2";

/** 正在输入的这条引用：`@` 在原文里的下标、它后面已输入的过滤词，以及是否已写成 `@[`。 */
interface Trigger {
  start: number;
  query: string;
  wrapped: boolean;
}

/**
 * 从光标往左找触发符。跨过句读（右括号、空白、换行）就不算同一条引用：用户放弃引用后
 * 再移动光标，悬挂的 `@` 不会把候选列表重新拉出来。
 */
function findTrigger(value: string, cursor: number): Trigger | null {
  for (let i = cursor - 1; i >= 0; i -= 1) {
    const ch = value[i];
    if (ch === "@") {
      // 与 MENTION_RE 的 `(?<!\w)` 对齐：@ 左侧是 ASCII 词字符时是邮件 / id 残片，不是引用。
      if (/\w/.test(i === 0 ? "" : value[i - 1])) return null;
      const raw = value.slice(i + 1, cursor);
      if (raw.startsWith("[")) {
        // 包裹形态：括号内允许任意标点（资产名可以带括号与间隔号），闭合即已写完。
        return raw.includes("]") ? null : { start: i, query: raw.slice(1), wrapped: true };
      }
      return TRIGGER_QUERY_RE.test(raw) ? { start: i, query: raw, wrapped: false } : null;
    }
    if (ch === "]" || /\s/.test(ch)) return null;
  }
  return null;
}

/**
 * 把 token 串渲染成镜像层节点；`caretOffset` 命中某个 token 内部或边界时在那里切一刀，
 * 插入零尺寸的光标锚点，供候选弹层定位（弹层经 Portal 渲染，锚点对齐真实光标位置）。
 */
function renderTokens(
  tokens: Token[],
  caretOffset: number | null,
  setAnchorEl: (el: HTMLSpanElement | null) => void,
  voiceoverLabel: string,
): ReactNode {
  const out: ReactNode[] = [];
  let acc = 0;
  const anchorEl =
    caretOffset === null ? null : (
      <span
        key="__caret_anchor__"
        ref={setAnchorEl}
        aria-hidden="true"
        className="inline-block h-[1em] w-0 align-baseline"
      />
    );

  const renderPiece = (tk: Token, sliceText: string, key: string): ReactNode => {
    if (sliceText.length === 0) return null;
    if (tk.kind === "mention") {
      const palette = assetColor(tk.assetKind);
      const unknown = tk.assetKind === "unknown";
      return (
        <span
          key={key}
          className={`${MENTION_SPAN_CLASS} ${palette.textClass} ${unknown ? "decoration-wavy" : ""}`}
        >
          {sliceText}
        </span>
      );
    }
    if (tk.kind === "speech") {
      // 与预览（ScriptHighlight）同一套着色：底色标出「这段被认成台词了」，说话人配色跟
      // 资产档位走（画外音无说话人，恒是 unknown 档）。原文逐字保留，只加颜色与 title。
      const palette = assetColor(tk.speakerKind);
      return (
        <span
          key={key}
          className={`rounded-sm bg-foreground/5 ${tk.speaker ? palette.textClass : ""}`}
          title={tk.speaker || voiceoverLabel}
        >
          {sliceText}
        </span>
      );
    }
    return <span key={key}>{sliceText}</span>;
  };

  let inserted = false;
  tokens.forEach((tk, i) => {
    const nextAcc = acc + tk.text.length;
    if (!inserted && caretOffset !== null && caretOffset >= acc && caretOffset <= nextAcc) {
      const local = caretOffset - acc;
      out.push(<Fragment key={`pre-${i}`}>{renderPiece(tk, tk.text.slice(0, local), `pre-${i}`)}</Fragment>);
      if (anchorEl) out.push(anchorEl);
      out.push(<Fragment key={`post-${i}`}>{renderPiece(tk, tk.text.slice(local), `post-${i}`)}</Fragment>);
      inserted = true;
    } else {
      out.push(<Fragment key={`t-${i}`}>{renderPiece(tk, tk.text, `t-${i}`)}</Fragment>);
    }
    acc = nextAcc;
  });
  if (!inserted && anchorEl && caretOffset !== null && caretOffset >= acc) {
    out.push(anchorEl);
  }
  return out;
}

export function MentionTextarea({
  value,
  onChange,
  lookup,
  candidates,
  kinds,
  projectName,
  insertForm = "prefixed",
  ariaLabel,
  placeholder,
  id,
  readOnly,
  disabled,
  maxLength,
  className,
}: MentionTextareaProps) {
  const { t } = useTranslation("dashboard");
  const taRef = useRef<HTMLTextAreaElement>(null);
  const preRef = useRef<HTMLPreElement>(null);
  const listboxId = useId();
  // 锚点用 state 而非 ref：弹层要等 span 真正挂载后才能拿它定位（floating-ui 的 reference）。
  const [anchorEl, setAnchorEl] = useState<HTMLSpanElement | null>(null);
  const [trigger, setTrigger] = useState<Trigger | null>(null);
  const [activeOptionId, setActiveOptionId] = useState<string | null>(null);

  const voiceoverLabel = t("script_highlight_voiceover");
  const tokens = useMemo(() => tokenizePrompt(value, lookup), [value, lookup]);
  // 弹层关闭是常态：这时镜像节点缓存一层，父组件的其它 state 变化不必重跑渲染。
  const staticNodes = useMemo(
    () => renderTokens(tokens, null, () => {}, voiceoverLabel),
    [tokens, voiceoverLabel],
  );

  const syncTrigger = useCallback((el: HTMLTextAreaElement) => {
    setTrigger(el.readOnly || el.disabled ? null : findTrigger(el.value, el.selectionStart ?? el.value.length));
  }, []);

  const handleChange = (event: React.ChangeEvent<HTMLTextAreaElement>) => {
    onChange(event.target.value);
    syncTrigger(event.currentTarget);
  };

  const handleCursorMove = (event: React.SyntheticEvent<HTMLTextAreaElement>) => {
    syncTrigger(event.currentTarget);
  };

  const handleBlur = useCallback(() => {
    // 候选按钮在 mousedown 上 preventDefault，点击不会让输入框失焦，所以这里只处理真正的离开。
    setTrigger(null);
    setActiveOptionId(null);
  }, []);

  // Backspace 两次删除：光标紧挨在一个完整引用的末尾且无选区时，第一次退格只选中整条
  // 引用（让人看见高亮），第二次由浏览器默认的 delete-selection 删掉整条。
  const handleKeyDown = useCallback((event: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key !== "Backspace" || event.nativeEvent.isComposing) return;
    const ta = event.currentTarget;
    const start = ta.selectionStart ?? 0;
    if (start !== (ta.selectionEnd ?? 0)) return;
    const scanFrom = Math.max(0, start - 96);
    for (const match of ta.value.slice(scanFrom, start).matchAll(MENTION_RE)) {
      const matchStart = scanFrom + (match.index ?? 0);
      if (matchStart + match[0].length !== start) continue;
      // 标准形态是 `名字@[登记名]`：裸名字也在同一条引用里，一起选中才删得干净。
      const bare = (match[1] ?? match[2] ?? "").trim();
      const from = bare && ta.value.slice(0, matchStart).endsWith(bare) ? matchStart - bare.length : matchStart;
      event.preventDefault();
      ta.setSelectionRange(from, start);
      return;
    }
  }, []);

  const handleSelect = useCallback(
    (ref: { type: AssetKind; name: string }) => {
      const ta = taRef.current;
      const at = trigger;
      setTrigger(null);
      setActiveOptionId(null);
      if (!ta || at === null) return;
      const cursor = ta.selectionStart ?? value.length;
      const rawBefore = value.slice(0, at.start);
      const after = value.slice(cursor);
      const typedName = rawBefore.endsWith(ref.name);
      // 只有 `prefixed` 形态留着 `@` 前那个裸名字；其余形态只写 `@[登记名]`，去掉刚输入的裸名字。
      const before = insertForm === "prefixed" ? rawBefore : typedName ? rawBefore.slice(0, -ref.name.length) : rawBefore;
      const insert =
        insertForm === "wrapped" || at.wrapped || typedName
          ? `@[${ref.name}] `
          : `${storyboardMentionToken(ref.name)} `;
      onChange(before + insert + after);
      requestAnimationFrame(() => {
        ta.focus();
        const pos = before.length + insert.length;
        ta.setSelectionRange(pos, pos);
      });
    },
    [insertForm, onChange, trigger, value],
  );

  // 输入框可能带 max-h 在框内滚动；镜像层跟着滚，否则可见文字与光标错位。
  const handleScroll = () => {
    const ta = taRef.current;
    const pre = preRef.current;
    if (!ta || !pre) return;
    pre.scrollTop = ta.scrollTop;
    pre.scrollLeft = ta.scrollLeft;
  };

  const pickerOpen = trigger !== null;

  return (
    <div className="relative">
      <pre
        ref={preRef}
        aria-hidden="true"
        data-overflow-ok="与输入框同步滚动的着色镜像层"
        // font-sans 要写死：preflight 把 <pre> 换成等宽字体，而输入框继承正文比例字体，
        // 字体不一致时镜像与光标的字符宽度就对不上了。
        className="pointer-events-none absolute inset-px m-0 overflow-hidden whitespace-pre-wrap break-words px-2.5 py-2 font-sans text-sm"
      >
        {pickerOpen ? renderTokens(tokens, trigger.start, setAnchorEl, voiceoverLabel) : staticNodes}
        {/* 末尾换行时给出一个零宽字符，最后一行空行才占得到高度。 */}
        {value.endsWith("\n") ? "\u200b" : null}
      </pre>

      <Textarea
        ref={taRef}
        id={id}
        variant="overlay"
        aria-label={ariaLabel}
        aria-autocomplete="list"
        aria-expanded={pickerOpen}
        aria-haspopup="listbox"
        aria-controls={pickerOpen ? listboxId : undefined}
        aria-activedescendant={pickerOpen && activeOptionId ? activeOptionId : undefined}
        value={value}
        onChange={handleChange}
        onKeyDown={handleKeyDown}
        onKeyUp={handleCursorMove}
        onClick={handleCursorMove}
        onSelect={handleCursorMove}
        onBlur={handleBlur}
        onScroll={handleScroll}
        readOnly={readOnly}
        disabled={disabled}
        maxLength={maxLength}
        placeholder={placeholder}
        spellCheck={false}
        className={className}
      />

      {pickerOpen && anchorEl && (
        <MentionPicker
          open
          query={trigger.query}
          candidates={candidates}
          kinds={kinds}
          projectName={projectName}
          anchorElement={anchorEl}
          listboxId={listboxId}
          onActiveChange={setActiveOptionId}
          onSelect={handleSelect}
          onClose={() => {
            setTrigger(null);
            setActiveOptionId(null);
          }}
        />
      )}
    </div>
  );
}
