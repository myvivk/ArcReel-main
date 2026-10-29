import type { ReactNode } from "react";
import { ChevronRight } from "lucide-react";
import { cn } from "cn";
import { Button } from "@/components/ui/button";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";

interface ReviewBlockProps {
  title: string;
  score: number;
  max: number;
  /** 分数配色：各评测的成功阈值不同，由调用方决定。 */
  tone: string;
  children: ReactNode;
}

/**
 * 只读评测块的折叠壳：标题行常显总分，维度、缺陷与建议收起。
 * 折叠写法与项目其它小节一致（触发器经 render 渲染为 Button，箭头随展开旋转），
 * 块本身不加边框，与详情里的小节排在同一列里。
 */
export function ReviewBlock({ title, score, max, tone, children }: ReviewBlockProps) {
  return (
    <Collapsible render={<div className="min-w-0" />}>
      <div className="flex min-h-7 items-center gap-1.5">
        <h4 className="min-w-0 text-xs font-medium text-muted-foreground">
          <CollapsibleTrigger render={<Button variant="ghost" size="xs" className="-ml-2" />}>
            <ChevronRight
              aria-hidden
              className="text-muted-foreground transition-transform group-aria-expanded/button:rotate-90"
            />
            {title}
          </CollapsibleTrigger>
        </h4>
        <span className={cn("ml-auto text-xs font-semibold tabular-nums", tone)}>
          {score} / {max}
        </span>
      </div>
      <CollapsibleContent>
        <div className="mt-1 flex flex-col gap-1 text-xs">{children}</div>
      </CollapsibleContent>
    </Collapsible>
  );
}
