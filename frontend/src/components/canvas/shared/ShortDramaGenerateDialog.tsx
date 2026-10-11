import { useEffect, useId, useState } from "react";
import { useTranslation } from "react-i18next";
import { Bot, Sparkles } from "lucide-react";
import { Button } from "@/components/ui/button";
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
import { useEpisodeLedger } from "@/hooks/useEpisodeLedger";
import { useAppStore } from "@/stores/app-store";
import { useAssistantStore } from "@/stores/assistant-store";
import { useShortDramaGenerateStore, type ShortDramaGenerateOpenRequest } from "@/stores/short-drama-generate-store";
import { episodeDisplayName } from "@/utils/episode-display";

export interface OriginalScriptBrief {
  episode: number;
  genre: string;
  brief: string;
  tone: string;
  /** 1 为单集；大于 1 时走连写多集并逐集导入。 */
  count: number;
}

/**
 * 把创意简报固化成一段交给 Agent 的指令：让内嵌 Agent 用 short-drama-factory 技能原创剧本，
 * 再调用已注册的 import_short_drama_* 工具落盘。纯函数，便于测试。
 */
export function buildOriginalShortDramaPrompt(b: OriginalScriptBrief): string {
  const multi = b.count > 1;
  const command = multi ? "/连写 或 /极速短剧" : `/写剧本 第${b.episode}集`;
  const tool = multi ? "import_short_drama_series" : "import_short_drama_script";
  const toneLine = b.tone.trim() ? b.tone.trim() : "（按题材默认基调）";
  const lines = [
    "请使用 short-drama-factory 技能，根据以下创意简报原创短剧剧本，并导入当前项目（剧情演绎 drama 模式）。",
    "",
    "【创意简报】",
    `- 题材 / 类型：${b.genre.trim()}`,
    `- 核心梗概：${b.brief.trim()}`,
    `- 风格基调：${toneLine}`,
    "- 篇幅：标准档 90~120 秒",
    "",
    "【要求】",
    `1. 先按 short-drama-factory 的 ${command} 流程产出符合规范的剧本 Markdown：前 3 秒钩子 + 往返回合 + 断章，动作/神态与台词分明。`,
    `2. 生成后调用 ${tool} 工具，把 Markdown 导入当前项目第 ${b.episode} 集（episode_id=${b.episode}）。`,
    "3. 视觉层（image_prompt / video_prompt）保持为空，留给后续 generate_episode_script 补出；不要臆造画面提示词。",
    `4. 第 ${b.episode} 集须已存在；若尚未创建，请先说明，不要擅自建集。`,
  ];
  if (multi) {
    lines.push(`5. 本次按简报连写 ${b.count} 集，逐集分别调用导入工具。`);
  }
  return lines.join("\n");
}

/** 集页上唯一的剧情演绎「AI 原创剧本」弹窗宿主：按 {@link useShortDramaGenerateStore} 的请求打开。 */
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
 * 不需要先准备小说或源文——故事由 Agent 按简报原创；视觉层留空待后续生成。
 */
function ShortDramaGenerateDialog({ request, onClose }: DialogProps) {
  const { t } = useTranslation("dashboard");
  const ledger = useEpisodeLedger();
  const fieldId = useId();
  const [episode, setEpisode] = useState<number>(request.episode);
  const [genre, setGenre] = useState("");
  const [brief, setBrief] = useState("");
  const [tone, setTone] = useState("");
  const [count, setCount] = useState(1);

  const canSubmit = genre.trim().length > 0 && brief.trim().length > 0 && count >= 1;

  const handOff = () => {
    if (!canSubmit) return;
    const prompt = buildOriginalShortDramaPrompt({
      episode: Number(episode),
      genre,
      brief,
      tone,
      count: Number(count),
    });
    useAssistantStore.getState().setInput(prompt);
    useAppStore.getState().setAssistantPanelOpen(true);
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
              <div className="flex flex-col gap-1.5 sm:w-1/3">
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
              <div className="flex flex-col gap-1.5 sm:w-1/3">
                <Label htmlFor={`${fieldId}-genre`}>{t("short_drama_generate_genre_label")}</Label>
                <input
                  id={`${fieldId}-genre`}
                  className="h-9 rounded-md border border-input bg-background px-3 text-sm"
                  value={genre}
                  onChange={(event) => setGenre(event.target.value)}
                  placeholder={t("short_drama_generate_genre_placeholder")}
                />
              </div>
              <div className="flex flex-col gap-1.5 sm:w-1/3">
                <Label htmlFor={`${fieldId}-count`}>{t("short_drama_generate_count_label")}</Label>
                <input
                  id={`${fieldId}-count`}
                  type="number"
                  min={1}
                  max={100}
                  className="h-9 rounded-md border border-input bg-background px-3 text-sm"
                  value={count}
                  onChange={(event) => setCount(Math.max(1, Number(event.target.value) || 1))}
                />
              </div>
            </div>

            <div className="flex flex-col gap-1.5">
              <Label htmlFor={`${fieldId}-brief`}>{t("short_drama_generate_brief_label")}</Label>
              <Textarea
                id={`${fieldId}-brief`}
                value={brief}
                onChange={(event) => setBrief(event.target.value)}
                placeholder={t("short_drama_generate_brief_placeholder")}
                className="min-h-28"
              />
            </div>

            <div className="flex flex-col gap-1.5">
              <Label htmlFor={`${fieldId}-tone`}>{t("short_drama_generate_tone_label")}</Label>
              <input
                id={`${fieldId}-tone`}
                className="h-9 rounded-md border border-input bg-background px-3 text-sm"
                value={tone}
                onChange={(event) => setTone(event.target.value)}
                placeholder={t("short_drama_generate_tone_placeholder")}
              />
              {count > 1 && (
                <span className="text-xs text-muted-foreground">{t("short_drama_generate_count_hint")}</span>
              )}
            </div>
          </div>
        </DialogBody>
        <DialogFooter>
          <Button onClick={handOff} disabled={!canSubmit}>
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
