import { useState, useCallback } from 'react';
import {
  useMemoryNamespaces,
  useMemoryKeys,
  useMemoryValue,
  useSetMemory,
  useDeleteMemory,
  useVectorMemorySearch,
} from '@/api/client';
import type { VectorMemoryResult } from '@/api/client';
import { Button } from '@/components/ui/Button';
import { Input } from '@/components/ui/Input';
import { ScrollArea } from '@/components/ui/ScrollArea';
import { TreeView } from '@/components/ui/TreeView';
import { Brain, Search, Save, Trash2, RefreshCw, Download, Loader2, Sparkles } from 'lucide-react';
import { toast } from 'sonner';

interface TreeNode {
  id: string;
  name: string;
  type: 'folder' | 'file';
  children?: TreeNode[];
}

type Tab = 'kv' | 'semantic';

function ScoreBadge({ score }: { score: number }) {
  const pct = Math.round(score * 100);
  const color = pct >= 80 ? 'text-green-400' : pct >= 50 ? 'text-yellow-400' : 'text-muted-foreground';
  return <span className={`text-xs font-mono ${color}`}>{pct}%</span>;
}

function TypeBadge({ type }: { type: string }) {
  const colors: Record<string, string> = {
    summary: 'bg-blue-500/20 text-blue-400',
    episode: 'bg-purple-500/20 text-purple-400',
    skill: 'bg-green-500/20 text-green-400',
    fact: 'bg-orange-500/20 text-orange-400',
  };
  return (
    <span className={`text-xs px-1.5 py-0.5 rounded ${colors[type] || 'bg-accent text-muted-foreground'}`}>
      {type}
    </span>
  );
}

function VectorResultCard({ result }: { result: VectorMemoryResult }) {
  const [expanded, setExpanded] = useState(false);
  const meta = result.metadata || {};

  return (
    <div
      className="p-4 rounded-lg border border-border bg-card hover:border-primary/50 cursor-pointer transition-colors"
      onClick={() => setExpanded(!expanded)}
    >
      <div className="flex items-start justify-between gap-3 mb-2">
        <div className="flex items-center gap-2 flex-wrap">
          <TypeBadge type={result.memory_type} />
          {meta.agent && (
            <span className="text-xs text-muted-foreground">{meta.agent}</span>
          )}
          <ScoreBadge score={result.final_score || result.score} />
        </div>
        <span className="text-xs text-muted-foreground font-mono shrink-0">
          {result.namespace}:{result.key.length > 30 ? result.key.slice(0, 27) + '...' : result.key}
        </span>
      </div>

      <p className="text-sm text-foreground line-clamp-3 whitespace-pre-wrap">
        {result.text}
      </p>

      {expanded && Object.keys(meta).length > 0 && (
        <div className="mt-3 pt-3 border-t border-border space-y-1">
          {meta.project && (
            <div className="text-xs"><span className="text-muted-foreground">project:</span> <span className="text-foreground">{meta.project}</span></div>
          )}
          {meta.model && (
            <div className="text-xs"><span className="text-muted-foreground">model:</span> <span className="text-foreground">{meta.model}</span></div>
          )}
          {meta.started && (
            <div className="text-xs"><span className="text-muted-foreground">started:</span> <span className="text-foreground">{new Date(meta.started).toLocaleString()}</span></div>
          )}
          {meta.tools_used && meta.tools_used.length > 0 && (
            <div className="text-xs"><span className="text-muted-foreground">tools:</span> <span className="text-foreground">{meta.tools_used.join(', ')}</span></div>
          )}
          {meta.git_branch && (
            <div className="text-xs"><span className="text-muted-foreground">branch:</span> <span className="text-foreground">{meta.git_branch}</span></div>
          )}
          {meta.session_id && (
            <div className="text-xs"><span className="text-muted-foreground">session:</span> <span className="text-foreground font-mono">{meta.session_id}</span></div>
          )}
        </div>
      )}
    </div>
  );
}

