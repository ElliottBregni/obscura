import { useState, useCallback } from 'react';
import { toast } from 'sonner';
import {
  Database,
  Search,
  Plus,
  Trash2,
  Pencil,
  Download,
  Upload,
  FolderOpen,
  Key,
} from 'lucide-react';
import {
  useMemoryNamespaces,
  useMemoryKeys,
  useMemoryValue,
  useSetMemory,
  useDeleteMemory,
  useCreateNamespace,
  useDeleteNamespace,
  useMemoryNamespaceStats,
  useExportMemory,
  useImportMemory,
} from '@/api/hooks/useMemory';
import { useVectorSearch } from '@/api/hooks/useVectorMemory';
import type { VectorMemoryResult } from '@/api/types';
import { Tabs, TabsList, TabsTrigger, TabsContent } from '@/components/ui/Tabs';
import { JsonViewer } from '@/components/ui/JsonViewer';
import { ScrollArea } from '@/components/ui/ScrollArea';
import { Button } from '@/components/ui/Button';
import { Input } from '@/components/ui/Input';
import { Label } from '@/components/ui/Label';
import { Badge } from '@/components/ui/Badge';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card';
import { Spinner } from '@/components/ui/Spinner';
import { EmptyState } from '@/components/ui/EmptyState';
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
  DialogFooter,
} from '@/components/ui/Dialog';
import {
  Select,
  SelectTrigger,
  SelectValue,
  SelectContent,
  SelectItem,
} from '@/components/ui/Select';
import { formatBytes } from '@/lib/utils';

// ---------------------------------------------------------------------------
// KV Browser
// ---------------------------------------------------------------------------

