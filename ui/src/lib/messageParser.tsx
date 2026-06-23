'use client';

import { useState } from 'react';
import { ChevronRight, ChevronDown } from 'lucide-react';

// --- Types ---

export interface ToolCallInfo {
  name: string;
  input: string | null;
  result_preview: string | null;
}

interface ParsedSegment {
  type: 'text' | 'system-reminder' | 'command' | 'command-output';
  content: string;
  commandName?: string;
  commandArgs?: string;
}

// --- XML Parsing Helpers ---

const XML_TAG_PATTERNS: { tag: string; type: ParsedSegment['type'] }[] = [
  { tag: 'system-reminder', type: 'system-reminder' },
  { tag: 'local-command-caveat', type: 'system-reminder' },
];

function extractTag(text: string, tagName: string): { content: string; before: string; after: string } | null {
  const openTag = `<${tagName}>`;
  const closeTag = `</${tagName}>`;
  const start = text.indexOf(openTag);
  if (start === -1) return null;
  const end = text.indexOf(closeTag, start);
  if (end === -1) return null;
  return {
    content: text.slice(start + openTag.length, end),
    before: text.slice(0, start),
    after: text.slice(end + closeTag.length),
  };
}

function extractCommandBlock(text: string): { commandName: string; commandArgs: string; stdout: string; before: string; after: string } | null {
  const nameMatch = extractTag(text, 'command-name');
  if (!nameMatch) return null;

  let remaining = nameMatch.after;
  const commandName = nameMatch.content.trim();

  const msgMatch = extractTag(remaining, 'command-message');
  if (msgMatch) remaining = msgMatch.after;

  const argsMatch = extractTag(remaining, 'command-args');
  const commandArgs = argsMatch ? argsMatch.content.trim() : '';
  if (argsMatch) remaining = argsMatch.after;

  const stdoutMatch = extractTag(remaining, 'local-command-stdout');
  const stdout = stdoutMatch ? stdoutMatch.content.trim() : '';
  if (stdoutMatch) remaining = stdoutMatch.after;

  return {
    commandName,
    commandArgs,
    stdout,
    before: nameMatch.before,
    after: remaining,
  };
}

function stripToolMarkers(text: string): string {
  return text.replace(/\[tool: [^\]]+\]\n?/g, '').trim();
}

export function parseMessageContent(raw: string): ParsedSegment[] {
  if (!raw) return [];

  const segments: ParsedSegment[] = [];
  let text = raw;

  while (text.length > 0) {
    let earliestIndex = text.length;
    let earliestType: 'xml-tag' | 'command' | null = null;
    let earliestTagIndex = -1;

    for (let i = 0; i < XML_TAG_PATTERNS.length; i++) {
      const idx = text.indexOf(`<${XML_TAG_PATTERNS[i].tag}>`);
      if (idx !== -1 && idx < earliestIndex) {
        earliestIndex = idx;
        earliestType = 'xml-tag';
        earliestTagIndex = i;
      }
    }

    const cmdIdx = text.indexOf('<command-name>');
    if (cmdIdx !== -1 && cmdIdx < earliestIndex) {
      earliestIndex = cmdIdx;
      earliestType = 'command';
    }

    if (earliestType === null) {
      const cleaned = stripToolMarkers(text);
      if (cleaned) segments.push({ type: 'text', content: cleaned });
      break;
    }

    const before = stripToolMarkers(text.slice(0, earliestIndex));
    if (before) segments.push({ type: 'text', content: before });

    if (earliestType === 'xml-tag') {
      const pattern = XML_TAG_PATTERNS[earliestTagIndex];
      const result = extractTag(text, pattern.tag);
      if (result) {
        segments.push({ type: pattern.type, content: result.content.trim() });
        text = result.after;
      } else {
        text = text.slice(earliestIndex + 1);
      }
    } else if (earliestType === 'command') {
      const result = extractCommandBlock(text);
      if (result) {
        segments.push({
          type: 'command',
          content: result.stdout,
          commandName: result.commandName,
          commandArgs: result.commandArgs,
        });
        text = result.after;
      } else {
        text = text.slice(earliestIndex + 1);
      }
    }
  }

  return segments;
}

export function hasStructuredContent(content: string): boolean {
  if (!content) return false;
  return /<(system-reminder|local-command-caveat|command-name|local-command-stdout)>/.test(content)
    || /\[tool: [^\]]+\]/.test(content);
}

// --- Tool Colors ---

const TOOL_THEME: Record<string, { bg: string; text: string; border: string }> = {
  Read:         { bg: 'bg-blue-500/15',    text: 'text-blue-600 dark:text-blue-400',       border: 'border-blue-500/25' },
  Edit:         { bg: 'bg-emerald-500/15', text: 'text-emerald-600 dark:text-emerald-400', border: 'border-emerald-500/25' },
  Write:        { bg: 'bg-emerald-500/15', text: 'text-emerald-600 dark:text-emerald-400', border: 'border-emerald-500/25' },
  Bash:         { bg: 'bg-orange-500/15',  text: 'text-orange-600 dark:text-orange-400',   border: 'border-orange-500/25' },
  Grep:         { bg: 'bg-violet-500/15',  text: 'text-violet-600 dark:text-violet-400',   border: 'border-violet-500/25' },
  Glob:         { bg: 'bg-violet-500/15',  text: 'text-violet-600 dark:text-violet-400',   border: 'border-violet-500/25' },
  Agent:        { bg: 'bg-pink-500/15',    text: 'text-pink-600 dark:text-pink-400',       border: 'border-pink-500/25' },
  WebSearch:    { bg: 'bg-cyan-500/15',    text: 'text-cyan-600 dark:text-cyan-400',       border: 'border-cyan-500/25' },
  WebFetch:     { bg: 'bg-cyan-500/15',    text: 'text-cyan-600 dark:text-cyan-400',       border: 'border-cyan-500/25' },
  TodoWrite:    { bg: 'bg-yellow-500/15',  text: 'text-yellow-600 dark:text-yellow-400',   border: 'border-yellow-500/25' },
  NotebookEdit: { bg: 'bg-indigo-500/15',  text: 'text-indigo-600 dark:text-indigo-400',   border: 'border-indigo-500/25' },
  LSP:          { bg: 'bg-teal-500/15',    text: 'text-teal-600 dark:text-teal-400',       border: 'border-teal-500/25' },
  Skill:        { bg: 'bg-rose-500/15',    text: 'text-rose-600 dark:text-rose-400',       border: 'border-rose-500/25' },
};

