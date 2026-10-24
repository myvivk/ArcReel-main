import { Textarea } from "@/components/ui/textarea";

/**
 * 视频提示词整段编辑。
 *
 * 持久化本就是单段 `video_prompt`（字符串或 `{ action }`）；`0-3秒：` 等时间标记写在正文里，
 * 不再按宫格拆成多个输入框。
 */

interface Props {
  value: string;
  onChange: (value: string) => void;
  readOnly: boolean;
  ariaLabel: string;
  placeholder: string;
}

export function VideoPromptEditor({
  value,
  onChange,
  readOnly,
  ariaLabel,
  placeholder,
}: Props) {
  return (
    <Textarea
      aria-label={ariaLabel}
      value={value}
      onChange={(event) => onChange(event.target.value)}
      readOnly={readOnly}
      placeholder={placeholder}
      className="max-h-none min-h-28"
    />
  );
}