export function MemoryBrowser() {
  const [activeTab, setActiveTab] = useState<Tab>('kv');

  // KV state
  const { data: namespaces = [], isLoading: namespacesLoading, refetch: refetchNamespaces } = useMemoryNamespaces();
  const [selectedNamespace, setSelectedNamespace] = useState<string | null>(null);
  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  const [selectedNode, setSelectedNode] = useState<any>(null);
  const [searchQuery, setSearchQuery] = useState('');
  const [editValue, setEditValue] = useState('');
  const [isDirty, setIsDirty] = useState(false);

  // Semantic state
  const [semanticQuery, setSemanticQuery] = useState('');
  const [submittedQuery, setSubmittedQuery] = useState('');
  const { data: vectorResults, isLoading: vectorLoading } = useVectorMemorySearch(submittedQuery, { top_k: 20 });

  const { data: keys = [] } = useMemoryKeys(selectedNamespace || '');
  const { data: valueData, isLoading: valueLoading } = useMemoryValue(
    selectedNamespace || '',
    selectedKey || ''
  );
  const setMemory = useSetMemory();
  const deleteMemory = useDeleteMemory();

  // Build tree from real namespace + key data
  const treeData: TreeNode[] = namespaces
    .filter(ns => !searchQuery || ns.toLowerCase().includes(searchQuery.toLowerCase()))
    .map(ns => {
      const nsKeys = keys.filter(k => k.namespace === ns);
      return {
        id: ns,
        name: ns,
        type: 'folder' as const,
        children: nsKeys.map(k => ({
          id: `${ns}/${k.key}`,
          name: k.key,
          type: 'file' as const,
        })),
      };
    });

  const handleNodeSelect = useCallback((node: any) => {
    setSelectedNode(node);
    if (node.type === 'folder') {
      setSelectedNamespace(node.id);
      setSelectedKey(null);
    } else if (node.type === 'file') {
      const parts = node.id.split('/');
      const ns = parts[0];
      const key = parts.slice(1).join('/');
      setSelectedNamespace(ns);
      setSelectedKey(key);
      setIsDirty(false);
    }
  }, []);

  // Sync editValue when value loads from API
  const currentValue = valueData?.value;
  const displayValue = isDirty
    ? editValue
    : (currentValue !== undefined ? JSON.stringify(currentValue, null, 2) : '');

  const handleSave = async () => {
    if (!selectedNamespace || !selectedKey) return;
    try {
      const parsed = JSON.parse(editValue || displayValue);
      await setMemory.mutateAsync({
        namespace: selectedNamespace,
        key: selectedKey,
        value: parsed
      });
      setIsDirty(false);
      toast.success('Saved');
    } catch (e: any) {
      toast.error(e.message || 'Failed to save');
    }
  };

  const handleDelete = async () => {
    if (!selectedNamespace || !selectedKey) return;
    try {
      await deleteMemory.mutateAsync({ namespace: selectedNamespace, key: selectedKey });
      setSelectedKey(null);
      setSelectedNode(null);
      setIsDirty(false);
      toast.success('Deleted');
    } catch (e: any) {
      toast.error(e.message || 'Failed to delete');
    }
  };

  const handleExport = async () => {
    try {
      const resp = await fetch(
        `${import.meta.env.VITE_API_URL || 'http://localhost:8080'}/api/v1/memory/export`
      );
      const data = await resp.json();
      const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `obscura-memory-export-${new Date().toISOString().slice(0, 10)}.json`;
      a.click();
      URL.revokeObjectURL(url);
      toast.success('Exported');
    } catch {
      toast.error('Export failed');
    }
  };

  const handleSemanticSearch = (e: React.FormEvent) => {
    e.preventDefault();
    setSubmittedQuery(semanticQuery);
  };

  return (
    <div className="h-[calc(100vh-4rem)] flex flex-col">
      {/* Tab bar */}
      <div className="h-12 border-b border-border flex items-center px-4 gap-1 bg-card shrink-0">
        <button
          className={`px-4 py-1.5 rounded text-sm font-medium transition-colors ${
            activeTab === 'kv'
              ? 'bg-primary text-primary-foreground'
              : 'text-muted-foreground hover:text-foreground'
          }`}
          onClick={() => setActiveTab('kv')}
        >
          <Brain className="w-4 h-4 inline mr-1.5 -mt-0.5" />
          Key-Value
        </button>
        <button
          className={`px-4 py-1.5 rounded text-sm font-medium transition-colors ${
            activeTab === 'semantic'
              ? 'bg-primary text-primary-foreground'
              : 'text-muted-foreground hover:text-foreground'
          }`}
          onClick={() => setActiveTab('semantic')}
        >
          <Sparkles className="w-4 h-4 inline mr-1.5 -mt-0.5" />
          Semantic Search
        </button>
      </div>

      {activeTab === 'kv' ? (
        /* KV Memory Browser */
        <div className="flex-1 flex min-h-0">
          {/* Sidebar */}
          <div className="w-80 border-r border-border bg-card flex flex-col">
            <div className="p-4 border-b border-border">
              <div className="flex items-center justify-between mb-4">
                <h2 className="font-semibold text-foreground">Namespaces</h2>
                <Button variant="ghost" size="sm" onClick={() => refetchNamespaces()}>
                  <RefreshCw className="w-4 h-4" />
                </Button>
              </div>
              <div className="relative">
                <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 w-4 h-4 text-muted-foreground" />
                <Input
                  type="text"
                  placeholder="Filter namespaces..."
                  value={searchQuery}
                  onChange={(e) => setSearchQuery(e.target.value)}
                  className="pl-9"
                />
              </div>
            </div>

            <ScrollArea className="flex-1">
              <div className="p-2">
                {namespacesLoading ? (
                  <div className="flex items-center justify-center py-8">
                    <Loader2 className="h-5 w-5 animate-spin text-muted-foreground" />
                  </div>
                ) : treeData.length === 0 ? (
                  <div className="text-center py-8 text-muted-foreground text-sm">
                    No namespaces found
                  </div>
                ) : (
                  <TreeView
                    data={treeData}
                    onSelect={handleNodeSelect}
                    selectedId={selectedNode?.id}
                  />
                )}
              </div>
            </ScrollArea>

            <div className="p-4 border-t border-border space-y-2">
              <Button variant="secondary" size="sm" className="w-full" onClick={handleExport}>
                <Download className="w-4 h-4 mr-2" /> Export
              </Button>
            </div>
          </div>

          {/* Content Area */}
          <div className="flex-1 flex flex-col bg-background">
            {selectedKey ? (
              <>
                <div className="h-14 border-b border-border flex items-center justify-between px-6">
                  <div className="flex items-center gap-3">
                    <Brain className="w-5 h-5 text-purple-400" />
                    <span className="font-medium text-foreground">
                      {selectedNamespace}/{selectedKey}
                    </span>
                    {isDirty && (
                      <span className="text-xs text-yellow-400">(unsaved)</span>
                    )}
                  </div>
                  <div className="flex items-center gap-2">
                    <Button
                      variant="secondary"
                      size="sm"
                      onClick={handleDelete}
                      isLoading={deleteMemory.isPending}
                    >
                      <Trash2 className="w-4 h-4 mr-2" /> Delete
                    </Button>
                    <Button
                      size="sm"
                      onClick={handleSave}
                      isLoading={setMemory.isPending}
                      disabled={!isDirty}
                    >
                      <Save className="w-4 h-4 mr-2" /> Save
                    </Button>
                  </div>
                </div>

                <div className="flex-1 p-6">
                  {valueLoading ? (
                    <div className="flex items-center justify-center h-full">
                      <Loader2 className="h-5 w-5 animate-spin text-muted-foreground" />
                    </div>
                  ) : (
                    <textarea
                      value={isDirty ? editValue : displayValue}
                      onChange={(e) => {
                        setEditValue(e.target.value);
                        setIsDirty(true);
                      }}
                      className="w-full h-full p-4 rounded-lg bg-card border border-border font-mono text-sm text-foreground resize-none focus:outline-none focus:ring-2 focus:ring-primary"
                      spellCheck={false}
                    />
                  )}
                </div>
              </>
            ) : (
              <div className="flex flex-col items-center justify-center h-full text-center">
                <div className="w-20 h-20 rounded-full bg-accent flex items-center justify-center mb-6">
                  <Brain className="w-10 h-10 text-muted-foreground" />
                </div>
                <h2 className="text-xl font-semibold text-foreground">Memory Browser</h2>
                <p className="text-muted-foreground mt-2 max-w-md">
                  {namespaces.length === 0
                    ? 'No memory namespaces exist yet. Create one via the API or agents.'
                    : 'Select a key to view or edit its contents'}
                </p>
              </div>
            )}
          </div>
        </div>
      ) : (
        /* Semantic Search */
        <div className="flex-1 flex flex-col bg-background min-h-0">
          <div className="p-6 border-b border-border">
            <form onSubmit={handleSemanticSearch} className="flex gap-3 max-w-2xl">
              <div className="relative flex-1">
                <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-muted-foreground" />
                <Input
                  type="text"
                  placeholder="Search memories semantically..."
                  value={semanticQuery}
                  onChange={(e) => setSemanticQuery(e.target.value)}
                  className="pl-10"
                />
              </div>
              <Button type="submit" disabled={!semanticQuery.trim()}>
                <Sparkles className="w-4 h-4 mr-2" /> Search
              </Button>
            </form>
          </div>

          <ScrollArea className="flex-1">
            <div className="p-6 max-w-4xl">
              {vectorLoading ? (
                <div className="flex items-center justify-center py-12">
                  <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" />
                </div>
              ) : vectorResults && vectorResults.results.length > 0 ? (
                <div className="space-y-3">
                  <p className="text-xs text-muted-foreground mb-4">
                    {vectorResults.count} results for "{vectorResults.query}"
                  </p>
                  {vectorResults.results.map((r, i) => (
                    <VectorResultCard key={`${r.namespace}:${r.key}:${i}`} result={r} />
                  ))}
                </div>
              ) : submittedQuery ? (
                <div className="text-center py-12 text-muted-foreground">
                  No results found for "{submittedQuery}"
                </div>
              ) : (
                <div className="flex flex-col items-center justify-center py-16 text-center">
                  <div className="w-20 h-20 rounded-full bg-accent flex items-center justify-center mb-6">
                    <Sparkles className="w-10 h-10 text-muted-foreground" />
                  </div>
                  <h2 className="text-xl font-semibold text-foreground">Semantic Search</h2>
                  <p className="text-muted-foreground mt-2 max-w-md">
                    Search across all vector memories using natural language. Find sessions, skills, and knowledge by meaning, not just keywords.
                  </p>
                </div>
              )}
            </div>
          </ScrollArea>
        </div>
      )}
    </div>
  );
}
