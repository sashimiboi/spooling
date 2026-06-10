'use client';

import { usePathname, useRouter } from 'next/navigation';
import { cn } from '@/lib/utils';
import { useTheme } from '@/components/ThemeProvider';
import {
  LayoutDashboard, List, Search, BarChart3, Link2,
  ChevronLeft, ChevronRight, MessageCircle, Settings,
  Sun, Moon, Activity, ClipboardList, Wrench,
} from 'lucide-react';

function SpoolLogo({ className }: { className?: string }) {
  return <img src="/logo.svg" alt="Spooling" className={className} />;
}

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
    title: 'Insights',
    items: [
      { url: '/traces', label: 'Traces', icon: Activity, match: (p) => p.startsWith('/traces') },
      { url: '/evals', label: 'Evals', icon: ClipboardList, match: (p) => p.startsWith('/evals') },
      { url: '/tools', label: 'Tools', icon: Wrench, match: (p) => p.startsWith('/tools') },
      { url: '/analytics', label: 'Analytics', icon: BarChart3, match: (p) => p.startsWith('/analytics') },
      { url: '/chat', label: 'Chat', icon: MessageCircle, match: (p) => p.startsWith('/chat') },
    ],
  },
  {
    title: 'Settings',
    items: [
      { url: '/connections', label: 'Connections', icon: Link2, match: (p) => p.startsWith('/connections') },
      { url: '/settings', label: 'Settings', icon: Settings, match: (p) => p.startsWith('/settings') },
    ],
  },
];

interface Props {
  collapsed: boolean;
  onToggle: () => void;
}

export default function AppNavigation({ collapsed, onToggle }: Props) {
  const pathname = usePathname();
  const router = useRouter();
  const { theme, setTheme, resolved } = useTheme();

  const cycleTheme = () => {
    const order: Array<'light' | 'dark' | 'system'> = ['light', 'dark', 'system'];
    const idx = order.indexOf(theme);
    setTheme(order[(idx + 1) % order.length]);
  };

  const ThemeIcon = resolved === 'dark' ? Moon : Sun;

  return (
    <nav
      className="sticky top-0 h-screen flex flex-col bg-sidebar border-r border-border shrink-0 transition-all duration-200 overflow-hidden"
      style={{ width: collapsed ? 52 : 208 }}
    >
      {/* Header */}
      <div className={cn('flex items-center h-14', collapsed ? 'justify-center px-2' : 'px-3')}>
        {!collapsed ? (
          <div className="flex items-center gap-2.5 flex-1 pl-1">
            <SpoolLogo className="h-8 w-8 text-foreground" />
            <span className="text-[18px] font-semibold text-foreground tracking-tight">
              Spooling
            </span>
          </div>
        ) : (
          <SpoolLogo className="h-7 w-7 text-foreground" />
        )}
        <button
          onClick={onToggle}
          className="p-1 rounded text-sidebar-foreground hover:text-foreground hover:bg-accent transition-colors"
        >
          {collapsed ? <ChevronRight className="h-3.5 w-3.5" /> : <ChevronLeft className="h-3.5 w-3.5" />}
        </button>
      </div>

      {/* Sections */}
      <div className={cn('flex-1 flex flex-col overflow-auto scrollbar-thin', collapsed ? 'px-1.5' : 'px-2')}>
        {NAV_SECTIONS.map((section, sIdx) => (
          <div key={section.title} className={sIdx > 0 ? 'mt-4' : ''}>
            {!collapsed && (
              <div className="px-2 pb-1 text-[11px] font-medium text-sidebar-foreground uppercase tracking-wider">
                {section.title}
              </div>
            )}
            {collapsed && sIdx > 0 && <div className="mx-2 my-2 border-t border-border" />}
            <div className="flex flex-col gap-px">
              {section.items.map((item) => {
                const active = item.match(pathname);
                const Icon = item.icon;
                return (
                  <button
                    key={item.url}
                    onClick={() => router.push(item.url)}
                    title={collapsed ? item.label : undefined}
                    className={cn(
                      'w-full flex items-center gap-2 rounded-md text-[13px] transition-colors',
                      collapsed ? 'justify-center p-2' : 'px-2 py-1.5',
                      active
                        ? 'bg-accent text-sidebar-active font-medium'
                        : 'text-sidebar-foreground hover:bg-accent/60 hover:text-foreground'
                    )}
                  >
                    <Icon className={cn('h-4 w-4 shrink-0', active ? 'text-primary' : '')} />
                    {!collapsed && <span>{item.label}</span>}
                  </button>
                );
              })}
            </div>
          </div>
        ))}
      </div>

      {/* Footer */}
      <div className={cn('border-t border-border', collapsed ? 'p-1.5' : 'px-3 py-2')}>
        <div className={cn('flex items-center', collapsed ? 'justify-center' : 'justify-between')}>
          <button
            onClick={cycleTheme}
            title={`Theme: ${theme}`}
            className="p-1.5 rounded text-sidebar-foreground hover:text-foreground hover:bg-accent transition-colors"
          >
            <ThemeIcon className="h-3.5 w-3.5" />
          </button>
          {!collapsed && (
            <span className="text-[11px] text-sidebar-foreground">v0.1.0</span>
          )}
        </div>
      </div>
    </nav>
  );
}
