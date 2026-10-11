import { useEffect, useId, useState } from "react";
import { useTranslation } from "react-i18next";
import { Bot, Loader2, Sparkles } from "lucide-react";
import { enqueuePromptAuthoring, promptAuthoringResourceId } from "@/actions/generation";
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
import { useAppStore } from "@/stores/app-store";
import { useAssistantStore } from "@/stores/assistant-store";
import { useEpisodeLedger } from "@/hooks/useEpisodeLedger";
import { useShortDramaVisualStore } from "@/stores/short-drama-visual-store";
import { isResourceBusy } from "@/stores/tasks-store";
import { errMsg } from "@/utils/async";
import { episodeAgentRef } from "@/utils/episode-display";

interface HostProps {
  projectName: string;
  episode: number;
}

/** drama 集页上唯一的「AI 生成剧本（补视觉层）」弹窗宿主：按 {@link useShortDramaVisualStore} 的请求打开。 */
export function ShortDramaVisualHost({ projectName, episode }: HostProps) {
  const openProject = useShortDramaVisualStore((s) => s.projectName);
  const openEpisode = useShortDramaVisualStore((s) => s.episode);
  const close = useShortDramaVisualStore((s) => s.close);
  useEffect(() => close, [close, projectName, episode]);
  if (openProject !== projectName || openEpisode !== episode) return null;
  return <ShortDramaVisualDialog projectName={projectName} episode={episode} onClose={close} />;
}

function ShortDramaVisualDialog({ projectName, episode, onClose }: HostProps & { onClose: () => void }) {
  const { t } = useTranslation(["dashboard", "workflow"]);
  const episodeLedger = useEpisodeLedger();
  const fieldId = useId();
  const [instructions, setInstructions] = useState("");
  const [submitting, setSubmitting] = useState(false);

  const submit = async () => {
    if (submitting) return;
    if (isResourceBusy("text_episode_script", projectName, promptAuthoringResourceId(episode))) {
      useAppStore.getState().pushToast(t("prompt_authoring_busy"), "error");
      return;
    }
    setSubmitting(true);
    try {
      await enqueuePromptAuthoring(projectName, episode, { instructions: instructions.trim() || null });
      onClose();
    } catch (err) {
      useAppStore.getState().pushToast(errMsg(err), "error");
    } finally {
      setSubmitting(false);
    }
  };

  const handOff = () => {
    if (submitting) return;
    const episodeRef = episodeAgentRef(episodeLedger, episode, t);
    const lines = [
      t("workflow:agent_prefill_generate_visuals", { episodeRef }),
      t("workflow:agent_prefill_visuals_fill_only"),
    ];
    const extra = instructions.trim();
    if (extra) lines.push(t("workflow:agent_prefill_instructions", { instructions: extra }));
    useAssistantStore.getState().setInput(lines.join("\n"));
    useAppStore.getState().setAssistantPanelOpen(true);
    onClose();
  };

  const submitIcon = submitting ? (
    <Loader2 aria-hidden data-icon="inline-start" className="animate-spin" />
  ) : (
    <Sparkles aria-hidden data-icon="inline-start" />
  );
  return (
    <Dialog
      open
      onOpenChange={(next) => {
        if (!next && !submitting) onClose();
      }}
    >
      <DialogContent showCloseButton={!submitting}>
        <DialogHeader>
          <DialogTitle>{t("short_drama_visual_title")}</DialogTitle>
          <DialogDescription>{t("short_drama_visual_desc")}</DialogDescription>
        </DialogHeader>
        <DialogBody>
          <div className="flex flex-col gap-1.5">
            <Label htmlFor={fieldId}>{t("short_drama_visual_instructions_label")}</Label>
            <Textarea
              id={fieldId}
              value={instructions}
              onChange={(event) => setInstructions(event.target.value)}
              maxLength={4000}
              placeholder={t("short_drama_visual_instructions_placeholder")}
              disabled={submitting}
            />
          </div>
        </DialogBody>
        <DialogFooter>
          <Button variant="outline" onClick={() => void submit()} disabled={submitting}>
            {submitIcon}
            {t("short_drama_visual_submit")}
          </Button>
          <Button onClick={handOff} disabled={submitting}>
            <Bot aria-hidden data-icon="inline-start" />
            {t("prompt_authoring_hand_to_agent")}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

interface ButtonProps {
  projectName: string;
  episode: number;
  /** 页头动作用描边小按钮；空状态里的主操作用实心按钮。 */
  prominent?: boolean;
}

/** 打开 drama 集「AI 生成剧本（补视觉层）」弹窗的入口；任务在跑时照常显示、置灰。 */
export function ShortDramaVisualButton({ projectName, episode, prominent = false }: ButtonProps) {
  const { t } = useTranslation("dashboard");
  const open = useShortDramaVisualStore((s) => s.open);
  const busy = isResourceBusy("text_episode_script", projectName, promptAuthoringResourceId(episode));
  const Icon = busy ? Loader2 : Sparkles;
  return (
    <Button
      variant={prominent ? "default" : "outline"}
      size={prominent ? "default" : "sm"}
      disabled={busy}
      onClick={() => open(projectName, episode)}
    >
      <Icon className={busy ? "animate-spin" : undefined} aria-hidden data-icon="inline-start" />
      {t("short_drama_visual_open")}
    </Button>
  );
}
