'use client';

import { useState, useEffect, useCallback } from 'react';
import { Card, CardContent } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import {
  Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription,
} from '@/components/ui/dialog';
import { CheckCircle, XCircle, Plus, Info, RefreshCw } from 'lucide-react';
import { fetchApi, postApi, deleteApi } from '@/lib/api';
import { toast } from 'sonner';

interface Provider {
  id: string; name: string; type: string; status: string;
  data_path: string; icon: string; session_count: number;
  last_synced_at: string | null; description: string;
}

interface AvailableProvider {
  type: string; name: string; icon: string;
  default_path: string; description: string; connected: boolean;
}

const COLORS: Record<string, string> = {
  openai: 'bg-emerald-500', github: 'bg-primary',
  cursor: 'bg-violet-500', windsurf: 'bg-cyan-500',
};
const LABELS: Record<string, string> = {
  openai: 'AI', github: 'GH', cursor: 'CU', windsurf: 'WS',
};

function Avatar({ icon }: { icon: string }) {
  return (
    <div className={`w-8 h-8 rounded-lg ${COLORS[icon] || 'bg-muted-foreground'} flex items-center justify-center text-white font-semibold text-[11px] shrink-0`}>
      {LABELS[icon] || icon.slice(0, 2).toUpperCase()}
    </div>
  );
}

