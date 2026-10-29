import { useCallback, useId } from "react";
import { useTranslation } from "react-i18next";
import { Check } from "lucide-react";
import { cn } from "cn";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import type { WorkflowPlanStep } from "@/types/workflow";
import { BatchAdmissionSummary } from "./BatchAdmissionSummary";
import { ProblemList } from "./ProblemList";
import { StaleArtifacts } from "./StaleArtifacts";
import { TaskChips } from "./TaskChips";
import { StepActButton } from "./StepActButton";
import { problemViews } from "./problem-views";
import type { NextStepView, StepAct, StepNote, StepRowTone, StepRowView } from "./step-list";

/** 时间轴节点：已完成打勾、当前步骤主色数字、其余数字；有问题的步骤用对应颜色描边。 */
function StepMarker({ tone, current, index }: { tone: StepRowTone; current: boolean; index: number }) {
  if (tone === "done") {
    return (
      <span
        aria-hidden
        className="flex size-7 shrink-0 items-center justify-center rounded-full border border-good/50 bg-good/15 text-good"
      >
        <Check className="size-3" />
      </span>
    );
  }
  const toneClass = current
    ? "border-transparent bg-primary text-primary-foreground"
    : tone === "danger"
      ? "border-destructive/50 text-destructive"
      : tone === "warn" || tone === "running"
        ? "border-warn/50 text-warn"
        : tone === "partial"
          ? "border-primary/40 text-primary"
          : "border-border text-muted-foreground";
  return (
    <span
      aria-hidden
      className={cn(
        "flex size-7 shrink-0 items-center justify-center rounded-full border text-xs font-medium",
        toneClass,
        tone === "running" && "animate-breath",
      )}
    >
      {index}
    </span>
  );
}

function NoteLine({ note, onRun }: { note: StepNote; onRun: (act: StepAct) => void }) {
  return (
    <div
      className={cn(
        "flex flex-wrap items-baseline gap-x-2 text-xs",
        note.tone === "warn" ? "text-warn" : note.tone === "danger" ? "text-destructive" : "text-subtle-foreground",
      )}
    >
      <span>{note.text}</span>
      {note.act && <StepActButton act={note.act} onRun={onRun} asLink />}
    </div>
  );
}

interface NextProps {
  next: NextStepView;
  instruction: string;
  onInstructionChange: (value: string) => void;
  onRun: (act: StepAct) => void;
  busy: boolean;
}

/**
 * 就地展开的下一步：说明、附加指令、主次入口、「或者 …」。强调色底只铺在这一块上，
 * 块里的次要文字一律用 subtle 档：muted 叠在弹层底色加强调色上不到 4.5:1。
 */
function NextStepBlock({ next, instruction, onInstructionChange, onRun, busy }: NextProps) {
  const { t } = useTranslation("workflow");
  const inputId = useId();
  return (
    <div className="flex flex-col gap-2 rounded-md border border-primary/30 bg-primary/10 p-2.5" data-testid="workflow-next-step">
      <p className="text-xs leading-relaxed">
        <span className="text-subtle-foreground">{t("next_step", { step: next.title })}</span>
        {next.detail && <span className="text-subtle-foreground"> {next.detail}</span>}
      </p>
      {next.hint && <NoteLine note={next.hint} onRun={onRun} />}
      {next.instruction && (
        <div className="max-w-110">
          <label htmlFor={inputId} className="sr-only">
            {t(next.instruction.persist ? "instruction_label_saved" : "instruction_label")}
          </label>
          <Input
            id={inputId}
            value={instruction}
            onChange={(event) => onInstructionChange(event.target.value)}
            placeholder={t(next.instruction.persist ? "instruction_placeholder_saved" : "instruction_placeholder")}
          />
        </div>
      )}
      {(next.primary.length > 0 || next.alternatives.length > 0) && (
        <div className="flex flex-wrap items-center gap-2">
          {next.primary.map((act) => (
            <StepActButton key={act.key} act={act} onRun={onRun} busy={busy} />
          ))}
          {next.alternatives.length > 0 && (
            <span className="inline-flex flex-wrap items-center gap-x-2 gap-y-1">
              <span className="text-xs text-subtle-foreground">{t("alternatives_lead")}</span>
              {next.alternatives.map((act) => (
                <StepActButton key={act.key} act={act} onRun={onRun} asLink busy={busy} />
              ))}
            </span>
          )}
        </div>
      )}
    </div>
  );
}

interface Props {
  row: StepRowView;
  next: NextStepView | null;
  instruction: string;
  onInstructionChange: (value: string) => void;
  onRun: (act: StepAct) => void;
  /** 跳到画布上的该单元。 */
  onViewUnit?: (unitId: string) => void;
  /** 显式重生这一步的指定单元（分镜图、视频）。 */
  onRegenerate?: (stepId: string, unitIds: string[]) => void;
  onConfirmDurations?: (durations: Record<string, number>) => void;
  busy: boolean;
  /** 把本行挂成动作插槽：当前视图的按钮按步骤 key 投放到这里。 */
  registerActionsSlot?: (key: string, node: HTMLDivElement | null) => void;
  /** 点行内动作时收起弹层。 */
  onActionInvoke?: () => void;
  /** 时间轴序号（1 起）。 */
  index: number;
  /** 最后一行不画时间轴连接线。 */
  isLast: boolean;
}

