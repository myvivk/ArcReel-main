import { useCallback, useState } from "react";
import { useTranslation } from "react-i18next";
import { Bot, Loader2, Plus, Scissors } from "lucide-react";
import { Button } from "@/components/ui/button";
import { EpisodeHeaderActions } from "@/components/canvas/episode-page/EpisodeHeaderActions";
import { useEpisodeLedger } from "@/hooks/useEpisodeLedger";
import { useAppStore } from "@/stores/app-store";
import { useAssistantStore } from "@/stores/assistant-store";
import { useWorkflowStore, workflowPlanKey } from "@/stores/workflow-store";
import { editAdmission } from "@/components/workflow/step-list";
import type { EditTimelineReadout } from "@/types/edit-timeline";
import { errMsg } from "@/utils/async";
import { episodeAgentRef } from "@/utils/episode-display";
import { createScriptEditTimeline } from "./create-script-timeline";

interface EditTimelineEmptyStateProps {
  projectName: string;
  episode: number;
  /** 鏂板缓鎴愬姛鍚庡洖璋冿紝鍓緫瑙嗗浘鎹鍒囧埌鏂扮殑鍓緫鏃堕棿绾裤€?*/
  onCreated: (created: EditTimelineReadout) => void;
}

/**
 * 鍓緫瑙嗗浘鍦ㄦ湰闆嗚繕娌℃湁鍓緫鏃堕棿绾挎椂鐨勭┖鐘舵€併€? *
 * 涓や釜鍏ュ彛涓?WorkflowPanel銆屽壀杈戙€嶈鍚屼竴濂楋細銆屼氦缁?Agent 鍓緫銆嶅彧鎶婅姹傞濉繘瀵硅瘽杈撳叆妗嗗苟鎵撳紑闈㈡澘锛? * 鐢辩敤鎴风‘璁ゅ彂閫侊紱銆屾柊寤哄壀杈戞椂闂寸嚎銆嶇洿鎺ユ寜鑴氭湰鏈烘鏂板缓锛屼笉缁忚繃 Agent銆傚嚭鐗囦粠涓嶈嚜鍔ㄦ柊寤哄壀杈戞椂闂寸嚎銆? *
 * 鍑嗗叆鍙栧埗浣滅姸鎬侀噷鐨勫悓涓€鎿嶄綔锛堟湰闆嗚嚦灏戞湁涓€涓彲鐢ㄨ棰戯級锛屼笌鏈嶅姟绔柊寤烘椂鐨勬嫆缁濆悓婧愶細鍑嗗叆涓嶆垚绔嬫垨璇讳笉鍒? * 鍒朵綔鐘舵€佹椂鍙鏄庡師鍥狅紝涓嶇粰鍑哄叆鍙ｂ€斺€旀寜涓嶅姩鐨勬寜閽笌鐐逛簡蹇呰鎷掔殑鎸夐挳閮戒笉璇ュ嚭鐜板湪杩欓噷銆? */
export function EditTimelineEmptyState({
  projectName,
  episode,
  onCreated,
}: EditTimelineEmptyStateProps) {
  const { t } = useTranslation(["dashboard", "workflow"]);
  const ledger = useEpisodeLedger();
  const [creating, setCreating] = useState(false);
  const operation = useWorkflowStore((s) =>
    s.planKey === workflowPlanKey(projectName, episode) ? s.plan?.status.operations.create_edit_timeline : undefined,
  );
  const admission = editAdmission(t, operation);
  const admitted = admission.admitted;
  const unavailable = admission.reason;

  const handleHandToAgent = useCallback(() => {
    // 鍙～涓嶅彂閫侊紝宸叉湁浼氳瘽鏃朵笉鍒囨崲銆佷笉鏂板缓
    const episodeRef = episodeAgentRef(ledger, episode, t);
    useAssistantStore.getState().setInput(t("workflow:agent_prefill_create_edit_timeline", { episodeRef }));
    useAppStore.getState().setAssistantPanelOpen(true);
  }, [episode, ledger, t]);

  const handleCreate = async () => {
    if (creating) return;
    setCreating(true);
    try {
      // 鏂板缓鐨勫壀杈戞椂闂寸嚎闅忓嵆鎴愪负閫変腑鐨勬爣绛撅紝涓嶅啀鍙﹀脊鎻愮ず銆?      onCreated((await createScriptEditTimeline(projectName, episode, t)).created);
    } catch (err) {
      useAppStore.getState().pushToast(t("edit_timeline_create_failed", { message: errMsg(err) }), "error");
    } finally {
      setCreating(false);
    }
  };

  return (
    <div className="flex min-h-0 flex-1 items-center justify-center p-8">
      <div className="flex max-w-md flex-col items-center gap-4 text-center">
        <span
          aria-hidden="true"
          className="grid size-11 place-items-center rounded-xl border border-primary/25 bg-primary/10 text-primary"
        >
          <Scissors className="size-5" />
        </span>
        <div className="flex flex-col gap-1.5">
          <h2 className="text-base font-semibold text-foreground">{t("edit_view_empty_title")}</h2>
          <p className="text-sm text-muted-foreground">
            {admitted ? t("edit_view_empty_description") : unavailable}
          </p>
        </div>
        <EpisodeHeaderActions>
          <Button
            size="sm"
            onClick={handleHandToAgent}
            disabled={!admitted}
            title={admitted ? undefined : (unavailable ?? undefined)}
          >
            <Bot data-icon="inline-start" aria-hidden />
            {t("workflow:act_agent_edit")}
          </Button>
          <Button
            variant="outline"
            size="sm"
            onClick={() => void handleCreate()}
            disabled={!admitted || creating}
            title={admitted ? undefined : (unavailable ?? undefined)}
          >
            {creating ? (
              <Loader2 data-icon="inline-start" className="animate-spin" aria-hidden />
            ) : (
              <Plus data-icon="inline-start" aria-hidden />
            )}
            {t("workflow:act_create_edit_timeline")}
          </Button>
        </EpisodeHeaderActions>
      </div>
    </div>
  );
}
