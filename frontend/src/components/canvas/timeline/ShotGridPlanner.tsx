import { useCallback, useEffect, useState } from "react";
import { Check, Loader2, RefreshCw, Save } from "lucide-react";
import { API } from "@/api";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import type { ShotGridPlan } from "@/types/shot-grid";

export function ShotGridPlanner({
  projectName,
  scriptFile,
  episode,
  shotId,
}: {
  projectName: string;
  scriptFile?: string;
  episode?: number;
  shotId: string;
}) {
  const [plan, setPlan] = useState<ShotGridPlan | null>(null);
  const [prompts, setPrompts] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const available = Boolean(scriptFile && episode);

  useEffect(() => {
    if (!scriptFile || !episode) return;
    const controller = new AbortController();
    void API.getShotGrid(projectName, scriptFile, shotId, { signal: controller.signal })
      .then((next) => {
        setPlan(next);
        setPrompts(next?.cells.map((cell) => cell.prompt) ?? ["", "", "", ""]);
      })
      .catch((cause) => {
        if (!controller.signal.aborted) setError(cause instanceof Error ? cause.message : "加载镜头规划失败");
      });
    return () => controller.abort();
  }, [episode, projectName, scriptFile, shotId]);

  const save = useCallback(async () => {
    if (!scriptFile || !episode) return;
    setBusy(true);
    setError(null);
    try {
      const next = await API.saveShotGrid(projectName, {
        script_file: scriptFile,
        episode,
        shot_id: shotId,
        rows: 2,
        cols: 2,
        cells: prompts.map((prompt, index) => ({ index, prompt })),
      });
      setPlan(next);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "保存镜头规划失败");
    } finally {
      setBusy(false);
    }
  }, [episode, projectName, prompts, scriptFile, shotId]);

  const generate = useCallback(async () => {
    if (!scriptFile || !plan) return;
    setBusy(true);
    setError(null);
    try {
      const response = await API.generateShotGrid(projectName, scriptFile, shotId);
      setPlan((current) => current ? { ...current, status: "generating" } : current);
      for (;;) {
        await new Promise((resolve) => window.setTimeout(resolve, 1500));
        const task = await API.getShotGridTask(projectName, response.task_id);
        if (task.status === "succeeded") {
          const next = await API.getShotGrid(projectName, scriptFile, shotId);
          setPlan(next);
          return;
        }
        if (task.status === "failed" || task.status === "cancelled") {
          throw new Error(task.error || "联合图生成失败");
        }
      }
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "生成联合图失败");
      const next = await API.getShotGrid(projectName, scriptFile, shotId).catch(() => null);
      if (next) setPlan(next);
    } finally {
      setBusy(false);
    }
  }, [plan, projectName, scriptFile, shotId]);

  const confirm = useCallback(async () => {
    if (!plan) return;
    setBusy(true);
    setError(null);
    try {
      const response = await API.confirmShotGrid(projectName, plan.id);
      setPlan(response.grid);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "确认镜头宫格失败");
    } finally {
      setBusy(false);
    }
  }, [plan, projectName]);

  if (!available) return null;
  return (
    <section className="mt-4 rounded-md border bg-card p-4" aria-label="镜头宫格规划">
      <div className="mb-3 flex items-center justify-between gap-3">
        <div>
          <h3 className="text-sm font-semibold">镜头宫格</h3>
          <p className="text-xs text-muted-foreground">一个分镜内规划多个镜头，确认后再写入镜头资产。</p>
        </div>
        <Badge variant="outline">{plan?.status ?? "draft"}</Badge>
      </div>
      <div className="grid grid-cols-1 gap-3 min-[560px]:grid-cols-2">
        {prompts.map((prompt, index) => (
          <div key={index} className="min-w-0">
            <div className="mb-1 text-xs text-muted-foreground">镜头 {index + 1}</div>
            <Textarea
              value={prompt}
              onChange={(event) => setPrompts((current) => current.map((item, itemIndex) => itemIndex === index ? event.target.value : item))}
              placeholder="描述景别、动作、构图和连续性"
              className="min-h-20 resize-y"
              disabled={busy || plan?.status === "confirmed"}
            />
          </div>
        ))}
      </div>
      {plan?.composite_url ? <img src={plan.composite_url} alt="联合图预览" className="mt-4 w-full border object-contain" /> : null}
      {error ? <p className="mt-3 text-sm text-destructive">{error}</p> : null}
      <div className="mt-4 flex flex-wrap gap-2">
        <Button size="sm" variant="outline" onClick={() => void save()} disabled={busy || plan?.status === "confirmed"}>
          {busy ? <Loader2 className="mr-2 size-4 animate-spin" /> : <Save className="mr-2 size-4" />}保存规划
        </Button>
        <Button size="sm" onClick={() => void generate()} disabled={busy || !plan || plan.status === "confirmed" || plan.status === "generating"}>
          {busy ? <Loader2 className="mr-2 size-4 animate-spin" /> : <RefreshCw className="mr-2 size-4" />}生成联合图
        </Button>
        <Button size="sm" variant="secondary" onClick={() => void confirm()} disabled={busy || plan?.status !== "completed"}>
          <Check className="mr-2 size-4" />确认并关联镜头
        </Button>
      </div>
    </section>
  );
}
