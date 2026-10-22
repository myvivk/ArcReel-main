import { useEffect, useId, useMemo, useRef, useState } from "react";
import { API } from "@/api";
import { errMsg } from "@/utils/async";
import { Autocomplete } from "@base-ui/react/autocomplete";
import { useTranslation } from "react-i18next";
import { Bot, ChevronDown, Sparkles, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { InputGroup, InputGroupAddon, InputGroupButton, InputGroupInput } from "@/components/ui/input-group";
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { useEpisodeLedger } from "@/hooks/useEpisodeLedger";
import { createBlankEpisodes } from "@/actions/shortDrama";
import { useAppStore } from "@/stores/app-store";
import { useAssistantStore } from "@/stores/assistant-store";
import { useProjectsStore } from "@/stores/projects-store";
import { useShortDramaGenerateStore, type ShortDramaGenerateOpenRequest } from "@/stores/short-drama-generate-store";
import { episodeDisplayName } from "@/utils/episode-display";
import { trailingBlankEpisodes } from "@/utils/episode-blank";
import { refreshAfterWrite } from "./refreshAfterWrite";

export type ShortDramaFormat = "standard" | "long" | "manju";

export interface OriginalScriptBrief {
  /** 目标集号列表：单集导入时长度为 1；连写多集时为各集号。 */
  episodes: number[];
  /** 全剧规划总集数：供 Agent 铺排四幕/付费墙与收尾位置；连写未达总集数时保持断章续写，达到时收尾闭环。 */
  totalEpisodes: number;
  genre: string;
  brief: string;
  tone: string;
  /** 篇幅档位：标准 / 长档 / 漫剧（short-drama-factory 的制式）。 */
  format: ShortDramaFormat;
}

/** 各档位篇幅说明，写入交给 Agent 的指令。 */
const FORMAT_LENGTH: Record<ShortDramaFormat, string> = {
  standard: "标准档 90~120 秒",
  long: "长档 165~195 秒（约 3 分钟，双回合 + 中段小钩）",
  manju: "漫剧档 60~90 秒",
};

/**
 * 把创意简报固化成一段交给 Agent 的指令：让内嵌 Agent 用 short-drama-factory 技能走工厂独立链路
 * 原创剧本——所有正文经 `generate_short_drama_factory` 交给文本模型写作与优化，通过
 * `ingest_short_drama_artifacts` 原样落盘为七类工厂工件。纯函数，便于测试。
 * 不把旧脚本生产入口（`import_short_drama_script` / `import_short_drama_series` /
 * `generate_episode_script`）作为交付手段。目标集由前端在提交时透明建好，用户无感。
 */
export function buildOriginalShortDramaPrompt(b: OriginalScriptBrief): string {
  const episodes = b.episodes;
  const single = episodes.length === 1;
  const sortedEpisodes = [...episodes].sort((a, c) => a - c);
  const toneLine = b.tone.trim() ? b.tone.trim() : "（按题材默认基调）";
  const rangeLabel = single
    ? `第 ${episodes[0]} 集`
    : `第 ${sortedEpisodes.join("、")} 集（共 ${episodes.length} 集）`;
  const lines = [
    "请使用 short-drama-factory 技能，按工厂独立链路原创短剧剧本并落盘到当前项目（剧情演绎 drama 模式）。严禁绕回旧脚本生产入口。",
    "",
    "【创意简报】",
    `- 题材 / 类型：${b.genre.trim()}`,
    `- 核心梗概：${b.brief.trim()}`,
    `- 风格基调：${toneLine}`,
    `- 篇幅：${FORMAT_LENGTH[b.format]}`,
    `- 本次目标集：${rangeLabel}`,
  ];
  {
    const total = b.totalEpisodes;
    const last = Math.max(...episodes);
    const reachesEnd = last >= total;
    lines.push(
      "",
      "【全剧规划】",
      `- 全剧规划总集数为 ${total} 集：据此重新规划四幕与付费墙位置，不要套用默认的 60/80/100 长档结构。`,
      `- 本次写作范围：${rangeLabel}。`,
      reachesEnd
        ? "- 本批次写到全剧结尾：最后一集必须收尾闭环、兑现情绪契约，主线不留未收束的悬钩。"
        : "- 本批次未达全剧结尾：每集保持断章钩子、留续写空间，不要在本批次内提前收尾或闭环主线。",
    );
  }
  lines.push(
    "",
    "【工厂链路要求（必须遵守，不得阉割）】",
    "1. 先读取工厂 references / templates / examples 和 source/short_drama/ 已有的人物圣经、全剧大纲、台账、分集规划及此前集正文，续写沿用已有设定。所有工厂正文（人物圣经 / 全剧大纲 / 连续性台账 / 分集规划 / 集正文 / 机检报告 / 合规报告）一律由文本模型产出：按工件类型组装 prompt，调用 generate_short_drama_factory 生成 Markdown 原文；Agent 本人不写正文、不转写为 ArcReel JSON。",
    "2. 每集必须走完整质量闭环：初稿 → AI 医生精修（dialogue-doctor-anti-ai）→ 规则机检（validate_episode.py，长档 / 漫剧按对应档位）→ 失败项回喂文本模型返修 → 更新 ledger.md → 阶段性 validate_series.py → 合规检查（screenplay-compliance-rules.md，红线问题返修）。机检 / 合规通过的报告随工件保存，不得裸交失败件。",
    "3. 每个单元或批次通过后立即用唯一交稿入口 ingest_short_drama_artifacts 把七类 Markdown 工件原样写入 source/short_drama/，不得等全剧写完：系列级工件（character-bible.md / series-outline.md / ledger.md）随首批提交并可跨批复用，台账有变化必须提交新版；每集必须同批包含分集规划、集正文、机检报告与合规报告。报告基于真实检查结果生成，以“结果: PASSED / FAILED”明确结论。传参为各工件的相对路径与 UTF-8 原文 Base64，不改写内容、不抽取到 story_plan、不登记资产、不调用 generate_episode_script。",
    "4. 禁止把 import_short_drama_script / import_short_drama_series（旧导入会把工厂 Markdown 重写为原生剧本 JSON）或 generate_episode_script（旧视觉提示词生成）作为交付手段；视觉层留待后续独立制作步骤处理。",
    `5. 目标集 ${rangeLabel} 已由前端建好；工件路径用零填充集号（如 episodes/episode_${String(episodes[0]).padStart(2, "0")}.md）。`,
  );
  return lines.join("\n");
}

/**
 * 预设输入框：下拉给常用预设，也允许填写预设之外的任意值。
 * 预设存于 i18n（逗号分隔），由 optionsKey 指定。
 * 用 Base UI 的 Autocomplete 而不是 Combobox：后者只接受候选项（同 ModelIdField 的取舍）。
 */
function SuggestField({
  id,
  value,
  onChange,
  placeholder,
  optionsKey,
}: {
  id: string;
  value: string;
  onChange: (value: string) => void;
  placeholder?: string;
  /** 预设选项的 i18n key，值为逗号分隔列表。 */
  optionsKey: string;
}) {
  const { t } = useTranslation("dashboard");
  const options = useMemo(
    () =>
      t(optionsKey)
        .split(",")
        .map((option) => option.trim())
        .filter(Boolean),
    [t, optionsKey],
  );
  return (
    <Autocomplete.Root items={options} value={value} onValueChange={onChange} openOnInputClick>
      <InputGroup>
        <Autocomplete.Input
          id={id}
          placeholder={placeholder}
          autoComplete="off"
          spellCheck={false}
          render={<InputGroupInput />}
        />
        <InputGroupAddon align="inline-end">
          {value && (
            <Autocomplete.Clear render={<InputGroupButton size="icon-xs" />}>
              <X aria-hidden />
            </Autocomplete.Clear>
          )}
          <Autocomplete.Trigger render={<InputGroupButton size="icon-xs" />}>
            <ChevronDown aria-hidden />
          </Autocomplete.Trigger>
        </InputGroupAddon>
      </InputGroup>
      <Autocomplete.Portal>
        <Autocomplete.Positioner sideOffset={6} align="start" className="isolate z-overlay">
          <Autocomplete.Popup className="max-h-(--available-height) w-(--anchor-width) overflow-hidden rounded-lg bg-popover text-popover-foreground shadow-overlay ring-1 ring-foreground/10 data-closed:animate-out data-closed:fade-out-0 data-open:animate-in data-open:fade-in-0">
            <Autocomplete.List className="relative max-h-72 overflow-y-auto overscroll-contain p-1 data-empty:p-0">
              {(item: string) => (
                <Autocomplete.Item
                  key={item}
                  value={item}
                  className="flex cursor-default items-center rounded-md px-2 py-1 text-sm outline-hidden select-none data-highlighted:bg-accent data-highlighted:text-accent-foreground"
                >
                  {item}
                </Autocomplete.Item>
              )}
            </Autocomplete.List>
          </Autocomplete.Popup>
        </Autocomplete.Positioner>
      </Autocomplete.Portal>
    </Autocomplete.Root>
  );
}

/** 篇幅档位下拉：标准 / 长档 / 漫剧（short-drama-factory 的三档制式）。 */
const FORMAT_ITEMS: { value: ShortDramaFormat; labelKey: string; descKey: string }[] = [
  { value: "standard", labelKey: "short_drama_format_standard", descKey: "short_drama_format_standard_desc" },
  { value: "long", labelKey: "short_drama_format_long", descKey: "short_drama_format_long_desc" },
  { value: "manju", labelKey: "short_drama_format_manju", descKey: "short_drama_format_manju_desc" },
];

function FormatSelect({
  id,
  value,
  onChange,
  disabled,
}: {
  id: string;
  value: ShortDramaFormat;
  onChange: (value: ShortDramaFormat) => void;
  disabled?: boolean;
}) {
  const { t } = useTranslation("dashboard");
  return (
    <Select
      items={FORMAT_ITEMS.map((item) => ({ value: item.value, label: t(item.labelKey) }))}
      value={value}
      disabled={disabled}
      onValueChange={(next) => {
        if (next !== null) onChange(next);
      }}
    >
      <SelectTrigger id={id} size="sm" aria-label={t("short_drama_generate_format_label")} className="w-full @sm/canvas:w-64">
        <SelectValue />
      </SelectTrigger>
      <SelectContent alignItemWithTrigger={false} align="start" className="w-auto max-w-xs">
        {FORMAT_ITEMS.map((item) => (
          <SelectItem key={item.value} value={item.value}>
            <span className="flex flex-col gap-0.5 whitespace-normal">
              <span>{t(item.labelKey)}</span>
              <span className="text-xs text-muted-foreground">{t(item.descKey)}</span>
            </span>
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  );
}

/** 「AI 原创剧本」弹窗宿主：按 {@link useShortDramaGenerateStore} 的请求打开。 */
export function ShortDramaGenerateHost({ projectName, episode }: { projectName: string; episode: number }) {
  const request = useShortDramaGenerateStore((s) => s.request);
  const close = useShortDramaGenerateStore((s) => s.close);
  useEffect(() => close, [close, projectName, episode]);
  if (!request || request.projectName !== projectName) return null;
  return <ShortDramaGenerateDialog request={request} onClose={close} />;
}

interface DialogProps {
  request: ShortDramaGenerateOpenRequest;
  onClose: () => void;
}

/**
 * 剧情演绎「AI 原创剧本」：填题材与创意简报，交给内嵌 Agent 用 short-drama-factory 原创并自动导入。
 * - 集页入口（request.episode > 0）：选定一个已存在的目标集，单集导入。
 * - 空项目入口（request.episode === 0，创建模式）：不显示目标集号；提交时透明建好对应集数再导入，用户无感；
 *   需要多集时由「连写集数」控制。
 * 不需要先准备小说或源文——故事由 Agent 按简报原创；视觉层留空待后续生成。
 */
function ShortDramaGenerateDialog({ request, onClose }: DialogProps) {
  const { t } = useTranslation("dashboard");
  const ledger = useEpisodeLedger();
  const episodeMetas = useProjectsStore((s) => s.currentProjectData?.episodes);
  // 项目级「全剧规划总集数」：首次生成时落库，后续续写读回，避免每次打开弹窗都从 1 开始。
  const projectTotal = useProjectsStore((s) => s.currentProjectData?.total_episodes ?? null);
  // 创建模式下优先填末尾已有的空白集，避免留下永久空集让剧集从第 2 集才开始。
  const reusable = useMemo(() => trailingBlankEpisodes(episodeMetas), [episodeMetas]);
  const fieldId = useId();
  const createMode = request.episode === 0;
  const [episode, setEpisode] = useState<number>(request.episode);
  const [genre, setGenre] = useState("");
  const [brief, setBrief] = useState("");
  const [tone, setTone] = useState("");
  const [count, setCount] = useState(1);
  const [total, setTotal] = useState<number>(projectTotal && projectTotal >= 1 ? projectTotal : 1);
  const [format, setFormat] = useState<ShortDramaFormat>("standard");
  const [submitting, setSubmitting] = useState(false);

  // 续写约束：本次续写后累计集数不得超过项目规划总集数。
  // 「已存在集数」取当前最大集号（集号连续时即已有集数）；有末尾空白集时从空白集开始续写。
  const maxEpisode = ledger.reduce((max, entry) => Math.max(max, entry.episode), 0);
  const reusedCount = createMode ? Math.min(reusable.length, Number(count)) : 0;
  const newEpisodeCount = Math.max(0, Number(count) - reusedCount);
  const startEpisode = createMode ? reusable[0] ?? maxEpisode + 1 : Number(episode);
  const projectedTotal = maxEpisode + newEpisodeCount;
  const overflow = createMode && Number.isFinite(total) && total >= 1 && projectedTotal > total;

  const canSubmit = genre.trim().length > 0 && count >= 1 && !overflow;

  const storyController = useRef(new AbortController());
  useEffect(() => {
    storyController.current = new AbortController();
    return () => storyController.current.abort();
  }, []);
  const handOff = async () => {
    if (!canSubmit || submitting) return;
    setSubmitting(true);
    const signal = storyController.current.signal;
    // 创建模式：优先复用末尾的空白集，不够再补建；上下文取这批目标集之前的内容，
    // 保证连写从正确的位置接着写（填第 1 集时上下文为空，等于从头起故事）。
    const reused = createMode ? reusable.slice(0, Number(count)) : [];
    const contextEpisode = createMode
      ? reused[0] ?? Math.max(0, ...ledger.map((entry) => entry.episode)) + 1
      : Number(episode);
    let storyContext: string;
    try {
      // 先读取上下文再创建空白集，失败重试不会留下重复集。
      const result = await API.getStoryContext(request.projectName, contextEpisode, {
        signal,
      });
      signal.throwIfAborted();
      storyContext = result.context;
    } catch (err) {
      if (!signal.aborted) {
        useAppStore.getState().pushToast(errMsg(err), "error");
        setSubmitting(false);
      }
      return;
    }
    // 总集数与建集都成功并刷新后才把指令交给 Agent；失败时保留简报供重试。
    if (createMode && total !== projectTotal) {
      try {
        await API.updateProject(request.projectName, { total_episodes: total }, { signal });
        const refreshed = await refreshAfterWrite(request.projectName, t);
        signal.throwIfAborted();
        if (refreshed !== "success") {
          setSubmitting(false);
          return;
        }
      } catch (err) {
        if (!signal.aborted) {
          useAppStore.getState().pushToast(errMsg(err), "error");
          setSubmitting(false);
        }
        return;
      }
    }
    // 创建模式下补足目标集；import 工具要求目标集已存在。
    let episodes: number[];
    if (createMode) {
      const remaining = Number(count) - reused.length;
      if (remaining > 0) {
        try {
          episodes = [...reused, ...(await createBlankEpisodes(request.projectName, remaining, { signal }))];
        } catch {
          // 部分建集可能已提交：补拉后下次复用这些空集，避免重复建集。
          await refreshAfterWrite(request.projectName, t);
          if (!signal.aborted) setSubmitting(false);
          return;
        }
        const refreshed = await refreshAfterWrite(request.projectName, t);
        if (signal.aborted) return;
        if (refreshed !== "success") {
          setSubmitting(false);
          return;
        }
      } else {
        episodes = reused;
      }
    } else {
      episodes = [Number(episode)];
    }
    if (signal.aborted) return;
    const prompt = buildOriginalShortDramaPrompt({
      episodes, genre, brief, tone, format,
      totalEpisodes: createMode ? total : Math.max(projectTotal ?? maxEpisode, ...episodes),
    }) +
      (storyContext ? "\n\n" + storyContext : "");
    useAssistantStore.getState().setInput(prompt);
    useAppStore.getState().setAssistantPanelOpen(true);
    setSubmitting(false);
    onClose();
  };

  return (
    <Dialog
      open
      onOpenChange={(next) => {
        if (!next) onClose();
      }}
    >
      <DialogContent showCloseButton>
        <DialogHeader>
          <DialogTitle>{t("short_drama_generate_title")}</DialogTitle>
          <DialogDescription>{t("short_drama_generate_desc")}</DialogDescription>
        </DialogHeader>
        <DialogBody>
          <div className="flex flex-col gap-4">
            <div className="flex flex-col gap-3 @sm/canvas:flex-row @sm/canvas:gap-4">
              {createMode ? (
                <>
                  <div className="flex flex-col gap-1.5 @sm/canvas:w-1/3">
                    <Label htmlFor={`${fieldId}-genre`}>{t("short_drama_generate_genre_label")}</Label>
                    <SuggestField
                      id={`${fieldId}-genre`}
                      value={genre}
                      onChange={setGenre}
                      placeholder={t("short_drama_generate_genre_placeholder")}
                      optionsKey="short_drama_genre_options"
                    />
                  </div>
                  <div className="flex flex-col gap-1.5 @sm/canvas:w-1/3">
                    <Label htmlFor={`${fieldId}-count`}>{t("short_drama_generate_count_label")}</Label>
                    <input
                      id={`${fieldId}-count`}
                      type="number"
                      min={1}
                      max={100}
                      className="h-9 w-32 rounded-md border border-input bg-background px-3 text-sm"
                      value={count}
                      onChange={(event) => {
                        const raw = Number(event.target.value);
                        const next = Math.min(100, Math.max(1, Number.isFinite(raw) ? Math.floor(raw) : 1));
                        // 连写集数上调时同步抬高总集数下限，避免总集数小于本批数量
                        setCount(next);
                        if (total < next) setTotal(next);
                      }}
                    />
                  </div>
                  <div className="flex flex-col gap-1.5 @sm/canvas:w-1/3">
                    <Label htmlFor={`${fieldId}-total`}>{t("short_drama_generate_total_label")}</Label>
                    <input
                      id={`${fieldId}-total`}
                      type="number"
                      min={count}
                      max={100}
                      className="h-9 w-32 rounded-md border border-input bg-background px-3 text-sm"
                      value={total}
                      onChange={(event) => {
                        const raw = Number(event.target.value);
                        // 总集数下限为连写集数：HTML min 只管步进按钮，键入必须在这里拦
                        const next = Math.min(100, Math.max(Number(count), Number.isFinite(raw) ? Math.floor(raw) : Number(count)));
                        setTotal(next);
                      }}
                    />
                  </div>
                </>
              ) : (
                <>
                  <div className="flex flex-col gap-1.5 @sm/canvas:w-1/2">
                    <Label htmlFor={`${fieldId}-genre`}>{t("short_drama_generate_genre_label")}</Label>
                    <SuggestField
                      id={`${fieldId}-genre`}
                      value={genre}
                      onChange={setGenre}
                      placeholder={t("short_drama_generate_genre_placeholder")}
                      optionsKey="short_drama_genre_options"
                    />
                  </div>
                  <div className="flex flex-col gap-1.5 @sm/canvas:w-1/2">
                    <Label htmlFor={`${fieldId}-episode`}>{t("short_drama_generate_episode_label")}</Label>
                    <select
                      id={`${fieldId}-episode`}
                      className="h-9 rounded-md border border-input bg-background px-3 text-sm"
                      value={episode}
                      onChange={(event) => setEpisode(Number(event.target.value))}
                    >
                      {ledger.map((ep) => (
                        <option key={ep.episode} value={ep.episode}>
                          {episodeDisplayName(ledger, ep.episode, t)}
                        </option>
                      ))}
                    </select>
                  </div>
                </>
              )}
            </div>

            <div className="flex flex-col gap-1.5">
              <Label htmlFor={`${fieldId}-format`}>{t("short_drama_generate_format_label")}</Label>
              <FormatSelect id={`${fieldId}-format`} value={format} onChange={setFormat} />
            </div>

            {createMode && (
              <p className="text-xs text-muted-foreground">{t("short_drama_generate_create_hint")}</p>
            )}
            {createMode && (
              <p className="text-xs text-muted-foreground">{t("short_drama_generate_total_hint")}</p>
            )}
            {createMode && reusable.length > 0 && (
              <p className="text-xs text-muted-foreground">
                {t("short_drama_generate_reuse_hint", { count: Math.min(reusable.length, count) })}
              </p>
            )}
            {createMode && (
              <p className="text-xs text-muted-foreground">
                {t("short_drama_generate_existing_hint", {
                  existing: maxEpisode,
                  start: startEpisode,
                  projected: projectedTotal,
                  planned: total,
                })}
              </p>
            )}
            {overflow && (
              <p className="text-xs text-destructive">
                {t("short_drama_generate_overflow", { projected: projectedTotal, planned: total })}
              </p>
            )}

            <div className="flex flex-col gap-1.5">
              <div className="flex items-center justify-between">
                <Label htmlFor={`${fieldId}-brief`}>{t("short_drama_generate_brief_label")}</Label>
                <span className="text-xs tabular-nums text-muted-foreground">{brief.length}/1000</span>
              </div>
              <Textarea
                id={`${fieldId}-brief`}
                value={brief}
                maxLength={1000}
                onChange={(event) => setBrief(event.target.value)}
                placeholder={t("short_drama_generate_brief_placeholder")}
                className="min-h-28"
              />
            </div>

            <div className="flex flex-col gap-1.5">
              <Label htmlFor={`${fieldId}-tone`}>{t("short_drama_generate_tone_label")}</Label>
              <SuggestField
                id={`${fieldId}-tone`}
                value={tone}
                onChange={setTone}
                placeholder={t("short_drama_generate_tone_placeholder")}
                optionsKey="short_drama_tone_options"
              />
              {createMode && count > 1 && (
                <span className="text-xs text-muted-foreground">{t("short_drama_generate_count_hint")}</span>
              )}
            </div>
          </div>
        </DialogBody>
        <DialogFooter>
          <Button onClick={() => void handOff()} disabled={!canSubmit || submitting}>
            <Bot aria-hidden data-icon="inline-start" />
            {t("short_drama_generate_submit")}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

interface ButtonProps {
  projectName: string;
  episode: number;
  prominent?: boolean;
}

/** 打开剧情演绎「AI 原创剧本」弹窗的入口；置于剧情演绎项目的集页页头动作区。 */
export function ShortDramaGenerateButton({ projectName, episode, prominent = false }: ButtonProps) {
  const { t } = useTranslation("dashboard");
  const open = useShortDramaGenerateStore((s) => s.open);
  return (
    <Button
      variant={prominent ? "default" : "outline"}
      size={prominent ? "default" : "sm"}
      onClick={() => open({ projectName, episode })}
    >
      <Sparkles aria-hidden data-icon="inline-start" />
      {t("short_drama_generate_open")}
    </Button>
  );
}