/** 分镜图与视频的过期产物可以就地查看与重生；资产图与视频提示词不是可重生文件产物。 */
const REGENERABLE_STEPS = new Set(["storyboard", "video"]);

/**
 * 归到这一行的后端步骤的明细：过期产物、进行中的任务、步骤自己的问题与视频整批准入。
 * 每条提示只出自一处——视频整批准入的逐单元问题只在准入结论里，计划不再把它复制到步骤问题。
 */
function StepDetails({ step, onViewUnit, onRegenerate, onConfirmDurations, busy }: {
  step: WorkflowPlanStep;
  onViewUnit?: (unitId: string) => void;
  onRegenerate?: (stepId: string, unitIds: string[]) => void;
  onConfirmDurations?: (durations: Record<string, number>) => void;
  busy: boolean;
}) {
  const { t } = useTranslation("workflow");
  const staleIds = REGENERABLE_STEPS.has(step.id) && Array.isArray(step.artifacts.stale_ids) ? step.artifacts.stale_ids : [];
  const admission = step.admission;
  const tiers = admission?.confirmation?.tiers ?? [];
  const confirm = () => {
    const durations: Record<string, number> = {};
    for (const tier of tiers) {
      if (tier.request_duration_seconds == null) continue;
      for (const unitId of tier.unit_ids) durations[unitId] = tier.request_duration_seconds;
    }
    onConfirmDurations?.(durations);
  };
  return (
    <>
      <StaleArtifacts
        staleIds={staleIds}
        onView={onViewUnit}
        onRegenerate={onRegenerate ? (unitIds) => onRegenerate(step.id, unitIds) : undefined}
        busy={busy}
      />
      <TaskChips tasks={step.tasks} />
      <ProblemList problems={problemViews(t, step.problems, step.id)} />
      {admission && admission.decision !== "admitted" && (
        <div className="flex flex-col gap-1.5">
          <BatchAdmissionSummary admission={admission} className="text-xs" />
          {admission.decision === "confirmation_required" && onConfirmDurations && (
            <div>
              <Button variant="outline" size="xs" disabled={busy} onClick={confirm}>
                {t("admission_confirm_cta")}
              </Button>
            </div>
          )}
        </div>
      )}
    </>
  );
}

/**
 * 竖向时间轴的一行：左侧是序号节点与连接线，右侧是步骤名、一句话现状、常驻入口、
 * 这一步的动作按钮；当前步骤额外标「进行中」并就地展开下一步块。
 */
export function StepListRow({ row, next, instruction, onInstructionChange, onRun, onViewUnit, onRegenerate, onConfirmDurations, busy, registerActionsSlot, onActionInvoke, index, isLast }: Props) {
  const { t } = useTranslation("workflow");
  const owns = next !== null;
  // 本行的动作插槽：当前视图把对应步骤的按钮 portal 进来。
  const actionSlotRef = useCallback((node: HTMLDivElement | null) => {
    registerActionsSlot?.(row.key, node);
  }, [registerActionsSlot, row.key]);
  return (
    <li
      className="flex min-w-0 gap-3"
      data-testid={`workflow-row-${row.key}`}
      aria-current={owns ? "step" : undefined}
    >
      <div className="relative flex w-7 shrink-0 justify-center">
        <StepMarker tone={row.tone} current={owns} index={index} />
        {!isLast && (
          <span aria-hidden className="absolute bottom-0 left-1/2 top-8 w-px -translate-x-1/2 bg-border" />
        )}
      </div>
      <div className={cn("flex min-w-0 flex-1 flex-col", isLast ? "pb-1" : "pb-4.5")}>
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-sm font-medium text-foreground">{row.title}</span>
          {owns && (
            <span className="rounded-full bg-primary/15 px-2 py-0.5 text-xs font-medium text-primary">
              {t("panel_in_progress")}
            </span>
          )}
        </div>
        <p
          className={cn(
            "text-xs leading-relaxed",
            row.tone === "warn" || row.tone === "running"
              ? "text-warn"
              : row.tone === "danger"
                ? "text-destructive"
                : "text-subtle-foreground",
          )}
        >
          {row.status}
        </p>
        {row.notes.length > 0 && (
          <div className="mt-1 flex flex-col gap-1">
            {row.notes.map((note) => (
              <NoteLine key={note.key} note={note} onRun={onRun} />
            ))}
          </div>
        )}
        {row.acts.length > 0 && (
          <div className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1">
            {row.acts.map((act) => (
              <StepActButton key={act.key} act={act} onRun={onRun} asLink busy={busy} />
            ))}
          </div>
        )}
        <div
          ref={actionSlotRef}
          className="mt-2 flex flex-wrap items-center gap-1.5 empty:hidden"
          onClickCapture={onActionInvoke}
        />
        {row.steps.length > 0 && (
          <div className="mt-1.5 flex flex-col gap-1.5">
            {row.steps.map((step) => (
              <StepDetails
                key={step.id}
                step={step}
                onViewUnit={onViewUnit}
                onRegenerate={onRegenerate}
                onConfirmDurations={onConfirmDurations}
                busy={busy}
              />
            ))}
          </div>
        )}
        {next && (
          <div className="mt-2.5">
            <NextStepBlock
              next={next}
              instruction={instruction}
              onInstructionChange={onInstructionChange}
              onRun={onRun}
              busy={busy}
            />
          </div>
        )}
      </div>
    </li>
  );
}
