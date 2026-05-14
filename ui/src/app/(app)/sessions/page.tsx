'use client';

import { useState, useEffect, useCallback, useMemo, useRef, memo } from 'react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { FilterSelect, type FilterOption } from '@/components/ui/filter-select';
import Link from 'next/link';
import { useSearchParams } from 'next/navigation';
import { ArrowLeft, ArrowUp, ArrowDown, Copy, Check, Search, X, ExternalLink, Activity, RefreshCw } from 'lucide-react';
import { cn } from '@/lib/utils';
import { AgGridReact } from 'ag-grid-react';
import { ModuleRegistry, AllCommunityModule, type ColDef } from 'ag-grid-community';
import { useTheme } from '@/components/ThemeProvider';
import { getGridTheme } from '@/lib/agGridTheme';
import { fetchApi, postApi, formatDate, cleanProject } from '@/lib/api';
import { toast } from 'sonner';
import { MessageContent, ToolBadges, ToolCallList, type ToolCallInfo } from '@/lib/messageParser';
import { highlightLines, getLangFromPath, type HighlightedLine } from '@/lib/highlight';

ModuleRegistry.registerModules([AllCommunityModule]);

// --- Types ---

interface Session {
  id: string;
  provider_id: string;
  project: string;
  title: string;
  started_at: string;
  message_count: number;
  tool_call_count: number;
  estimated_cost_usd: number;
  git_branch: string;
  cwd: string;
}

interface SessionMessage {
  role: string;
  content: string;
  timestamp: string;
  tools_used: string;
  estimated_tokens: number;
  tool_calls: ToolCallInfo[];
}

interface SessionDetail {
  session: Session & { estimated_input_tokens: number; estimated_output_tokens: number };
  messages: SessionMessage[];
  tool_summary: Array<{ tool_name: string; uses: number }>;
}

interface FileChange {
  file: string;
  edits: Array<{ type: 'edit' | 'write'; diff: string }>;
}

type RoleFilter = 'all' | 'user' | 'assistant';
type ContentFilter = 'all' | 'text' | 'tools';
type ViewMode = 'conversation' | 'changes';
type DiffMode = 'unified' | 'split';
type DateRange = 'all' | '24h' | '7d' | '30d';

// --- Constants ---

const PROVIDER_LABELS: Record<string, string> = {
  'claude-code': 'Claude Code',
  'codex': 'Codex CLI',
  'copilot': 'Copilot',
  'cursor': 'Cursor',
  'windsurf': 'Windsurf',
  'kiro': 'Kiro',
  'antigravity': 'Antigravity',
  'gemini': 'Gemini Code Assist',
};

const DATE_RANGES: Array<{ key: DateRange; label: string; ms: number | null }> = [
  { key: 'all', label: 'All time', ms: null },
  { key: '24h', label: '24h', ms: 86_400_000 },
  { key: '7d', label: '7 days', ms: 604_800_000 },
  { key: '30d', label: '30 days', ms: 2_592_000_000 },
];

const IDE_OPTIONS = [
  { key: 'cursor', label: 'Cursor', scheme: 'cursor://file' },
  { key: 'zed', label: 'Zed', scheme: 'zed://file' },
  { key: 'vscode', label: 'VS Code', scheme: 'vscode://file' },
  { key: 'windsurf', label: 'Windsurf', scheme: 'windsurf://file' },
] as const;

const TOOL_MARKER_RE = /\[tool: [^\]]+\]\n?/g;

const AG_GRID_DEFAULT_COL_DEF = {
  flex: 1,
  minWidth: 80,
  resizable: true,
  suppressHeaderMenuButton: true,
  suppressHeaderFilterButton: false,
} as const;

// --- Helpers ---

function getMessageText(m: SessionMessage): string {
  return (m.content || '').replace(TOOL_MARKER_RE, '').trim();
}

function getMessageToolNames(m: SessionMessage): string[] {
  if (m.tool_calls?.length) return m.tool_calls.map(tc => tc.name);
  try {
    const tools = typeof m.tools_used === 'string' ? JSON.parse(m.tools_used) : m.tools_used;
    return Array.isArray(tools) ? tools : [];
  } catch { return []; }
}

function getLegacyTools(m: SessionMessage): string[] | null {
  if (m.tool_calls?.length) return null;
  try {
    const tools = typeof m.tools_used === 'string' ? JSON.parse(m.tools_used) : m.tools_used;
    return Array.isArray(tools) && tools.length > 0 ? tools : null;
  } catch { return null; }
}