export default function ConnectionsPage() {
  const [loading, setLoading] = useState(true);
  const [providers, setProviders] = useState<Provider[]>([]);
  const [available, setAvailable] = useState<AvailableProvider[]>([]);
  const [modalOpen, setModalOpen] = useState(false);
  const [selectedType, setSelectedType] = useState<AvailableProvider | null>(null);
  const [customPath, setCustomPath] = useState('');
  const [syncing, setSyncing] = useState<Record<string, boolean>>({});
  const [syncingAll, setSyncingAll] = useState(false);

  const load = useCallback(async () => {
    try {
      const [p, a] = await Promise.all([fetchApi('/api/providers'), fetchApi('/api/providers/available')]);
      setProviders(p); setAvailable(a);
    } catch (e) { console.error(e); }
    finally { setLoading(false); }
  }, []);

  useEffect(() => { load(); }, [load]);

  const connect = async () => {
    if (!selectedType) return;
    const providerName = selectedType.name;
    const providerType = selectedType.type;
    try {
      await postApi('/api/providers', { type: providerType, data_path: customPath || selectedType.default_path });
    } catch (e) {
      console.error(e);
      toast.error(`Failed to connect ${providerName}`);
      return;
    }
    toast.success(`Connected ${providerName}`);
    setModalOpen(false); setSelectedType(null); setCustomPath('');
    // Auto-sync the newly connected provider
    setSyncing(s => ({ ...s, [providerType]: true }));
    await load();
    const syncToast = toast.loading(`Syncing ${providerName}...`);
    try {
      await postApi(`/api/providers/${providerType}/sync`, {});
      await load();
      toast.success(`${providerName} synced`, { id: syncToast });
    } catch (e) {
      console.error(e);
      toast.error(`Failed to sync ${providerName}`, { id: syncToast });
    }
    finally { setSyncing(s => ({ ...s, [providerType]: false })); }
  };

  const disconnect = async (id: string) => {
    const name = providers.find(p => p.id === id)?.name ?? 'provider';
    try {
      await deleteApi(`/api/providers/${id}`);
      toast.success(`Disconnected ${name}`);
      load();
    } catch (e) {
      console.error(e);
      toast.error(`Failed to disconnect ${name}`);
    }
  };

  const syncProvider = async (id: string) => {
    const name = providers.find(p => p.id === id)?.name ?? 'provider';
    setSyncing(s => ({ ...s, [id]: true }));
    const t = toast.loading(`Syncing ${name}...`);
    try {
      await postApi(`/api/providers/${id}/sync`, {});
      await load();
      toast.success(`${name} synced`, { id: t });
    } catch (e) {
      console.error(e);
      toast.error(`Failed to sync ${name}`, { id: t });
    }
    finally { setSyncing(s => ({ ...s, [id]: false })); }
  };

  const syncAll = async () => {
    setSyncingAll(true);
    const t = toast.loading('Syncing all providers...');
    try {
      await postApi('/api/sync', { embed: false });
      await load();
      toast.success('All providers synced', { id: t });
    } catch (e) {
      console.error(e);
      toast.error('Failed to sync providers', { id: t });
    }
    finally { setSyncingAll(false); }
  };

  if (loading) return <div className="flex items-center justify-center h-64"><div className="animate-spin rounded-full h-6 w-6 border-2 border-primary border-t-transparent" /></div>;

  const unconnected = available.filter(a => !a.connected);

  return (
    <div className="space-y-5">
      <div className="flex items-center justify-between">
        <h1 className="text-lg font-semibold tracking-tight">Connections</h1>
        <div className="flex items-center gap-2">
          {providers.length > 0 && (
            <Button variant="outline" size="sm" onClick={syncAll} disabled={syncingAll}>
              <RefreshCw className={`h-3.5 w-3.5 mr-1.5 ${syncingAll ? 'animate-spin' : ''}`} />
              {syncingAll ? 'Syncing...' : 'Sync All'}
            </Button>
          )}
          {unconnected.length > 0 && (
            <Button onClick={() => setModalOpen(true)} size="sm">
              <Plus className="h-3.5 w-3.5 mr-1.5" /> Add Connection
            </Button>
          )}
        </div>
      </div>

      {/* Info banner */}
      <div className="flex items-start gap-2.5 p-3 rounded-lg bg-primary/5 border border-primary/15 text-[13px] text-foreground">
        <Info className="h-4 w-4 mt-0.5 shrink-0 text-primary" />
        Connect your AI coding tools to track sessions across all providers. Spool reads local session data only.
      </div>

      {/* Connected */}
      <div>
        <h2 className="text-[11px] font-medium text-muted-foreground uppercase tracking-wider mb-2">Connected</h2>
        <div className="grid md:grid-cols-2 gap-3">
          {providers.map(p => (
            <Card key={p.id}>
              <CardContent className="pt-4 pb-4 space-y-3">
                <div className="flex items-center gap-2.5">
                  <Avatar icon={p.icon || 'default'} />
                  <div className="flex-1 min-w-0">
                    <div className="text-[13px] font-semibold">{p.name}</div>
                    <div className="text-[11px] text-muted-foreground truncate font-mono">{p.data_path}</div>
                  </div>
                  {p.status === 'connected'
                    ? <Badge variant="success"><CheckCircle className="h-3 w-3 mr-1" /> Active</Badge>
                    : <Badge variant="warning"><XCircle className="h-3 w-3 mr-1" /> Off</Badge>
                  }
                </div>
                <div className="flex gap-5 text-[13px]">
                  <div><span className="text-[11px] text-muted-foreground block">Sessions</span><span className="font-medium">{p.session_count || 0}</span></div>
                  <div><span className="text-[11px] text-muted-foreground block">Last Synced</span><span className="font-medium">{p.last_synced_at ? new Date(p.last_synced_at).toLocaleDateString() : 'Never'}</span></div>
                </div>
                <div className="flex gap-2">
                  <Button variant="outline" size="sm" onClick={() => syncProvider(p.id)} disabled={syncing[p.id]}>
                    <RefreshCw className={`h-3 w-3 mr-1.5 ${syncing[p.id] ? 'animate-spin' : ''}`} />
                    {syncing[p.id] ? 'Syncing...' : 'Sync'}
                  </Button>
                  <Button variant="outline" size="sm" onClick={() => disconnect(p.id)}>Disconnect</Button>
                </div>
              </CardContent>
            </Card>
          ))}
        </div>
      </div>

      {/* Available */}
      {unconnected.length > 0 && (
        <div>
          <h2 className="text-[11px] font-medium text-muted-foreground uppercase tracking-wider mb-2">Available</h2>
          <div className="grid md:grid-cols-2 lg:grid-cols-3 gap-3">
            {unconnected.map(a => (
              <Card key={a.type}>
                <CardContent className="pt-4 pb-4 space-y-2.5">
                  <div className="flex items-center gap-2.5">
                    <Avatar icon={a.icon} />
                    <div><div className="text-[13px] font-semibold">{a.name}</div><div className="text-[11px] text-muted-foreground font-mono">{a.default_path}</div></div>
                  </div>
                  <p className="text-[11px] text-muted-foreground">{a.description}</p>
                  <Button size="sm" onClick={() => { setSelectedType(a); setCustomPath(''); setModalOpen(true); }}>
                    <Plus className="h-3 w-3 mr-1" /> Connect
                  </Button>
                </CardContent>
              </Card>
            ))}
          </div>
        </div>
      )}

      {/* Modal */}
      <Dialog open={modalOpen} onOpenChange={(o) => { setModalOpen(o); if (!o) setSelectedType(null); }}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{selectedType ? `Connect ${selectedType.name}` : 'Add Connection'}</DialogTitle>
            <DialogDescription>
              {selectedType ? selectedType.description : 'Select a provider to connect.'}
            </DialogDescription>
          </DialogHeader>
          {!selectedType ? (
            <div className="space-y-1.5">
              {unconnected.map(a => (
                <button
                  key={a.type}
                  onClick={() => { setSelectedType(a); setCustomPath(''); }}
                  className="w-full flex items-center gap-2.5 p-2.5 rounded-md border hover:bg-accent transition-colors text-left"
                >
                  <Avatar icon={a.icon} />
                  <div><div className="text-[13px] font-medium">{a.name}</div><div className="text-[11px] text-muted-foreground">{a.description}</div></div>
                </button>
              ))}
            </div>
          ) : (
            <div className="space-y-3">
              <div className="flex items-center gap-2.5">
                <Avatar icon={selectedType.icon} />
                <span className="text-[13px] font-semibold">{selectedType.name}</span>
              </div>
              <div>
                <label className="text-[13px] font-medium mb-1 block">Data Path</label>
                <Input value={customPath || selectedType.default_path} onChange={(e) => setCustomPath(e.target.value)} />
                <p className="text-[11px] text-muted-foreground mt-1">Path to the directory where session data is stored.</p>
              </div>
              <div className="flex justify-end gap-2">
                <Button variant="outline" size="sm" onClick={() => { setModalOpen(false); setSelectedType(null); }}>Cancel</Button>
                <Button size="sm" onClick={connect}>Connect</Button>
              </div>
            </div>
          )}
        </DialogContent>
      </Dialog>
    </div>
  );
}
