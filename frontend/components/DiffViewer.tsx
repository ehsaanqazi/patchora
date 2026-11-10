"use client";

import clsx from "clsx";
import hljs from "highlight.js/lib/core";
import bash from "highlight.js/lib/languages/bash";
import go from "highlight.js/lib/languages/go";
import javascript from "highlight.js/lib/languages/javascript";
import json from "highlight.js/lib/languages/json";
import python from "highlight.js/lib/languages/python";
import typescript from "highlight.js/lib/languages/typescript";
import yaml from "highlight.js/lib/languages/yaml";
import { FileCode2, FilePlus2 } from "lucide-react";
import { useMemo } from "react";

hljs.registerLanguage("python", python);
hljs.registerLanguage("javascript", javascript);
hljs.registerLanguage("typescript", typescript);
hljs.registerLanguage("json", json);
hljs.registerLanguage("yaml", yaml);
hljs.registerLanguage("go", go);
hljs.registerLanguage("bash", bash);

const LANGUAGES: Record<string, string> = {
  py: "python", js: "javascript", mjs: "javascript", cjs: "javascript", jsx: "javascript", ts: "typescript",
  tsx: "typescript", json: "json", yml: "yaml", yaml: "yaml", go: "go", sh: "bash",
};

interface DiffLine { kind: "add" | "del" | "ctx" | "meta"; text: string; oldNo?: number; newNo?: number }
interface DiffFile { path: string; isNew: boolean; added: number; removed: number; lines: DiffLine[] }

export function parseDiff(text: string): DiffFile[] {
  const files: DiffFile[] = [];
  let current: DiffFile | null = null;
  let oldNo = 0;
  let newNo = 0;
  for (const line of text.split("\n")) {
    if (line.startsWith("diff --git ")) {
      const path = line.split(" b/").pop() ?? line;
      current = { path, isNew: false, added: 0, removed: 0, lines: [] };
      files.push(current);
    } else if (!current) {
      continue;
    } else if (line.startsWith("new file mode")) {
      current.isNew = true;
    } else if (line.startsWith("--- ") || line.startsWith("+++ ") || line.startsWith("index ")) {
      continue;
    } else if (line.startsWith("@@")) {
      const match = /@@ -(\d+)(?:,\d+)? \+(\d+)/.exec(line);
      oldNo = Number(match?.[1] ?? 0);
      newNo = Number(match?.[2] ?? 0);
      current.lines.push({ kind: "meta", text: line });
    } else if (line.startsWith("+")) {
      current.lines.push({ kind: "add", text: line.slice(1), newNo: newNo++ });
      current.added++;
    } else if (line.startsWith("-")) {
      current.lines.push({ kind: "del", text: line.slice(1), oldNo: oldNo++ });
      current.removed++;
    } else if (line.startsWith(" ")) {
      current.lines.push({ kind: "ctx", text: line.slice(1), oldNo: oldNo++, newNo: newNo++ });
    }
  }
  return files;
}

function highlight(text: string, language: string): string {
  return hljs.highlight(text, { language, ignoreIllegals: true }).value; // hljs escapes HTML in its output
}

function escapeHtml(text: string): string {
  return text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

export function DiffFileList({ files }: { files: DiffFile[] }) {
  return (
    <ul className="space-y-1 text-sm">
      {files.map((file) => (
        <li key={file.path}>
          <a href={`#file-${encodeURIComponent(file.path)}`} className="flex items-center justify-between gap-2 rounded-md px-2 py-1.5 hover:bg-surface-3">
            <span className="flex min-w-0 items-center gap-2">
              {file.isNew ? <FilePlus2 className="size-4 shrink-0 text-ok" aria-hidden /> : <FileCode2 className="size-4 shrink-0 text-ink-faint" aria-hidden />}
              <span className="truncate font-mono text-xs">{file.path}</span>
            </span>
            <span className="shrink-0 font-mono text-xs"><span className="text-ok">+{file.added}</span> <span className="text-bad">−{file.removed}</span></span>
          </a>
        </li>
      ))}
    </ul>
  );
}

export function DiffViewer({ diff }: { diff: string }) {
  const files = useMemo(() => parseDiff(diff), [diff]);
  if (!files.length) return <p className="text-sm text-ink-muted">Empty diff.</p>;
  return (
    <div className="space-y-4">
      {files.map((file) => {
        const ext = file.path.split(".").pop() ?? "";
        const language = LANGUAGES[ext];
        return (
          <section key={file.path} id={`file-${encodeURIComponent(file.path)}`} className="overflow-hidden rounded-lg border border-line">
            <header className="flex items-center justify-between border-b border-line bg-surface-2 px-3 py-2">
              <h3 className="font-mono text-xs text-ink">{file.path}{file.isNew && <span className="ml-2 text-ok">(new file)</span>}</h3>
              <span className="font-mono text-xs"><span className="text-ok">+{file.added}</span> <span className="text-bad">−{file.removed}</span></span>
            </header>
            <div className="scrollbar-thin overflow-x-auto">
              <table className="w-full border-collapse font-mono text-xs leading-5">
                <caption className="sr-only">Changes to {file.path}</caption>
                <tbody>
                  {file.lines.map((line, i) => line.kind === "meta" ? (
                    <tr key={i} className="bg-info-soft/60"><td colSpan={3} className="px-3 py-1 text-info">{line.text}</td></tr>
                  ) : (
                    <tr key={i} className={clsx(line.kind === "add" && "bg-ok-soft", line.kind === "del" && "bg-bad-soft")}>
                      <td className="w-10 select-none border-r border-line/60 px-2 text-right text-ink-faint">{line.oldNo ?? ""}</td>
                      <td className="w-10 select-none border-r border-line/60 px-2 text-right text-ink-faint">{line.newNo ?? ""}</td>
                      <td className="whitespace-pre px-3">
                        <span className={clsx("mr-2 select-none", line.kind === "add" ? "text-ok" : line.kind === "del" ? "text-bad" : "text-ink-faint")}
                          aria-label={line.kind === "add" ? "added" : line.kind === "del" ? "removed" : undefined}>
                          {line.kind === "add" ? "+" : line.kind === "del" ? "−" : " "}
                        </span>
                        <span dangerouslySetInnerHTML={{ __html: language ? highlight(line.text, language) : escapeHtml(line.text) }} />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </section>
        );
      })}
    </div>
  );
}