function buildFileChanges(messages: SessionMessage[]): FileChange[] {
  const byFile: Record<string, FileChange> = {};
  for (const m of messages) {
    if (!m.tool_calls) continue;
    for (const tc of m.tool_calls) {
      if (tc.name !== 'Edit' && tc.name !== 'Write') continue;
      if (!tc.input || !tc.result_preview) continue;
      const file = tc.input;
      if (!byFile[file]) byFile[file] = { file, edits: [] };
      byFile[file].edits.push({
        type: tc.name === 'Edit' ? 'edit' : 'write',
        diff: tc.result_preview,
      });
    }
  }
  return Object.values(byFile);
}

function shortPath(fullPath: string): string {
  const parts = fullPath.split('/');
  return parts.length > 3 ? parts.slice(-3).join('/') : fullPath;
}

function parseDiffParts(diff: string): { removed: string[]; added: string[] } {
  const removed: string[] = [];
  const added: string[] = [];
  for (const line of diff.split('\n')) {
    if (line.startsWith('-')) removed.push(line.slice(1));
    else if (line.startsWith('+')) added.push(line.slice(1));
  }
  return { removed, added };
}

// --- Reusable toggle component ---

function SegmentedToggle<T extends string>({ options, value, onChange, size = 'sm' }: {
  options: Array<{ key: T; label: string }>;
  value: T;
  onChange: (key: T) => void;
  size?: 'sm' | 'md';
}) {
  const textSize = size === 'sm' ? 'text-[11px]' : 'text-[12px]';
  const pad = size === 'sm' ? 'px-2 py-0.5' : 'px-2.5 py-0.5';
  return (
    <div className={`flex items-center rounded-md border bg-card p-0.5 ${textSize}`}>
      {options.map(o => (
        <button
          key={o.key}
          onClick={() => onChange(o.key)}
          className={cn(
            `${pad} rounded transition-colors`,
            value === o.key
              ? 'bg-accent text-foreground font-medium'
              : 'text-muted-foreground hover:text-foreground'
          )}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}

// --- IDE hook ---

function usePreferredIde() {
  const [ide, setIde] = useState<string | null>(() => {
    if (typeof window === 'undefined') return null;
    return localStorage.getItem('spool-preferred-ide');
  });
  const save = useCallback((key: string) => {
    localStorage.setItem('spool-preferred-ide', key);
    setIde(key);
  }, []);
  const option = useMemo(() => IDE_OPTIONS.find(o => o.key === ide) || null, [ide]);
  return { option, save };
}

// --- Syntax highlighting hook ---

function useHighlightedLines(lines: string[], filePath: string): HighlightedLine[] | null {
  const [highlighted, setHighlighted] = useState<HighlightedLine[] | null>(null);
  const lang = useMemo(() => getLangFromPath(filePath), [filePath]);
  const linesRef = useRef(lines);
  const linesKey = useMemo(() => lines.join('\n'), [lines]);

  useEffect(() => {
    linesRef.current = lines;
  }, [lines]);

  useEffect(() => {
    if (!lang) return;
    let cancelled = false;
    highlightLines(linesRef.current, lang).then(result => {
      if (!cancelled) setHighlighted(result);
    });
    return () => { cancelled = true; };
  }, [linesKey, lang]);

  return lang ? highlighted : null;
}

function HighlightedSpan({ line }: { line: HighlightedLine | null }) {
  if (!line) return null;
  return (
    <>
      {line.tokens.map((t, j) => (
        t.color
          ? <span key={j} style={{ color: t.color }}>{t.content}</span>
          : <span key={j}>{t.content}</span>
      ))}
    </>
  );
}

// --- Diff components ---

const UnifiedDiff = memo(function UnifiedDiff({ diff, filePath }: { diff: string; filePath: string }) {
  const parsed = useMemo(() => {
    return diff.split('\n').map(line => {
      const isAdd = line.startsWith('+');
      const isDel = line.startsWith('-');
      const isCtx = line.startsWith(' ');
      return {
        type: isAdd ? 'add' as const : isDel ? 'del' as const : 'ctx' as const,
        content: (isAdd || isDel || isCtx) ? line.slice(1) : line,
      };
    });
  }, [diff]);

  const plainLines = useMemo(() => parsed.map(l => l.content), [parsed]);
  const highlighted = useHighlightedLines(plainLines, filePath);

  return (
    <div className="overflow-x-auto scrollbar-thin">
      <pre className="text-[11px] font-mono leading-relaxed whitespace-pre pb-2">
        {parsed.map((line, i) => (
          <div
            key={i}
            className={cn(
              'px-2 -mx-1',
              line.type === 'add' && 'bg-emerald-500/10',
              line.type === 'del' && 'bg-red-500/10',
            )}
          >
            <span className={cn(
              'inline-block w-3 shrink-0 select-none',
              line.type === 'add' ? 'text-emerald-600 dark:text-emerald-500' : line.type === 'del' ? 'text-red-600 dark:text-red-500' : 'text-muted-foreground/30',
            )}>
              {line.type === 'add' ? '+' : line.type === 'del' ? '-' : ' '}
            </span>
            {highlighted ? (
              <HighlightedSpan line={highlighted[i]} />
            ) : (
              <span className={line.type === 'ctx' ? 'text-muted-foreground' : undefined}>{line.content}</span>
            )}
          </div>
        ))}
      </pre>
    </div>
  );
});

const SplitDiff = memo(function SplitDiff({ diff, filePath }: { diff: string; filePath: string }) {
  const { removed, added } = useMemo(() => parseDiffParts(diff), [diff]);
  const highlightedRemoved = useHighlightedLines(removed, filePath);
  const highlightedAdded = useHighlightedLines(added, filePath);

  return (
    <div className="grid grid-cols-2 gap-0 divide-x">
      <div>
        <div className="text-[9px] uppercase tracking-wider text-red-500/70 font-semibold px-2 py-1">Before</div>
        <div className="overflow-x-auto scrollbar-thin">
          <pre className="text-[11px] font-mono leading-relaxed whitespace-pre pb-2">
            {removed.map((line, i) => (
              <div key={i} className="px-2 bg-red-500/8">
                {highlightedRemoved ? <HighlightedSpan line={highlightedRemoved[i]} /> : <span className="text-red-700 dark:text-red-400">{line}</span>}
              </div>
            ))}
            {removed.length === 0 && <div className="px-2 text-muted-foreground/40 italic">empty</div>}
          </pre>
        </div>
      </div>
      <div>
        <div className="text-[9px] uppercase tracking-wider text-emerald-500/70 font-semibold px-2 py-1">After</div>
        <div className="overflow-x-auto scrollbar-thin">
          <pre className="text-[11px] font-mono leading-relaxed whitespace-pre pb-2">
            {added.map((line, i) => (
              <div key={i} className="px-2 bg-emerald-500/8">
                {highlightedAdded ? <HighlightedSpan line={highlightedAdded[i]} /> : <span className="text-emerald-700 dark:text-emerald-400">{line}</span>}
              </div>
            ))}
            {added.length === 0 && <div className="px-2 text-muted-foreground/40 italic">empty</div>}
          </pre>
        </div>
      </div>
    </div>
  );
});

function DiffBlock({ diff, mode, filePath }: { diff: string; mode: DiffMode; filePath: string }) {
  if (mode === 'split') return <SplitDiff diff={diff} filePath={filePath} />;
  return <UnifiedDiff diff={diff} filePath={filePath} />;
}

// --- Open in IDE ---

function OpenInIdeButton({ filePath }: { filePath: string }) {
  const { option, save } = usePreferredIde();
  const [showPicker, setShowPicker] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!showPicker) return;
    const close = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setShowPicker(false);
    };
    document.addEventListener('mousedown', close);
    return () => document.removeEventListener('mousedown', close);
  }, [showPicker]);

  const handleClick = useCallback((e: React.MouseEvent) => {
    e.stopPropagation();
    if (option) {
      window.open(`${option.scheme}${filePath}`, '_self');
    } else {
      setShowPicker(true);
    }
  }, [option, filePath]);

  const pick = useCallback((key: string) => {
    const opt = IDE_OPTIONS.find(o => o.key === key)!;
    save(key);
    setShowPicker(false);
    window.open(`${opt.scheme}${filePath}`, '_self');
  }, [save, filePath]);

  return (
    <div ref={ref} className="relative">
      <button
        onClick={handleClick}
        className="flex items-center gap-1 px-2 py-1 rounded-md text-[10px] font-medium text-muted-foreground hover:text-foreground bg-secondary hover:bg-accent transition-colors border border-border shadow-sm"
        title={option ? `Open in ${option.label}` : 'Open in editor'}
      >
        <ExternalLink className="h-3 w-3" />
      </button>
      {showPicker && (
        <div className="absolute right-0 top-full mt-1 z-20 bg-card border rounded-md shadow-lg py-1 min-w-[120px]">
          {IDE_OPTIONS.map(opt => (
            <button
              key={opt.key}
              onClick={(e) => { e.stopPropagation(); pick(opt.key); }}
              className="w-full text-left px-3 py-1.5 text-[11px] hover:bg-accent transition-colors"
            >
              {opt.label}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

// --- Changes view ---

function ChangesView({ messages, diffMode }: { messages: SessionMessage[]; diffMode: DiffMode }) {
  const fileChanges = useMemo(() => buildFileChanges(messages), [messages]);
  const [expandedFiles, setExpandedFiles] = useState<Set<string>>(() => new Set(fileChanges.map(f => f.file)));

  const toggleFile = useCallback((file: string) => {
    setExpandedFiles(prev => {
      const next = new Set(prev);
      if (next.has(file)) next.delete(file); else next.add(file);
      return next;
    });
  }, []);

  if (fileChanges.length === 0) {
    return <p className="text-[12px] text-muted-foreground text-center py-8">No file changes in this session.</p>;
  }

  return (
    <div className="space-y-2">
      {fileChanges.map(fc => (
        <div key={fc.file} className="rounded-md border overflow-hidden">
          <div className="flex items-center gap-2 px-3 py-2 bg-muted/30">
            <button
              onClick={() => toggleFile(fc.file)}
              className="flex items-center gap-2 flex-1 min-w-0 text-left hover:text-primary transition-colors"
            >
              <span className="text-[12px] font-mono font-medium text-foreground truncate" title={fc.file}>{shortPath(fc.file)}</span>
              <span className="text-[10px] text-muted-foreground tabular-nums shrink-0">{fc.edits.length} {fc.edits.length === 1 ? 'change' : 'changes'}</span>
            </button>
            <OpenInIdeButton filePath={fc.file} />
          </div>
          {expandedFiles.has(fc.file) && (
            <div className="divide-y">
              {fc.edits.map((edit, i) => (
                <div key={i} className="px-3 py-2">
                  <DiffBlock diff={edit.diff} mode={diffMode} filePath={fc.file} />
                </div>
              ))}
            </div>
          )}
        </div>
      ))}
    </div>
  );
}

// --- Message item ---

const MessageItem = memo(function MessageItem({ m }: { m: SessionMessage }) {
  const legacyTools = getLegacyTools(m);
  return (
    <div className={`p-3 rounded-md border-l-2 ${
      m.role === 'user'
        ? 'bg-primary/5 border-l-primary'
        : 'bg-secondary/50 border-l-border'
    }`}>
      <div className="flex items-center justify-between mb-1">
        <div className="flex items-center gap-2">
          <span className={`text-[11px] font-semibold uppercase tracking-wider ${m.role === 'user' ? 'text-primary' : 'text-muted-foreground'}`}>
            {m.role}
          </span>
          {legacyTools && <ToolBadges tools={legacyTools} />}
        </div>
        <span className="text-[11px] text-muted-foreground tabular-nums">
          {m.timestamp ? new Date(m.timestamp).toLocaleTimeString() : ''}
        </span>
      </div>
      <MessageContent content={m.content || ''} />
      {m.tool_calls?.length > 0 && <ToolCallList toolCalls={m.tool_calls} />}
    </div>
  );
});

// --- Session detail view ---

function ConversationView({ messages, toolSummary }: { messages: SessionMessage[]; toolSummary: Array<{ tool_name: string; uses: number }> }) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const topRef = useRef<HTMLDivElement>(null);
  const bottomRef = useRef<HTMLDivElement>(null);

  const [viewMode, setViewMode] = useState<ViewMode>('conversation');
  const [diffMode, setDiffMode] = useState<DiffMode>('unified');
  const [roleFilter, setRoleFilter] = useState<RoleFilter>('all');
  const [contentFilter, setContentFilter] = useState<ContentFilter>('all');
  const [toolFilter, setToolFilter] = useState<string | null>(null);
  const [searchQuery, setSearchQuery] = useState('');

  const toolCounts = useMemo(() => {
    if (toolSummary.length > 0) return [...toolSummary].sort((a, b) => b.uses - a.uses);
    const counts: Record<string, number> = {};
    for (const m of messages) {
      for (const t of getMessageToolNames(m)) counts[t] = (counts[t] || 0) + 1;
    }
    return Object.entries(counts)
      .sort(([, a], [, b]) => b - a)
      .map(([tool_name, uses]) => ({ tool_name, uses }));
  }, [messages, toolSummary]);

  const filtered = useMemo(() => {
    const q = searchQuery.toLowerCase();
    return messages.filter(m => {
      if (roleFilter !== 'all' && m.role !== roleFilter) return false;

      const text = getMessageText(m);
      const tools = getMessageToolNames(m);
      const hasText = text.length > 0 && !text.startsWith('<system-reminder>') && !text.startsWith('<local-command');
      const hasTools = tools.length > 0;

      if (contentFilter === 'text' && !hasText) return false;
      if (contentFilter === 'tools' && !hasTools) return false;
      if (toolFilter && !tools.includes(toolFilter)) return false;

      if (q) {
        const tcParts = (m.tool_calls || []).flatMap(tc => [tc.input || '', tc.result_preview || '']);
        const haystack = [text, ...tools, ...tcParts].join(' ').toLowerCase();
        if (!haystack.includes(q)) return false;
      }

      return true;
    });
  }, [messages, roleFilter, contentFilter, toolFilter, searchQuery]);

  const fileChangeCount = useMemo(() => buildFileChanges(messages).length, [messages]);
  const hasActiveFilters = roleFilter !== 'all' || contentFilter !== 'all' || toolFilter !== null || searchQuery !== '';

  const clearFilters = useCallback(() => {
    setRoleFilter('all');
    setContentFilter('all');
    setToolFilter(null);
    setSearchQuery('');
  }, []);

  const scrollTo = useCallback((target: 'top' | 'bottom') => {
    const el = target === 'top' ? topRef.current : bottomRef.current;
    el?.scrollIntoView({ behavior: 'smooth' });
  }, []);

  return (
    <div className="relative">
      {/* Header */}
      <div className="flex items-center justify-between mb-2">
        <div className="flex items-center gap-3">
          <SegmentedToggle
            options={[{ key: 'conversation' as ViewMode, label: 'Conversation' }, { key: 'changes' as ViewMode, label: 'Changes' }]}
            value={viewMode}
            onChange={setViewMode}
            size="md"
          />
          {viewMode === 'conversation' && (
            <span className="text-[11px] text-muted-foreground tabular-nums">
              {hasActiveFilters ? `${filtered.length} of ${messages.length}` : messages.length}
            </span>
          )}
          {viewMode === 'changes' && (
            <>
              <SegmentedToggle
                options={[{ key: 'unified' as DiffMode, label: 'Unified' }, { key: 'split' as DiffMode, label: 'Split' }]}
                value={diffMode}
                onChange={setDiffMode}
              />
              <span className="text-[11px] text-muted-foreground tabular-nums">
                {fileChangeCount} files changed
              </span>
            </>
          )}
        </div>
        <div className="flex items-center gap-1">
          {viewMode === 'conversation' && messages.length > 20 && (
            <>
              <Button variant="ghost" size="sm" className="h-6 w-6 p-0" onClick={() => scrollTo('top')} title="Jump to top">
                <ArrowUp className="h-3.5 w-3.5" />
              </Button>
              <Button variant="ghost" size="sm" className="h-6 w-6 p-0" onClick={() => scrollTo('bottom')} title="Jump to bottom">
                <ArrowDown className="h-3.5 w-3.5" />
              </Button>
            </>
          )}
        </div>
      </div>

      {/* Filter bar (conversation mode only) */}
      {viewMode === 'conversation' && (
        <div className="flex flex-wrap items-center gap-2 mb-2">
          <div className="relative flex-1 min-w-[180px] max-w-xs">
            <div className="absolute left-2.5 top-1/2 -translate-y-1/2 pointer-events-none">
              <Search size={13} strokeWidth={2.5} className="text-muted-foreground" />
            </div>
            <Input
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              placeholder="Search messages, tools, results..."
              className="pl-8 h-7 text-[12px]"
            />
          </div>

          <SegmentedToggle
            options={[{ key: 'all', label: 'All' }, { key: 'user', label: 'User' }, { key: 'assistant', label: 'Assistant' }]}
            value={roleFilter}
            onChange={setRoleFilter}
          />

          <SegmentedToggle
            options={[{ key: 'all', label: 'All' }, { key: 'text', label: 'Text' }, { key: 'tools', label: 'Tools' }]}
            value={contentFilter}
            onChange={setContentFilter}
          />

          {toolCounts.length > 0 && (
            <div className="flex items-center gap-1 flex-wrap">
              {toolCounts.map(t => (
                <button
                  key={t.tool_name}
                  onClick={() => setToolFilter(toolFilter === t.tool_name ? null : t.tool_name)}
                  className={cn(
                    'inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] font-mono font-medium transition-colors border',
                    toolFilter === t.tool_name
                      ? 'bg-primary/15 text-primary border-primary/30'
                      : 'text-muted-foreground border-transparent hover:text-foreground hover:bg-accent'
                  )}
                >
                  {t.tool_name}
                  <span className={cn(
                    'tabular-nums text-[9px]',
                    toolFilter === t.tool_name ? 'text-primary/70' : 'text-muted-foreground/60'
                  )}>
                    {t.uses}
                  </span>
                </button>
              ))}
            </div>
          )}

          {hasActiveFilters && (
            <Button variant="ghost" size="sm" onClick={clearFilters} className="h-6 text-[11px] text-muted-foreground px-2">
              <X className="h-3 w-3 mr-1" /> Clear
            </Button>
          )}
        </div>
      )}

      {/* Conversation view */}
      <div ref={scrollRef} className={cn("max-h-[calc(100vh-150px)] overflow-y-auto scrollbar-thin pr-1 rounded-lg border bg-card p-3", viewMode !== 'conversation' && 'hidden')}>
        <div ref={topRef} />
        <div className="space-y-1.5">
          {filtered.length === 0 ? (
            <p className="text-[12px] text-muted-foreground text-center py-8">No messages match your filters.</p>
          ) : (
            filtered.map((m, i) => <MessageItem key={i} m={m} />)
          )}
        </div>
        <div ref={bottomRef} />
      </div>

      {/* Changes view */}
      <div className={cn("max-h-[calc(100vh-150px)] overflow-y-auto scrollbar-thin pr-1 rounded-lg border bg-card p-3", viewMode !== 'changes' && 'hidden')}>
        <ChangesView messages={messages} diffMode={diffMode} />
      </div>
    </div>
  );
}

// --- Main page ---

export default function SessionsPage() {
  const { resolved } = useTheme();
  const [loading, setLoading] = useState(true);
  const [sessions, setSessions] = useState<Session[]>([]);
  const [selected, setSelected] = useState<SessionDetail | null>(null);
  const [copied, setCopied] = useState(false);

  const [search, setSearch] = useState('');
  const [providerFilter, setProviderFilter] = useState<string | null>(null);
  const [projectFilter, setProjectFilter] = useState<string | null>(null);
  const [dateRange, setDateRange] = useState<DateRange>('all');
  const [resyncing, setResyncing] = useState(false);

  const searchParams = useSearchParams();
  const projectParam = searchParams.get('project');
  useEffect(() => { setProjectFilter(projectParam || null); }, [projectParam]);

  const copyId = useCallback((id: string) => {
    navigator.clipboard.writeText(id);
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  }, []);

  const fetchSessions = useCallback(async () => {
    try {
      setSessions(await fetchApi('/api/sessions?limit=100'));
    } catch (e) { console.error(e); }
    finally { setLoading(false); }
  }, []);

  useEffect(() => { fetchSessions(); }, [fetchSessions]);

  const resync = useCallback(async () => {
    setResyncing(true);
    const t = toast.loading('Syncing all providers...');
    try {
      await postApi('/api/sync', { embed: false });
      await fetchSessions();
      toast.success('Sessions refreshed', { id: t });
    } catch (e) {
      console.error(e);
      toast.error('Failed to sync', { id: t });
    }
    finally { setResyncing(false); }
  }, [fetchSessions]);

  const openSession = useCallback(async (id: string) => {
    try { setSelected(await fetchApi(`/api/session/${id}`)); }
    catch (e) { console.error(e); }
  }, []);

  const gridTheme = useMemo(() => getGridTheme(resolved), [resolved]);

  const availableProviders = useMemo(() => {
    const set = new Set<string>();
    sessions.forEach(s => s.provider_id && set.add(s.provider_id));
    return Array.from(set);
  }, [sessions]);

  const availableProjects = useMemo(() => {
    const counts = new Map<string, number>();
    sessions.forEach((s) => {
      const p = s.project || '';
      if (!p) return;
      counts.set(p, (counts.get(p) || 0) + 1);
    });
    return Array.from(counts.entries())
      .sort((a, b) => b[1] - a[1])
      .map(([project, n]) => ({ project, count: n }));
  }, [sessions]);

  const filteredSessions = useMemo(() => {
    const q = search.trim().toLowerCase();
    const cutoff = DATE_RANGES.find(r => r.key === dateRange)?.ms;
    const since = cutoff ? Date.now() - cutoff : null;
    return sessions.filter(s => {
      if (providerFilter && s.provider_id !== providerFilter) return false;
      if (projectFilter && s.project !== projectFilter) return false;
      if (since && s.started_at && new Date(s.started_at).getTime() < since) return false;
      if (q) {
        const hay = `${s.title || ''} ${s.project || ''} ${s.git_branch || ''} ${s.id}`.toLowerCase();
        if (!hay.includes(q)) return false;
      }
      return true;
    });
  }, [sessions, search, providerFilter, projectFilter, dateRange]);

  const hasActiveFilters = search !== '' || providerFilter !== null || projectFilter !== null || dateRange !== 'all';
  const clearFilters = useCallback(() => { setSearch(''); setProviderFilter(null); setProjectFilter(null); setDateRange('all'); }, []);

  const columnDefs = useMemo<ColDef<Session>[]>(() => [
    { field: 'title', headerName: 'Session', sortable: true, filter: 'agTextColumnFilter', flex: 2, minWidth: 240, valueFormatter: (p) => (p.value || 'Untitled').slice(0, 80), tooltipValueGetter: (p) => p.value || 'Untitled' },
    { field: 'id', headerName: 'Session ID', sortable: true, filter: 'agTextColumnFilter', flex: 1, minWidth: 130, cellClass: 'font-mono', tooltipValueGetter: (p) => p.value },
    { field: 'provider_id', headerName: 'Provider', sortable: true, filter: 'agTextColumnFilter', flex: 1, minWidth: 110, valueFormatter: (p) => PROVIDER_LABELS[p.value as string] || p.value },
    { field: 'project', headerName: 'Project', sortable: true, filter: 'agTextColumnFilter', flex: 1.3, minWidth: 140, valueFormatter: (p) => cleanProject(p.value || '') },
    { field: 'git_branch', headerName: 'Branch', sortable: true, filter: 'agTextColumnFilter', flex: 1, minWidth: 110, valueFormatter: (p) => p.value || '—' },
    { field: 'started_at', headerName: 'Started', sortable: true, filter: 'agDateColumnFilter', flex: 1, minWidth: 130, sort: 'desc', valueFormatter: (p) => p.value ? formatDate(p.value) : '', filterValueGetter: (p) => p.data?.started_at ? new Date(p.data.started_at) : null },
    { field: 'message_count', headerName: 'Msgs', sortable: true, filter: 'agNumberColumnFilter', type: 'rightAligned', flex: 0.6, minWidth: 80 },
    { field: 'tool_call_count', headerName: 'Tools', sortable: true, filter: 'agNumberColumnFilter', type: 'rightAligned', flex: 0.6, minWidth: 80 },
  ], []);

  if (loading) {
    return <div className="flex items-center justify-center h-64">
      <div className="animate-spin rounded-full h-6 w-6 border-2 border-primary border-t-transparent" />
    </div>;
  }

  if (selected) {
    const sess = selected.session;
    return (
      <div className="space-y-2">
        <div className="flex items-center gap-3 min-w-0">
          <Button variant="ghost" size="sm" className="h-8 px-2 shrink-0" onClick={() => setSelected(null)}>
            <ArrowLeft className="h-4 w-4" />
          </Button>
          <button
            onClick={() => copyId(sess.id)}
            className="flex items-center gap-1.5 text-base font-semibold font-mono truncate min-w-0 hover:text-primary transition-colors"
            title="Copy session ID"
          >
            {sess.id}
            {copied ? <Check className="h-3.5 w-3.5 text-emerald-500 shrink-0" /> : <Copy className="h-3.5 w-3.5 text-muted-foreground shrink-0" />}
          </button>
          <div className="flex items-center gap-1 shrink-0 ml-auto">
            {[
              cleanProject(sess.project || ''),
              sess.git_branch || null,
              formatDate(sess.started_at),
              `${sess.message_count} msgs`,
              `${sess.tool_call_count} tools`,
            ].filter(Boolean).map((val, i) => (
              <span key={i} className="text-[12px] text-muted-foreground px-1.5 py-0.5 rounded bg-secondary/50">
                {val}
              </span>
            ))}
            <Link
              href={`/traces?session=${sess.id}`}
              className="ml-1 inline-flex items-center gap-1 text-[12px] font-medium text-primary hover:text-primary/80 px-2 py-0.5 rounded border border-primary/30 bg-primary/5 hover:bg-primary/10 transition-colors"
              title="Open this session's trace"
            >
              <Activity className="h-3 w-3" />
              View trace
            </Link>
          </div>
        </div>

        <ConversationView messages={selected.messages} toolSummary={selected.tool_summary} />
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-lg font-semibold tracking-tight">Sessions</h1>
        <div className="flex items-center gap-3">
          <span className="text-[11px] text-muted-foreground tabular-nums">
            {filteredSessions.length} of {sessions.length}
          </span>
          <Button variant="outline" size="sm" onClick={resync} disabled={resyncing}>
            <RefreshCw className={`h-3.5 w-3.5 mr-1.5 ${resyncing ? 'animate-spin' : ''}`} />
            {resyncing ? 'Syncing...' : 'Resync'}
          </Button>
        </div>
      </div>

      {/* Filter toolbar */}
      <div className="flex flex-wrap items-center gap-2">
        <div className="relative flex-1 min-w-[220px] max-w-md">
          <div className="absolute left-3 top-1/2 -translate-y-1/2 pointer-events-none z-10">
            <Search size={16} strokeWidth={2.25} className="text-muted-foreground" />
          </div>
          <Input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search title, project, branch, id..."
            className="pl-9 h-9 text-[13px]"
          />
        </div>

        {availableProviders.length > 1 && (
          <div className="flex items-center rounded-md border bg-card p-0.5 text-[12px]">
            <button
              onClick={() => setProviderFilter(null)}
              className={cn(
                'px-2.5 py-1 rounded transition-colors',
                providerFilter === null
                  ? 'bg-accent text-foreground font-medium'
                  : 'text-muted-foreground hover:text-foreground'
              )}
            >
              All
            </button>
            {availableProviders.map(p => (
              <button
                key={p}
                onClick={() => setProviderFilter(providerFilter === p ? null : p)}
                className={cn(
                  'px-2.5 py-1 rounded transition-colors',
                  providerFilter === p
                    ? 'bg-accent text-foreground font-medium'
                    : 'text-muted-foreground hover:text-foreground'
                )}
              >
                {PROVIDER_LABELS[p] || p}
              </button>
            ))}
          </div>
        )}

        {availableProjects.length > 1 && (
          <FilterSelect
            label="Project"
            value={projectFilter ?? 'all'}
            onChange={(v) => setProjectFilter(v === 'all' ? null : v)}
            options={[
              { value: 'all', label: 'All projects' } as FilterOption,
              ...availableProjects.map((p) => ({
                value: p.project,
                label: cleanProject(p.project),
                hint: String(p.count),
              })),
            ]}
          />
        )}

        <SegmentedToggle
          options={DATE_RANGES.map(r => ({ key: r.key, label: r.label }))}
          value={dateRange}
          onChange={setDateRange}
          size="md"
        />

        {hasActiveFilters && (
          <Button variant="ghost" size="sm" onClick={clearFilters} className="h-8 text-[12px] text-muted-foreground">
            <X className="h-3 w-3 mr-1" /> Clear
          </Button>
        )}
      </div>

      <div style={{ height: 'calc(100vh - 220px)', minHeight: 480, width: '100%' }}>
        <AgGridReact<Session>
          theme={gridTheme}
          columnDefs={columnDefs}
          rowData={filteredSessions}
          headerHeight={36}
          rowHeight={34}
          suppressMovableColumns
          defaultColDef={AG_GRID_DEFAULT_COL_DEF}
          onRowClicked={(e) => e.data && openSession(e.data.id)}
          rowClass="cursor-pointer"
          overlayNoRowsTemplate='<span style="color: hsl(var(--muted-foreground))">No sessions match your filters.</span>'
        />
      </div>
    </div>
  );
}
