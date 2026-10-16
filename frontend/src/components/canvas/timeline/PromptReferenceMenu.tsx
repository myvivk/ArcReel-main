import { useState } from "react";
import { ChevronDown } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";

interface PromptReferenceItem {
  label: string;
  token: string;
}

interface PromptReferenceGroup {
  label: string;
  items: PromptReferenceItem[];
}

interface PromptReferenceMenuProps {
  label: string;
  disabled?: boolean;
  groups: PromptReferenceGroup[];
  onSelect: (token: string) => void;
}

export function PromptReferenceMenu({ label, disabled, groups, onSelect }: PromptReferenceMenuProps) {
  const [open, setOpen] = useState(false);
  const availableGroups = groups.filter((group) => group.items.length > 0);

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger
        render={<Button type="button" variant="outline" size="xs" disabled={disabled || availableGroups.length === 0} />}
      >
        {label}
        <ChevronDown aria-hidden data-icon="inline-end" />
      </PopoverTrigger>
      <PopoverContent align="start" className="w-64">
        {availableGroups.map((group) => (
          <section key={group.label} className="flex flex-col gap-1">
            <h3 className="px-2 py-1 text-xs font-medium text-muted-foreground">{group.label}</h3>
            {group.items.map((item) => (
              <button
                key={`${item.token}:${item.label}`}
                type="button"
                className="focus-ring rounded-md px-2 py-1.5 text-left text-sm hover:bg-muted"
                onClick={() => {
                  onSelect(item.token);
                  setOpen(false);
                }}
              >
                {item.label}
              </button>
            ))}
          </section>
        ))}
      </PopoverContent>
    </Popover>
  );
}
