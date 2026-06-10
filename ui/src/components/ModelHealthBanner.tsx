'use client';

import { useState, useEffect, useCallback } from 'react';
import Link from 'next/link';
import { useSearchParams } from 'next/navigation';
import { AlertTriangle, RefreshCw, X } from 'lucide-react';
import { fetchApi } from '@/lib/api';

interface AgentStatus {
  chat: { name: string; provider: string; model: string; connected: boolean };
  judge: { name: string; provider: string; model: string; connected: boolean; note?: string | null };
  mcp: { name: string; transport: string; connected: boolean; url?: string };
  ollama: { status: string; url: string; models: string[] };
}

interface Issue {
  id: string;
  severity: 'error' | 'warning';
  title: string;
  detail: string;
  fix?: { label: string; command: string };
}

const POLL_MS = 30_000;

function deriveIssues(a: AgentStatus): Issue[] {
  const issues: Issue[] = [];
  const ollamaDown = a.ollama.status !== 'connected';
  const needsOllama = a.chat.provider === 'ollama' || a.judge.provider === 'ollama';

  if (needsOllama && ollamaDown) {
    issues.push({
      id: 'ollama-down',
      severity: 'error',
      title: 'Ollama is not running',
      detail: 'The chat or judge agent needs Ollama to answer. Eval rubrics will fail with "All connection attempts failed" until it is up.',
      fix: { label: 'Start Ollama', command: 'ollama serve' },
    });
    return issues;
  }

  if (!a.judge.connected && a.judge.provider === 'ollama' && !ollamaDown) {
    issues.push({
      id: 'judge-model-missing',
      severity: 'warning',
      title: `Judge model "${a.judge.model}" is not installed`,
      detail: 'LLM-as-judge evals will be skipped until the model is pulled.',
      fix: { label: 'Pull judge model', command: `ollama pull ${a.judge.model}` },
    });
  }

  if (!a.chat.connected && a.chat.provider === 'ollama' && !ollamaDown) {
    issues.push({
      id: 'chat-model-missing',
      severity: 'warning',
      title: `Chat model "${a.chat.model}" is not installed`,
      detail: 'The Spool Assistant chat page will not respond until the model is pulled.',
      fix: { label: 'Pull chat model', command: `ollama pull ${a.chat.model}` },
    });
  }

  if (!a.mcp.connected) {
    issues.push({
      id: 'mcp-down',
      severity: 'warning',
      title: 'Spooling MCP server is not reachable',
      detail: 'External agents will not be able to query Spooling as an MCP context source.',
      fix: { label: 'Start MCP server', command: 'spooling mcp' },
    });
  }

  return issues;
}

// The banner surfaces dev-machine warnings (Ollama down, MCP unreachable,
// missing models). On a buyer demo it's the first red bar they see, so we
// only render it when the user opts in via `?debug=1` or
// `localStorage.spoolingDebug = '1'`. Engineers running the OSS locally can
// flip the flag once and forget. Toggle off with `?debug=0`.
function useDebugMode(): boolean {
  const params = useSearchParams();
  const [enabled, setEnabled] = useState(false);
  useEffect(() => {
    if (typeof window === 'undefined') return;
    const fromUrl = params.get('debug');
    if (fromUrl === '1') {
      window.localStorage.setItem('spoolingDebug', '1');
      setEnabled(true);
      return;
    }
    if (fromUrl === '0') {
      window.localStorage.removeItem('spoolingDebug');
      setEnabled(false);
      return;
    }
    setEnabled(window.localStorage.getItem('spoolingDebug') === '1');
  }, [params]);
  return enabled;
}

export default function ModelHealthBanner() {
  const debug = useDebugMode();
  const [issues, setIssues] = useState<Issue[]>([]);
  const [dismissed, setDismissed] = useState<Set<string>>(new Set());
  const [copied, setCopied] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);

  const check = useCallback(async () => {
    try {
      const a: AgentStatus = await fetchApi('/api/settings/agents');
      setIssues(deriveIssues(a));
    } catch {
      setIssues([
        {
          id: 'api-down',
          severity: 'error',
          title: 'Cannot reach the Spooling API',
          detail: 'The API server on :3002 is not responding. Settings, chat, and evals will not work.',
          fix: { label: 'Start Spooling', command: 'spooling ui' },
        },
      ]);
    }
    setDismissed(new Set());
  }, []);

  useEffect(() => {
    if (!debug) return;
    check();
    const id = setInterval(check, POLL_MS);
    return () => clearInterval(id);
  }, [check, debug]);

  if (!debug) return null;

  const retry = async () => {
    setRefreshing(true);
    await check();
    setTimeout(() => setRefreshing(false), 400);
  };

  const visible = issues.filter((i) => !dismissed.has(i.id));
  if (visible.length === 0) return null;

  return (
    <div className="space-y-2 mb-4">
      {visible.map((issue) => {
        const isError = issue.severity === 'error';
        return (
          <div
            key={issue.id}
            className={
              'flex items-start gap-3 rounded-lg border px-3 py-2.5 ' +
              (isError
                ? 'border-red-500/30 bg-red-500/10 text-red-700 dark:text-red-300'
                : 'border-amber-500/30 bg-amber-500/10 text-amber-700 dark:text-amber-300')
            }
          >
            <AlertTriangle className="h-4 w-4 shrink-0 mt-0.5" />
            <div className="flex-1 min-w-0">
              <div className="flex items-center gap-2 flex-wrap">
                <span className="text-[13px] font-semibold">{issue.title}</span>
                <Link
                  href="/settings"
                  className="text-[11px] underline underline-offset-2 opacity-80 hover:opacity-100"
                >
                  Settings
                </Link>
              </div>
              <p className="text-[11px] mt-0.5 leading-snug opacity-90">{issue.detail}</p>
              {issue.fix && (
                <div className="mt-1.5 flex items-center gap-2">
                  <code className="bg-black/20 dark:bg-white/10 px-1.5 py-0.5 rounded font-mono text-[11px]">
                    {issue.fix.command}
                  </code>
                  <button
                    onClick={() => {
                      navigator.clipboard.writeText(issue.fix!.command);
                      setCopied(issue.id);
                      setTimeout(() => setCopied(null), 1500);
                    }}
                    className="text-[11px] underline underline-offset-2 opacity-80 hover:opacity-100"
                  >
                    {copied === issue.id ? 'Copied!' : 'Copy'}
                  </button>
                </div>
              )}
            </div>
            <button
              onClick={retry}
              title="Recheck"
              className="p-1 rounded hover:bg-black/10 dark:hover:bg-white/10 transition-colors"
            >
              <RefreshCw className={'h-3.5 w-3.5 ' + (refreshing ? 'animate-spin' : '')} />
            </button>
            <button
              onClick={() => setDismissed((d) => new Set(d).add(issue.id))}
              title="Dismiss until next check"
              className="p-1 rounded hover:bg-black/10 dark:hover:bg-white/10 transition-colors"
            >
              <X className="h-3.5 w-3.5" />
            </button>
          </div>
        );
      })}
    </div>
  );
}
