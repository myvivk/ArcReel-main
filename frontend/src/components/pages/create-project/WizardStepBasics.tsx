import { useId, type Ref } from "react";
import { useTranslation } from "react-i18next";
import { cn } from "cn";
import { Input } from "@/components/ui/input";
import { WizardSection } from "./WizardSection";

export interface WizardBasicsValue {
  title: string;
  aspectRatio: "9:16" | "16:9";
}

const ASPECT_RATIOS = [
  { ratio: "9:16", labelKey: "portrait_9_16", glyph: "h-3.5 w-2" },
  { ratio: "16:9", labelKey: "landscape_16_9", glyph: "h-2 w-3.5" },
] as const;

/** 整块可点的单选卡：选中项用品牌色描边与浅底。 */
function choiceClass(selected: boolean): string {
  return cn(
    "relative flex cursor-pointer rounded-lg border transition-colors has-focus-visible:ring-3 has-focus-visible:ring-ring/50",
    selected ? "border-primary/60 bg-primary/10" : "border-border hover:bg-accent",
  );
}

export interface WizardStepBasicsProps {
  value: WizardBasicsValue;
  onChange: (next: WizardBasicsValue) => void;
  titleRef?: Ref<HTMLInputElement>;
}

export function WizardStepBasics({ value, onChange, titleRef }: WizardStepBasicsProps) {
  const { t } = useTranslation("dashboard");
  const reactId = useId();
  const titleId = `${reactId}-title`;
  const titleHintId = `${reactId}-title-hint`;
  const aspectId = `${reactId}-aspect`;

  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-col gap-1.5">
        <label htmlFor={titleId} className="text-sm font-medium text-foreground">
          {t("project_title")}
        </label>
        <Input
          ref={titleRef}
          id={titleId}
          value={value.title}
          onChange={(e) => onChange({ ...value, title: e.target.value })}
          placeholder={t("rebirth_empress_example")}
          aria-required="true"
          aria-describedby={titleHintId}
          autoComplete="off"
        />
        <p id={titleHintId} className="text-xs text-muted-foreground">
          {t("project_id_auto_gen_hint")}
        </p>
      </div>

      <WizardSection title={<span id={aspectId}>{t("aspect_ratio")}</span>}>
        <div role="radiogroup" aria-labelledby={aspectId} className="flex gap-2.5">
          {ASPECT_RATIOS.map(({ ratio, labelKey, glyph }) => (
            <label key={ratio} className={cn(choiceClass(value.aspectRatio === ratio), "items-center gap-2 px-3 py-2")}>
              <input
                type="radio"
                name={aspectId}
                value={ratio}
                checked={value.aspectRatio === ratio}
                onChange={() => onChange({ ...value, aspectRatio: ratio })}
                className="sr-only"
              />
              <span aria-hidden className={cn("rounded-xs border border-muted-foreground", glyph)} />
              <span className="text-sm text-foreground">{t(labelKey)}</span>
            </label>
          ))}
        </div>
      </WizardSection>

    </div>
  );
}
