'use client';

import { usePathname } from 'next/navigation';
import Link from 'next/link';
import { useState } from 'react';
import {
  LayoutDashboard, List, Search, BarChart3, Link2,
  ChevronLeft, ChevronRight, MessageCircle, Settings as SettingsIcon,
  Activity, ClipboardList, Users, Bot,
} from 'lucide-react';

interface NavItem {
  url: string;
  label: string;
  icon: React.ElementType;
  match: (p: string) => boolean;
}

interface NavSection {
  title: string;
  items: NavItem[];
}

const NAV_SECTIONS: NavSection[] = [
  {
    title: 'Overview',
    items: [
      { url: '/dashboard', label: 'Dashboard', icon: LayoutDashboard, match: (p) => p === '/dashboard' || p === '/' },
      { url: '/sessions', label: 'Sessions', icon: List, match: (p) => p.startsWith('/sessions') },
      { url: '/search', label: 'Search', icon: Search, match: (p) => p.startsWith('/search') },
    ],
  },
  {
    title: 'Team',
    items: [
      { url: '/agents', label: 'Agents', icon: Bot, match: (p) => p.startsWith('/agents') },
    ],
  },
  {
    title: 'Insights',
    items: [
      { url: '/traces', label: 'Traces', icon: Activity, match: (p) => p.startsWith('/traces') },
      { url: '/evals', label: 'Evals', icon: ClipboardList, match: (p) => p.startsWith('/evals') },
      { url: '/analytics', label: 'Analytics', icon: BarChart3, match: (p) => p.startsWith('/analytics') },
      { url: '/chat', label: 'Chat', icon: MessageCircle, match: (p) => p.startsWith('/chat') },
    ],
  },
  {
    title: 'Settings',
    items: [
      { url: '/connections', label: 'Connections', icon: Link2, match: (p) => p.startsWith('/connections') },
      { url: '/settings', label: 'Settings', icon: SettingsIcon, match: (p) => p.startsWith('/settings') },
    ],
  },
];

export default function AppLayout({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const [collapsed, setCollapsed] = useState(false);

  return (
    <div style={{ display: 'flex', minHeight: '100vh', background: 'var(--bg)', color: 'var(--fg)' }}>
      <nav
        style={{
          position: 'sticky', top: 0, height: '100vh',
          display: 'flex', flexDirection: 'column',
          background: 'var(--sidebar-bg)',
          borderRight: '1px solid var(--border)',
          flexShrink: 0, transition: 'width 200ms, background 180ms',
          overflow: 'hidden',
          width: collapsed ? 52 : 208,
        }}
      >
        <div style={{
          display: 'flex', alignItems: 'center', height: 56,
          padding: collapsed ? '0' : '0 12px',
          justifyContent: collapsed ? 'center' : 'flex-start',
        }}>
          {!collapsed ? (
            <>
              <Link href="/dashboard" style={{ display: 'flex', alignItems: 'center', gap: 10, flex: 1, color: 'var(--fg)', textDecoration: 'none', paddingLeft: 4 }}>
                <img src="/logo.svg" alt="Spooling" style={{ height: 32, width: 32 }} />
                <span style={{ fontSize: 18, fontWeight: 600, letterSpacing: '-0.02em' }}>Spooling</span>
              </Link>
              <button onClick={() => setCollapsed(true)} style={iconBtn} aria-label="Collapse sidebar">
                <ChevronLeft size={14} />
              </button>
            </>
          ) : (
            <button onClick={() => setCollapsed(false)} style={{ background: 'transparent', border: 'none', cursor: 'pointer', display: 'flex', alignItems: 'center', justifyContent: 'center', padding: 0 }}>
              <img src="/logo.svg" alt="Spooling" style={{ height: 28, width: 28, display: 'block' }} />
            </button>
          )}
        </div>

        <div style={{ flex: 1, display: 'flex', flexDirection: 'column', overflow: 'auto', padding: collapsed ? '0 6px' : '0 8px' }}>
          {NAV_SECTIONS.map((section, sIdx) => (
            <div key={section.title} style={{ marginTop: sIdx > 0 ? 16 : 0 }}>
              {!collapsed && (
                <div style={{ padding: '0 8px 4px', fontSize: 11, fontWeight: 500, color: 'var(--muted-2)', textTransform: 'uppercase', letterSpacing: '0.06em' }}>
                  {section.title}
                </div>
              )}
              {collapsed && sIdx > 0 && <div style={{ margin: '8px 8px', borderTop: '1px solid var(--surface-hover)' }} />}
              <div style={{ display: 'flex', flexDirection: 'column', gap: 1 }}>
                {section.items.map((item) => {
                  const active = item.match(pathname);
                  const Icon = item.icon;
                  return (
                    <Link
                      key={item.url}
                      href={item.url}
                      title={collapsed ? item.label : undefined}
                      style={{
                        display: 'flex', alignItems: 'center', gap: 8,
                        borderRadius: 6, fontSize: 13, textDecoration: 'none',
                        padding: collapsed ? 8 : '6px 8px',
                        justifyContent: collapsed ? 'center' : 'flex-start',
                        color: active ? 'var(--fg)' : 'var(--muted)',
                        background: active ? 'var(--surface-hover)' : 'transparent',
                        fontWeight: active ? 500 : 400,
                      }}
                    >
                      <Icon size={16} style={{ flexShrink: 0, color: active ? '#a78bfa' : undefined }} />
                      {!collapsed && <span>{item.label}</span>}
                    </Link>
                  );
                })}
              </div>
            </div>
          ))}
        </div>

        <div style={{ borderTop: '1px solid var(--surface-hover)', padding: collapsed ? 6 : 12 }}>
          <div style={{ display: 'flex', justifyContent: collapsed ? 'center' : 'flex-end', alignItems: 'center' }}>
            {!collapsed && <span style={{ fontSize: 10, color: 'var(--muted-2)' }}>v0.1.0</span>}
          </div>
        </div>
      </nav>

      <main style={{ flex: 1, padding: '28px 32px', minWidth: 0 }}>
        {children}
      </main>
    </div>
  );
}

const iconBtn: React.CSSProperties = {
  background: 'transparent', border: 'none',
  color: 'var(--muted)', padding: 6, borderRadius: 6,
  cursor: 'pointer', display: 'inline-flex',
  alignItems: 'center', justifyContent: 'center',
};
