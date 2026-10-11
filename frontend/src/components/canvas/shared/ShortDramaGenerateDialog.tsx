import { useEffect, useId, useMemo, useState } from "react";
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
import { useShortDramaGenerateStore, type ShortDramaGenerateOpenRequest } from "@/stores/short-drama-generate-store";
import { episodeDisplayName } from "@/utils/episode-display";

export type ShortDramaFormat = "standard" | "long" | "manju";

export interface OriginalScriptBrief {
  /** 目标集号列表：单集导入时长度为 1；连写多集时为各集号。 */
  episodes: number[];
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

/** 按档位给出短剧工厂的撰写指令：长档用 /写长档、/连写 长档。 */
function formatWriteCommand(format: ShortDramaFormat, single: boolean, episode: number): string {
  if (single) return format === "long" ? `/写长档 第${episode}集` : `/写剧本 第${episode}集`;
  return format === "long" ? "/连写 长档" : "/连写";
}

/**
 * 把创意简报固化成一段交给 Agent 的指令：让内嵌 Agent 用 short-drama-factory 技能原创剧本，
 * 再调用已注册的 import_short_drama_* 工具落盘。纯函数，便于测试。
 * 注意：import_short_drama_script 要求目标集已存在；创建模式下由前端在提交时透明建好集，用户无感。
 */
export function buildOriginalShortDramaPrompt(b: OriginalScriptBrief): string {
  const episodes = b.episodes;
  const single = episodes.length === 1;
  const command = formatWriteCommand(b.format, single, episodes[0]);
  const tool = single ? "import_short_drama_script" : "import_short_drama_series";
  const toneLine = b.tone.trim() ? b.tone.trim() : "（按题材默认基调）";
  const lines = [
    "请使用 short-drama-factory 技能，根据以下创意简报原创短剧剧本，并导入当前项目（剧情演绎 drama 模式）。",
    "",
    "【创意简报】",
    `- 题材 / 类型：${b.genre.trim()}`,
    `- 核心梗概：${b.brief.trim()}`,
    `- 风格基调：${toneLine}`,
    `- 篇幅：${FORMAT_LENGTH[b.format]}`,
    "",
    "【要求】",
    `1. 先按 short-drama-factory 的 ${command} 流程产出符合规范的剧本 Markdown：前 3 秒钩子 + 往返回合 + 断章，动作/神态与台词分明。`,
  ];
  if (single) {
    lines.push(
      `2. 生成后调用 ${tool} 工具，把 Markdown 导入当前项目第 ${episodes[0]} 集（episode_id=${episodes[0]}）。`,
      "3. 视觉层（image_prompt / video_prompt）保持为空，留给后续 generate_episode_script 补出；不要臆造画面提示词。",
      `4. 第 ${episodes[0]} 集已存在，直接导入即可。`,
    );
  } else {
    lines.push(
      `2. 生成后逐集调用 ${tool} 工具，把各集 Markdown 分别导入本项目的第 ${episodes.join("、")} 集（episode_id 对应）。`,
      "3. 视觉层（image_prompt / video_prompt）保持为空，留给后续 generate_episode_script 补出；不要臆造画面提示词。",
      `4. 第 ${episodes.join("、")} 集均已存在，逐集导入即可。`,
    );
  }
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
      (t(optionsKey) as string)
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
        if (next !== null) onChange(next as ShortDramaFormat);
      }}
    >
      <SelectTrigger size="sm" aria-label={t("short_drama_generate_format_label")} className="w-full sm:w-64">
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
  const fieldId = useId();
  const createMode = request.episode === 0;
  const [episode, setEpisode] = useState<number>(request.episode);
  const [genre, setGenre] = useState("");
  const [brief, setBrief] = useState("");
  const [tone, setTone] = useState("");
  const [count, setCount] = useState(1);
  const [format, setFormat] = useState<ShortDramaFormat>("standard");
  const [submitting, setSubmitting] = useState(false);

  const canSubmit = genre.trim().length > 0 && count >= 1;

  const handOff = async () => {
    if (!canSubmit || submitting) return;
    // 创建模式下先透明建好目标集（import 工具要求目标集已存在）；用户不在界面上执行「建集」。
    let episodes: number[];
    if (createMode) {
      setSubmitting(true);
      try {
        episodes = await createBlankEpisodes(request.projectName, Number(count));
      } catch {
        setSubmitting(false);
        return;
      }
    } else {
      episodes = [Number(episode)];
    }
    const prompt = buildOriginalShortDramaPrompt({ episodes, genre, brief, tone, format });
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
            <div className="flex flex-col gap-3 sm:flex-row sm:gap-4">
              {createMode ? (
                <>
                  <div className="flex flex-col gap-1.5 sm:w-1/2">
                    <Label htmlFor={`${fieldId}-genre`}>{t("short_drama_generate_genre_label")}</Label>
                    <SuggestField
                      id={`${fieldId}-genre`}
                      value={genre}
                      onChange={setGenre}
                      placeholder={t("short_drama_generate_genre_placeholder")}
                      optionsKey="short_drama_genre_options"
                    />
                  </div>
                  <div className="flex flex-col gap-1.5 sm:w-1/2">
                    <Label htmlFor={`${fieldId}-count`}>{t("short_drama_generate_count_label")}</Label>
                    <input
                      id={`${fieldId}-count`}
                      type="number"
                      min={1}
                      max={100}
                      className="h-9 w-32 rounded-md border border-input bg-background px-3 text-sm"
                      value={count}
                      onChange={(event) => {
                        const next = Number(event.target.value);
                        // 硬钳制到 1~100：HTML min/max 只管步进按钮，键入必须在这里拦
                        setCount(Math.min(100, Math.max(1, Number.isFinite(next) ? Math.floor(next) : 1)));
                      }}
                    />
                  </div>
                </>
              ) : (
                <>
                  <div className="flex flex-col gap-1.5 sm:w-1/2">
                    <Label htmlFor={`${fieldId}-genre`}>{t("short_drama_generate_genre_label")}</Label>
                    <SuggestField
                      id={`${fieldId}-genre`}
                      value={genre}
                      onChange={setGenre}
                      placeholder={t("short_drama_generate_genre_placeholder")}
                      optionsKey="short_drama_genre_options"
                    />
                  </div>
                  <div className="flex flex-col gap-1.5 sm:w-1/2">
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