const DEFAULT_THEME = { bg: 'bg-primary/15', text: 'text-primary', border: 'border-primary/25' };

function getToolColor(toolName: string): string {
  const t = TOOL_THEME[toolName] || DEFAULT_THEME;
  return `${t.bg} ${t.text}`;
}

function getToolBorderColor(toolName: string): string {
  return (TOOL_THEME[toolName] || DEFAULT_THEME).border;
}

// --- Legacy ToolBadges (still used in grid/summary views) ---

export function ToolBadges({ tools }: { tools: string[] }) {
  if (!tools.length) return null;
  return (
    <div className="flex items-center gap-1 flex-wrap">
      {tools.map((tool, i) => (
        <span key={i} className={`inline-flex items-center rounded-md border border-transparent px-1.5 py-0.5 text-[10px] font-medium font-mono ${getToolColor(tool)}`}>
          {tool}
        </span>
      ))}
    </div>
  );
}

// --- Expandable Tool Call ---

function ToolCallItem({ tc }: { tc: ToolCallInfo }) {
  const [expanded, setExpanded] = useState(false);
  const canExpand = !!(tc.result_preview?.trim());

  return (
    <div className="rounded-md border bg-card/50">
      <button
        onClick={() => canExpand && setExpanded(!expanded)}
        className={`flex items-center gap-2 w-full text-left px-2.5 py-1.5 ${canExpand ? 'cursor-pointer hover:bg-accent/50' : 'cursor-default'} transition-colors rounded-md`}
      >
        {canExpand ? (
          expanded ? <ChevronDown className="h-3 w-3 text-muted-foreground shrink-0" /> : <ChevronRight className="h-3 w-3 text-muted-foreground shrink-0" />
        ) : (
          <span className="w-3 shrink-0" />
        )}
        <span className={`inline-flex items-center rounded px-1.5 py-0.5 text-[10px] font-semibold font-mono shrink-0 ${getToolColor(tc.name)}`}>
          {tc.name}
        </span>
        {tc.input && (
          <span className="text-[12px] text-muted-foreground font-mono truncate">
            {tc.input}
          </span>
        )}
      </button>
      {expanded && canExpand && (
        <div className="px-3 pb-2.5 pt-0.5">
          <pre className="text-[11px] text-muted-foreground bg-muted/50 rounded-md px-3 py-2 font-mono whitespace-pre-wrap break-all max-h-[300px] overflow-auto scrollbar-thin leading-relaxed">
            {tc.result_preview}
          </pre>
        </div>
      )}
    </div>
  );
}

// --- Tool Call List ---

export function ToolCallList({ toolCalls }: { toolCalls: ToolCallInfo[] }) {
  if (!toolCalls.length) return null;
  return (
    <div className="flex flex-col gap-1 mt-1.5">
      {toolCalls.map((tc, i) => (
        <ToolCallItem key={i} tc={tc} />
      ))}
    </div>
  );
}

// --- Message Content ---

export function MessageContent({ content }: { content: string }) {
  if (!hasStructuredContent(content)) {
    const display = content.slice(0, 500);
    return (
      <div className="text-[13px] leading-relaxed text-foreground whitespace-pre-wrap max-h-[200px] overflow-auto scrollbar-thin">
        {display}
        {content.length > 500 && <span className="text-muted-foreground"> ...truncated</span>}
      </div>
    );
  }

  const segments = parseMessageContent(content);
  if (segments.length === 0) return null;

  return (
    <div className="space-y-1.5">
      {segments.map((seg, i) => {
        if (seg.type === 'system-reminder') {
          return (
            <div key={i} className="inline-flex items-center gap-1.5">
              <span className="inline-flex items-center rounded-md border border-transparent bg-amber-500/15 text-amber-600 dark:text-amber-400 px-1.5 py-0.5 text-[11px] font-medium">
                System Reminder
              </span>
            </div>
          );
        }

        if (seg.type === 'command') {
          return (
            <div key={i} className="flex flex-col gap-1">
              <div className="flex items-center gap-1.5 flex-wrap">
                <span className="inline-flex items-center rounded-md border border-transparent bg-primary/15 text-primary px-1.5 py-0.5 text-[11px] font-medium font-mono">
                  {seg.commandName}{seg.commandArgs ? ` ${seg.commandArgs}` : ''}
                </span>
              </div>
              {seg.content && (
                <code className="text-[12px] text-muted-foreground bg-muted/50 rounded px-2 py-1 block font-mono whitespace-pre-wrap">
                  {seg.content}
                </code>
              )}
            </div>
          );
        }

        const display = seg.content.slice(0, 500);
        return (
          <div key={i} className="text-[13px] leading-relaxed text-foreground whitespace-pre-wrap max-h-[200px] overflow-auto scrollbar-thin">
            {display}
            {seg.content.length > 500 && <span className="text-muted-foreground"> ...truncated</span>}
          </div>
        );
      })}
    </div>
  );
}
