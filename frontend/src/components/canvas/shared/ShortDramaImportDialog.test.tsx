import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { importShortDramaArtifacts } from "@/actions/shortDrama";
import { useShortDramaImportStore } from "@/stores/short-drama-import-store";
import { ShortDramaImportHost } from "./ShortDramaImportDialog";

vi.mock("@/actions/shortDrama", () => ({ importShortDramaArtifacts: vi.fn().mockResolvedValue({ artifacts: [] }) }));
vi.mock("./refreshAfterWrite", () => ({ refreshAfterWrite: vi.fn().mockResolvedValue("success") }));

function file(path: string, text: string): File {
  const bytes = new TextEncoder().encode(text);
  const result = new File([bytes], path.split("/").at(-1)!, { type: "text/markdown" });
  Object.defineProperties(result, {
    webkitRelativePath: { value: path },
    arrayBuffer: { value: async () => bytes.buffer },
  });
  return result;
}

beforeEach(() => {
  vi.clearAllMocks();
  useShortDramaImportStore.getState().open({ projectName: "demo" });
});

describe("factory artifact directory import", () => {
  it("forwards original bytes and canonical relative paths from a selected directory", async () => {
    render(<ShortDramaImportHost projectName="demo" />);
    const source = "# 人物圣经\r\n林夏\r\n";
    const input = screen.getByLabelText("工件目录", { selector: "input" });
    fireEvent.change(input, { target: { files: [file("delivery/character-bible.md", source)] } });
    await screen.findByText("character-bible.md");
    fireEvent.click(screen.getByRole("button", { name: "原样导入工件" }));
    await waitFor(() => expect(importShortDramaArtifacts).toHaveBeenCalledTimes(1));
    const submitted = vi.mocked(importShortDramaArtifacts).mock.calls[0][1][0];
    expect(submitted.path).toBe("character-bible.md");
    expect(new TextDecoder().decode(Uint8Array.from(atob(submitted.content_base64), (c) => c.charCodeAt(0)))).toBe(source);
    await waitFor(() => expect(useShortDramaImportStore.getState().request).toBeNull());
  });

  it("rejects unrelated files and has no legacy Markdown or episode selector", async () => {
    render(<ShortDramaImportHost projectName="demo" />);
    fireEvent.change(screen.getByLabelText("工件目录", { selector: "input" }), {
      target: { files: [file("delivery/novel.md", "# 小说")] },
    });
    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "原样导入工件" })).toBeDisabled();
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
    expect(screen.queryByRole("combobox")).not.toBeInTheDocument();
    expect(importShortDramaArtifacts).not.toHaveBeenCalled();
  });
});