function KVBrowser() {
  const [selectedNamespace, setSelectedNamespace] = useState<string>();
  const [selectedKey, setSelectedKey] = useState<string>();
  const [createNsOpen, setCreateNsOpen] = useState(false);
  const [newNsName, setNewNsName] = useState('');
  const [editOpen, setEditOpen] = useState(false);
  const [editValue, setEditValue] = useState('');

  const namespacesQuery = useMemoryNamespaces();
  const keysQuery = useMemoryKeys(selectedNamespace);
  const valueQuery = useMemoryValue(selectedNamespace, selectedKey);
  const nsStatsQuery = useMemoryNamespaceStats(selectedNamespace);
  const exportQuery = useExportMemory();

  const createNs = useCreateNamespace();
  const deleteNs = useDeleteNamespace();
  const setMemory = useSetMemory();
  const deleteMemory = useDeleteMemory();
  const importMemory = useImportMemory();

  const handleCreateNamespace = useCallback(() => {
    if (!newNsName.trim()) return;
    createNs.mutate(newNsName.trim(), {
      onSuccess: () => {
        toast.success(`Namespace "${newNsName.trim()}" created`);
        setNewNsName('');
        setCreateNsOpen(false);
      },
      onError: (err) => toast.error(`Failed to create namespace: ${String(err)}`),
    });
  }, [newNsName, createNs]);

  const handleDeleteNamespace = useCallback(
    (ns: string) => {
      if (!confirm(`Delete namespace "${ns}" and all its keys?`)) return;
      deleteNs.mutate(ns, {
        onSuccess: () => {
          toast.success(`Namespace "${ns}" deleted`);
          if (selectedNamespace === ns) {
            setSelectedNamespace(undefined);
            setSelectedKey(undefined);
          }
        },
        onError: (err) => toast.error(`Failed to delete namespace: ${String(err)}`),
      });
    },
    [deleteNs, selectedNamespace],
  );

  const handleDeleteKey = useCallback(() => {
    if (!selectedNamespace || !selectedKey) return;
    if (!confirm(`Delete key "${selectedKey}"?`)) return;
    deleteMemory.mutate(
      { namespace: selectedNamespace, key: selectedKey },
      {
        onSuccess: () => {
          toast.success('Key deleted');
          setSelectedKey(undefined);
        },
        onError: (err) => toast.error(`Delete failed: ${String(err)}`),
      },
    );
  }, [deleteMemory, selectedNamespace, selectedKey]);

  const handleEdit = useCallback(() => {
    if (!selectedNamespace || !selectedKey) return;
    try {
      const parsed = JSON.parse(editValue);
      setMemory.mutate(
        { namespace: selectedNamespace, key: selectedKey, value: parsed },
        {
          onSuccess: () => {
            toast.success('Value updated');
            setEditOpen(false);
          },
          onError: (err) => toast.error(`Update failed: ${String(err)}`),
        },
      );
    } catch {
      toast.error('Invalid JSON');
    }
  }, [setMemory, selectedNamespace, selectedKey, editValue]);

  const handleExport = useCallback(() => {
    exportQuery.refetch().then(({ data }) => {
      if (!data) return;
      const blob = new Blob([JSON.stringify(data, null, 2)], {
        type: 'application/json',
      });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = 'memory-export.json';
      a.click();
      URL.revokeObjectURL(url);
      toast.success('Export downloaded');
    });
  }, [exportQuery]);

  const handleImport = useCallback(() => {
    const input = document.createElement('input');
    input.type = 'file';
    input.accept = '.json';
    input.onchange = async (e) => {
      const file = (e.target as HTMLInputElement).files?.[0];
      if (!file) return;
      try {
        const text = await file.text();
        const data = JSON.parse(text);
        importMemory.mutate(data, {
          onSuccess: () => toast.success('Import successful'),
          onError: (err) => toast.error(`Import failed: ${String(err)}`),
        });
      } catch {
        toast.error('Invalid JSON file');
      }
    };
    input.click();
  }, [importMemory]);

  return (
    <div className="flex flex-col gap-4">
      {/* Top bar */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          {nsStatsQuery.data && (
            <>
              <Badge variant="secondary">
                {nsStatsQuery.data.key_count} keys
              </Badge>
              <Badge variant="secondary">
                {formatBytes(nsStatsQuery.data.total_size_bytes)}
              </Badge>
            </>
          )}
        </div>
        <div className="flex items-center gap-2">
          <Button variant="outline" size="sm" onClick={handleExport}>
            <Download className="mr-1.5 h-3.5 w-3.5" />
            Export
          </Button>
          <Button variant="outline" size="sm" onClick={handleImport}>
            <Upload className="mr-1.5 h-3.5 w-3.5" />
            Import
          </Button>
        </div>
      </div>

      {/* Three-panel layout */}
      <div className="grid grid-cols-12 gap-4" style={{ minHeight: 500 }}>
        {/* Left panel: namespaces */}
        <Card className="col-span-3 flex flex-col">
          <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-3">
            <CardTitle className="text-sm font-medium">Namespaces</CardTitle>
            <Button
              variant="ghost"
              size="icon"
              className="h-7 w-7"
              onClick={() => setCreateNsOpen(true)}
            >
              <Plus className="h-4 w-4" />
            </Button>
          </CardHeader>
          <CardContent className="flex-1 p-0">
            {namespacesQuery.isLoading ? (
              <div className="flex h-32 items-center justify-center">
                <Spinner size={20} />
              </div>
            ) : namespacesQuery.isError ? (
              <p className="p-4 text-sm text-destructive">
                Failed to load namespaces
              </p>
            ) : !namespacesQuery.data?.length ? (
              <p className="p-4 text-sm text-muted-foreground">
                No namespaces yet
              </p>
            ) : (
              <ScrollArea className="h-[420px]">
                <div className="space-y-0.5 px-2 pb-2">
                  {namespacesQuery.data.map((ns) => (
                    <div
                      key={ns}
                      className="group flex items-center justify-between"
                    >
                      <button
                        type="button"
                        className={`flex-1 rounded-md px-3 py-2 text-left text-sm transition-colors ${
                          selectedNamespace === ns
                            ? 'bg-primary/10 text-primary'
                            : 'text-foreground hover:bg-muted'
                        }`}
                        onClick={() => {
                          setSelectedNamespace(ns);
                          setSelectedKey(undefined);
                        }}
                      >
                        <FolderOpen className="mr-2 inline-block h-3.5 w-3.5" />
                        {ns}
                      </button>
                      <Button
                        variant="ghost"
                        size="icon"
                        className="h-7 w-7 opacity-0 group-hover:opacity-100"
                        onClick={() => handleDeleteNamespace(ns)}
                      >
                        <Trash2 className="h-3.5 w-3.5 text-destructive" />
                      </Button>
                    </div>
                  ))}
                </div>
              </ScrollArea>
            )}
          </CardContent>
        </Card>

        {/* Middle panel: keys */}
        <Card className="col-span-3 flex flex-col">
          <CardHeader className="pb-3">
            <CardTitle className="text-sm font-medium">
              Keys
              {keysQuery.data && (
                <Badge variant="secondary" className="ml-2 text-xs">
                  {keysQuery.data.length}
                </Badge>
              )}
            </CardTitle>
          </CardHeader>
          <CardContent className="flex-1 p-0">
            {!selectedNamespace ? (
              <p className="p-4 text-sm text-muted-foreground">
                Select a namespace
              </p>
            ) : keysQuery.isLoading ? (
              <div className="flex h-32 items-center justify-center">
                <Spinner size={20} />
              </div>
            ) : keysQuery.isError ? (
              <p className="p-4 text-sm text-destructive">Failed to load keys</p>
            ) : !keysQuery.data?.length ? (
              <p className="p-4 text-sm text-muted-foreground">
                No keys in this namespace
              </p>
            ) : (
              <ScrollArea className="h-[420px]">
                <div className="space-y-0.5 px-2 pb-2">
                  {keysQuery.data.map((entry) => (
                    <button
                      type="button"
                      key={entry.key}
                      className={`w-full rounded-md px-3 py-2 text-left text-sm transition-colors ${
                        selectedKey === entry.key
                          ? 'bg-primary/10 text-primary'
                          : 'text-foreground hover:bg-muted'
                      }`}
                      onClick={() => setSelectedKey(entry.key)}
                    >
                      <Key className="mr-2 inline-block h-3.5 w-3.5" />
                      {entry.key}
                    </button>
                  ))}
                </div>
              </ScrollArea>
            )}
          </CardContent>
        </Card>

        {/* Right panel: value viewer */}
        <Card className="col-span-6 flex flex-col">
          <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-3">
            <CardTitle className="text-sm font-medium">
              {selectedKey ? selectedKey : 'Value'}
            </CardTitle>
            {selectedKey && (
              <div className="flex items-center gap-1">
                <Button
                  variant="ghost"
                  size="icon"
                  className="h-7 w-7"
                  onClick={() => {
                    setEditValue(
                      JSON.stringify(valueQuery.data?.value ?? null, null, 2),
                    );
                    setEditOpen(true);
                  }}
                >
                  <Pencil className="h-3.5 w-3.5" />
                </Button>
                <Button
                  variant="ghost"
                  size="icon"
                  className="h-7 w-7"
                  onClick={handleDeleteKey}
                >
                  <Trash2 className="h-3.5 w-3.5 text-destructive" />
                </Button>
              </div>
            )}
          </CardHeader>
          <CardContent className="flex-1 p-0 px-4 pb-4">
            {!selectedKey ? (
              <div className="flex h-full items-center justify-center text-sm text-muted-foreground">
                Select a key to view its value
              </div>
            ) : valueQuery.isLoading ? (
              <div className="flex h-32 items-center justify-center">
                <Spinner size={20} />
              </div>
            ) : valueQuery.isError ? (
              <p className="text-sm text-destructive">Failed to load value</p>
            ) : (
              <ScrollArea className="h-[420px]">
                <JsonViewer data={valueQuery.data?.value ?? null} />
              </ScrollArea>
            )}
          </CardContent>
        </Card>
      </div>

      {/* Create namespace dialog */}
      <Dialog open={createNsOpen} onOpenChange={setCreateNsOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Create Namespace</DialogTitle>
            <DialogDescription>
              Enter a name for the new memory namespace.
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-2">
            <Label htmlFor="ns-name">Name</Label>
            <Input
              id="ns-name"
              value={newNsName}
              onChange={(e) => setNewNsName(e.target.value)}
              placeholder="my-namespace"
              onKeyDown={(e) => e.key === 'Enter' && handleCreateNamespace()}
            />
          </div>
          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => setCreateNsOpen(false)}
            >
              Cancel
            </Button>
            <Button
              onClick={handleCreateNamespace}
              disabled={!newNsName.trim() || createNs.isPending}
            >
              {createNs.isPending ? <Spinner size={16} className="mr-2" /> : null}
              Create
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Edit value dialog */}
      <Dialog open={editOpen} onOpenChange={setEditOpen}>
        <DialogContent className="max-w-2xl">
          <DialogHeader>
            <DialogTitle>Edit Value</DialogTitle>
            <DialogDescription>
              Editing {selectedNamespace}/{selectedKey}
            </DialogDescription>
          </DialogHeader>
          <textarea
            className="h-64 w-full rounded-md border border-input bg-background p-3 font-mono text-sm focus:outline-none focus:ring-2 focus:ring-ring"
            value={editValue}
            onChange={(e) => setEditValue(e.target.value)}
          />
          <DialogFooter>
            <Button variant="outline" onClick={() => setEditOpen(false)}>
              Cancel
            </Button>
            <Button onClick={handleEdit} disabled={setMemory.isPending}>
              {setMemory.isPending ? <Spinner size={16} className="mr-2" /> : null}
              Save
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Vector Search
// ---------------------------------------------------------------------------

function VectorSearchTab() {
  const [query, setQuery] = useState('');
  const [submitted, setSubmitted] = useState('');
  const [namespace, setNamespace] = useState<string>('');
  const [topK, setTopK] = useState(20);

  const namespacesQuery = useMemoryNamespaces();
  const searchQuery = useVectorSearch(submitted, {
    top_k: topK,
    namespace: namespace || undefined,
  });

  const handleSearch = (e: React.FormEvent) => {
    e.preventDefault();
    if (!query.trim()) return;
    setSubmitted(query.trim());
  };

  return (
    <div className="space-y-6">
      {/* Search form */}
      <Card>
        <CardContent className="pt-6">
          <form onSubmit={handleSearch} className="space-y-4">
            <div className="flex gap-4">
              <div className="flex-1">
                <Label htmlFor="vector-query">Query</Label>
                <Input
                  id="vector-query"
                  value={query}
                  onChange={(e) => setQuery(e.target.value)}
                  placeholder="Search vector memory..."
                />
              </div>
              <div className="w-48">
                <Label>Namespace (optional)</Label>
                <Select value={namespace} onValueChange={setNamespace}>
                  <SelectTrigger>
                    <SelectValue placeholder="All namespaces" />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="">All namespaces</SelectItem>
                    {namespacesQuery.data?.map((ns) => (
                      <SelectItem key={ns} value={ns}>
                        {ns}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
            </div>
            <div className="flex items-end gap-6">
              <div className="flex-1">
                <Label htmlFor="top-k">
                  Top K: {topK}
                </Label>
                <input
                  id="top-k"
                  type="range"
                  min={5}
                  max={50}
                  value={topK}
                  onChange={(e) => setTopK(Number(e.target.value))}
                  className="mt-1.5 w-full accent-primary"
                />
              </div>
              <Button type="submit" disabled={!query.trim()}>
                <Search className="mr-1.5 h-4 w-4" />
                Search
              </Button>
            </div>
          </form>
        </CardContent>
      </Card>

      {/* Results */}
      {!submitted ? (
        <EmptyState
          icon={Search}
          title="Vector Search"
          description="Enter a query to search across vector memory."
        />
      ) : searchQuery.isLoading ? (
        <div className="flex h-48 items-center justify-center">
          <Spinner size={24} />
        </div>
      ) : searchQuery.isError ? (
        <Card>
          <CardContent className="py-8 text-center text-sm text-destructive">
            Search failed. Please try again.
          </CardContent>
        </Card>
      ) : !searchQuery.data?.results.length ? (
        <EmptyState
          icon={Search}
          title="No Results"
          description="No vector memory entries matched your query."
        />
      ) : (
        <div className="space-y-3">
          <p className="text-sm text-muted-foreground">
            {searchQuery.data.count} result
            {searchQuery.data.count !== 1 ? 's' : ''} for &quot;{searchQuery.data.query}&quot;
          </p>
          {searchQuery.data.results.map((result: VectorMemoryResult) => (
            <Card key={`${result.namespace}/${result.key}`}>
              <CardContent className="py-4">
                <div className="flex items-start justify-between gap-4">
                  <div className="min-w-0 flex-1 space-y-1">
                    <div className="flex items-center gap-2">
                      <span className="font-medium">{result.key}</span>
                      <Badge variant="secondary" className="text-xs">
                        {result.namespace}
                      </Badge>
                      <Badge variant="outline" className="text-xs">
                        {result.memory_type}
                      </Badge>
                    </div>
                    <p className="line-clamp-2 text-sm text-muted-foreground">
                      {result.text}
                    </p>
                  </div>
                  <div className="flex flex-col items-end gap-1 text-xs text-muted-foreground">
                    <span>score: {result.score.toFixed(4)}</span>
                    <span>final: {result.final_score.toFixed(4)}</span>
                  </div>
                </div>
              </CardContent>
            </Card>
          ))}
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Main Page
// ---------------------------------------------------------------------------

export default function MemoryPage() {
  return (
    <div className="space-y-6">
      <div className="flex items-center gap-3">
        <Database className="h-6 w-6 text-primary" />
        <h1 className="text-2xl font-bold tracking-tight">Memory</h1>
      </div>

      <Tabs defaultValue="kv">
        <TabsList>
          <TabsTrigger value="kv">KV Browser</TabsTrigger>
          <TabsTrigger value="vector">Vector Search</TabsTrigger>
        </TabsList>
        <TabsContent value="kv">
          <KVBrowser />
        </TabsContent>
        <TabsContent value="vector">
          <VectorSearchTab />
        </TabsContent>
      </Tabs>
    </div>
  );
}
