'use client';

import { useMemo, useState } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { ChevronDown, ChevronRight, Wrench, AlertCircle, CheckCircle2 } from 'lucide-react';
import { cn } from '@/lib/utils';
import { Badge } from '@/components/ui/badge';
import type { Span } from '@/components/SpanTree';

interface Message {
  role: string;
  content: string;
  timestamp: string | null;
  tools_used: string | string[] | null;
  estimated_tokens: number;
}

interface Props {
  messages: Message[];
  spans: Span[];
}

function parseTime(t: string | null): number {
  if (!t) return 0;
  const n = new Date(t).getTime();
  return Number.isFinite(n) ? n : 0;
}

// For each assistant message, find the tool/agent spans that executed
// between its timestamp and the next user message's timestamp. Those are
// the "thinking" steps to show under that bubble.
function attachSpansToAssistantTurns(messages: Message[], spans: Span[]) {
  const assistantSpans: Span[][] = messages.map(() => []);
  const toolSpans = spans
    .filter((s) => s.kind === 'tool' || s.kind === 'agent')
    .sort((a, b) => a.sequence - b.sequence);

  const turns: Array<{ index: number; start: number; end: number }> = [];
  for (let i = 0; i < messages.length; i++) {
    if (messages[i].role !== 'assistant') continue;
    const start = parseTime(messages[i].timestamp);
    let end = Infinity;
    for (let j = i + 1; j < messages.length; j++) {
      if (messages[j].role === 'user') {
        end = parseTime(messages[j].timestamp) || Infinity;
        break;
      }
    }
    turns.push({ index: i, start, end });
  }

  for (const ts of toolSpans) {
    const t = parseTime(ts.started_at);
    const turn = turns.find((tu) => t >= tu.start && t < tu.end);
    if (turn) {
      assistantSpans[turn.index].push(ts);
    }
  }
  return assistantSpans;
}

function ThinkingPanel({ spans }: { spans: Span[] }) {
  const [open, setOpen] = useState(false);
  if (!spans.length) return null;

  const grouped = useMemo(() => {
    const map = new Map<string, { count: number; errors: number; spans: Span[] }>();
    for (const s of spans) {
      const key = s.tool_name || s.agent_type || s.name;
      const cur = map.get(key) || { count: 0, errors: 0, spans: [] };
      cur.count += 1;
      if (s.tool_is_error || s.status === 'error') cur.errors += 1;
      cur.spans.push(s);
      map.set(key, cur);
    }
    return Array.from(map.entries())
      .map(([key, v]) => ({ key, ...v }))
      .sort((a, b) => b.count - a.count);
  }, [spans]);

  return (
    <div className="mt-2 border-t border-border/60 pt-2">
      <button
        onClick={() => setOpen(!open)}
        className="flex items-center gap-1.5 text-[11px] text-muted-foreground hover:text-foreground transition-colors"
      >
        {open ? <ChevronDown className="h-3 w-3" /> : <ChevronRight className="h-3 w-3" />}
        <Wrench className="h-3 w-3" />
        <span>
          Thinking · {spans.length} step{spans.length === 1 ? '' : 's'}
          {grouped.slice(0, 4).map((g) => (
            <span key={g.key} className="ml-1.5 inline-flex items-center gap-0.5">
              <span className="text-muted-foreground/60">·</span>
              <span>{g.key}</span>
              {g.count > 1 && <span className="text-muted-foreground/60">×{g.count}</span>}
            </span>
          ))}
          {grouped.length > 4 && <span className="ml-1 text-muted-foreground/60">+{grouped.length - 4} more</span>}
        </span>
      </button>
      {open && (
        <div className="mt-2 space-y-1 max-h-64 overflow-auto scrollbar-thin">
          {spans
            .sort((a, b) => a.sequence - b.sequence)
            .map((s) => {
              const isErr = s.tool_is_error || s.status === 'error';
              const label = s.tool_name || s.agent_type || s.name;
              return (
                <div
                  key={s.id}
                  className={cn(
                    'flex items-start gap-2 px-2 py-1.5 rounded text-[11px]',
                    isErr ? 'bg-destructive/5' : 'bg-secondary/40'
                  )}
                >
                  {isErr ? (
                    <AlertCircle className="h-3 w-3 text-destructive shrink-0 mt-0.5" />
                  ) : (
                    <CheckCircle2 className="h-3 w-3 text-emerald-500 shrink-0 mt-0.5" />
                  )}
                  <div className="min-w-0 flex-1">
                    <div className="font-medium truncate">
                      {s.kind === 'agent' ? `agent:${label}` : `tool:${label}`}
                      {s.vendor && s.vendor !== 'filesystem' && s.vendor !== 'shell' && s.vendor !== 'search' && s.vendor !== 'unknown' && (
                        <Badge variant="outline" className="ml-1.5 text-[10px]">
                          {s.vendor}
                        </Badge>
                      )}
                    </div>
                    {!!s.tool_input && (
                    <pre className="mt-0.5 text-[10px] text-muted-foreground/80 truncate whitespace-pre">
                      {JSON.stringify(s.tool_input).slice(0, 140)}
                    </pre>
                  )}
                  {s.tool_output != null && (
                      <pre className="mt-0.5 text-[10px] text-muted-foreground/80 whitespace-pre-wrap max-h-20 overflow-auto">
                        {String(s.tool_output).slice(0, 300)}
                      </pre>
                    )}
                  </div>
                </div>
              );
            })}
        </div>
      )}
    </div>
  );
}

export default function TraceConversation({ messages, spans }: Props) {
  const assistantSpans = useMemo(
    () => attachSpansToAssistantTurns(messages, spans),
    [messages, spans],
  );

  if (!messages.length) {
    return <p className="text-[12px] text-muted-foreground p-6 text-center">No messages in this trace.</p>;
  }

  return (
    <div className="space-y-3 p-1 max-h-[70vh] overflow-auto scrollbar-thin">
      {messages.map((m, i) => {
        const isUser = m.role === 'user';
        const turnSpans = assistantSpans[i] || [];
        return (
          <div key={i} className={cn('flex', isUser ? 'justify-end' : 'justify-start')}>
            <div
              className={cn(
                'max-w-[85%] rounded-lg px-3 py-2 text-[13px]',
                isUser
                  ? 'bg-primary text-primary-foreground'
                  : 'bg-secondary/60 border border-border text-foreground'
              )}
            >
              <div className="flex items-center gap-2 mb-1">
                <span className={cn(
                  'text-[10px] uppercase tracking-wider font-semibold',
                  isUser ? 'text-primary-foreground/70' : 'text-muted-foreground'
                )}>
                  {m.role}
                </span>
                {m.timestamp && (
                  <span className={cn(
                    'text-[10px] tabular-nums',
                    isUser ? 'text-primary-foreground/60' : 'text-muted-foreground/70'
                  )}>
                    {new Date(m.timestamp).toLocaleTimeString()}
                  </span>
                )}
              </div>
              {isUser ? (
                <div className="whitespace-pre-wrap break-words">{m.content}</div>
              ) : (
                <div className="prose prose-sm max-w-none dark:prose-invert prose-p:my-1 prose-pre:my-1 prose-pre:text-[11px] prose-code:text-[11px]">
                  <ReactMarkdown remarkPlugins={[remarkGfm]}>
                    {m.content || ''}
                  </ReactMarkdown>
                </div>
              )}
              {!isUser && turnSpans.length > 0 && <ThinkingPanel spans={turnSpans} />}
            </div>
          </div>
        );
      })}
    </div>
  );
}
