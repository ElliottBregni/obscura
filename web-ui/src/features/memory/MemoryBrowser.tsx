import { useState, useCallback } from 'react';
import {
  useMemoryNamespaces,
  useMemoryKeys,
  useMemoryValue,
  useSetMemory,
  useDeleteMemory,
} from '@/api/client';
import { Button } from '@/components/ui/Button';
import { Input } from '@/components/ui/Input';
import { ScrollArea } from '@/components/ui/ScrollArea';
import { TreeView } from '@/components/ui/TreeView';
import { Brain, Search, Save, Trash2, RefreshCw, Download, Loader2 } from 'lucide-react';
import { toast } from 'sonner';

interface TreeNode {
  id: string;
  name: string;
  type: 'folder' | 'file';
  children?: TreeNode[];
}

export function MemoryBrowser() {
  const { data: namespaces = [], isLoading: namespacesLoading, refetch: refetchNamespaces } = useMemoryNamespaces();
  const [selectedNamespace, setSelectedNamespace] = useState<string | null>(null);
  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  const [selectedNode, setSelectedNode] = useState<any>(null);
  const [searchQuery, setSearchQuery] = useState('');
  const [editValue, setEditValue] = useState('');
  const [isDirty, setIsDirty] = useState(false);

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

  return (
    <div className="h-[calc(100vh-4rem)] flex">
      {/* Sidebar */}
      <div className="w-80 border-r border-border bg-card flex flex-col">
        <div className="p-4 border-b border-border">
          <div className="flex items-center justify-between mb-4">
            <h2 className="font-semibold text-foreground">Memory Browser</h2>
            <Button variant="ghost" size="sm" onClick={() => refetchNamespaces()}>
              <RefreshCw className="w-4 h-4" />
            </Button>
          </div>
          <div className="relative">
            <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 w-4 h-4 text-muted-foreground" />
            <Input
              type="text"
              placeholder="Search namespaces..."
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
  );
}
