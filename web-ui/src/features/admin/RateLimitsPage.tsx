import { useState } from 'react';
import {
  useRateLimits,
  useSetRateLimit,
  useDeleteRateLimit,
} from '@/api/hooks/useRateLimits';
import { Button } from '@/components/ui/Button';
import { Input } from '@/components/ui/Input';
import { Label } from '@/components/ui/Label';
import {
  Card,
  CardHeader,
  CardTitle,
  CardContent,
} from '@/components/ui/Card';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/Table';
import { Spinner } from '@/components/ui/Spinner';

export default function RateLimitsPage() {
  const { data: limits, isLoading, error } = useRateLimits();
  const setLimit = useSetRateLimit();
  const deleteLimit = useDeleteRateLimit();

  const [apiKey, setApiKey] = useState('');
  const [rpm, setRpm] = useState('');
  const [concurrentAgents, setConcurrentAgents] = useState('');
  const [memoryQuota, setMemoryQuota] = useState('');

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (!apiKey) return;
    setLimit.mutate(
      {
        api_key: apiKey,
        ...(rpm ? { requests_per_minute: Number(rpm) } : {}),
        ...(concurrentAgents ? { concurrent_agents: Number(concurrentAgents) } : {}),
        ...(memoryQuota ? { memory_quota_mb: Number(memoryQuota) } : {}),
      },
      {
        onSuccess: () => {
          setApiKey('');
          setRpm('');
          setConcurrentAgents('');
          setMemoryQuota('');
        },
      }
    );
  };

  const customEntries = limits
    ? Object.entries(limits.custom).map(([key, cfg]) => ({ api_key: key, ...cfg }))
    : [];

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-3xl font-bold tracking-tight">Rate Limits</h1>
        <p className="mt-1 text-muted-foreground">
          Configure per-key request throttling for the API.
        </p>
      </div>

      {/* Default limits */}
      {limits && (
        <Card>
          <CardHeader>
            <CardTitle className="text-lg">Default Limits</CardTitle>
          </CardHeader>
          <CardContent>
            <div className="flex gap-8 text-sm">
              <div>
                <span className="text-muted-foreground">Requests / min:</span>{' '}
                <span className="font-semibold">{limits.default.requests_per_minute}</span>
              </div>
              <div>
                <span className="text-muted-foreground">Concurrent agents:</span>{' '}
                <span className="font-semibold">{limits.default.concurrent_agents}</span>
              </div>
              <div>
                <span className="text-muted-foreground">Memory quota:</span>{' '}
                <span className="font-semibold">{limits.default.memory_quota_mb} MB</span>
              </div>
            </div>
          </CardContent>
        </Card>
      )}

      <Card>
        <CardHeader>
          <CardTitle className="text-lg">Set Custom Limit</CardTitle>
        </CardHeader>
        <CardContent>
          <form onSubmit={handleSubmit} className="flex flex-wrap items-end gap-4">
            <div className="space-y-1.5">
              <Label htmlFor="rl-key">API Key</Label>
              <Input
                id="rl-key"
                placeholder="sk-..."
                value={apiKey}
                onChange={(e) => setApiKey(e.target.value)}
                className="w-56"
              />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="rl-rpm">Req / min</Label>
              <Input
                id="rl-rpm"
                type="number"
                min={0}
                placeholder="100"
                value={rpm}
                onChange={(e) => setRpm(e.target.value)}
                className="w-32"
              />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="rl-agents">Concurrent agents</Label>
              <Input
                id="rl-agents"
                type="number"
                min={0}
                placeholder="10"
                value={concurrentAgents}
                onChange={(e) => setConcurrentAgents(e.target.value)}
                className="w-32"
              />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="rl-memory">Memory (MB)</Label>
              <Input
                id="rl-memory"
                type="number"
                min={0}
                placeholder="1024"
                value={memoryQuota}
                onChange={(e) => setMemoryQuota(e.target.value)}
                className="w-32"
              />
            </div>
            <Button type="submit" disabled={setLimit.isPending}>
              {setLimit.isPending ? 'Saving...' : 'Set Limit'}
            </Button>
          </form>
          {setLimit.isError && (
            <p className="mt-2 text-sm text-red-500">
              {(setLimit.error as Error).message}
            </p>
          )}
        </CardContent>
      </Card>

      {isLoading && (
        <div className="flex items-center justify-center py-12">
          <Spinner size={32} />
        </div>
      )}

      {error && (
        <p className="text-sm text-red-500">
          Failed to load rate limits: {(error as Error).message}
        </p>
      )}

      {limits && (
        <Card>
          <CardHeader>
            <CardTitle className="text-lg">Custom Limits</CardTitle>
          </CardHeader>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>API Key</TableHead>
                <TableHead className="text-right">Req / min</TableHead>
                <TableHead className="text-right">Concurrent</TableHead>
                <TableHead className="text-right">Memory (MB)</TableHead>
                <TableHead className="w-24" />
              </TableRow>
            </TableHeader>
            <TableBody>
              {customEntries.length === 0 && (
                <TableRow>
                  <TableCell
                    colSpan={5}
                    className="text-center text-muted-foreground"
                  >
                    No custom rate limits configured.
                  </TableCell>
                </TableRow>
              )}
              {customEntries.map((entry) => (
                <TableRow key={entry.api_key}>
                  <TableCell className="font-mono text-xs">
                    {entry.api_key}
                  </TableCell>
                  <TableCell className="text-right">
                    {entry.requests_per_minute}
                  </TableCell>
                  <TableCell className="text-right">
                    {entry.concurrent_agents}
                  </TableCell>
                  <TableCell className="text-right">
                    {entry.memory_quota_mb}
                  </TableCell>
                  <TableCell>
                    <Button
                      variant="destructive"
                      size="sm"
                      disabled={deleteLimit.isPending}
                      onClick={() => deleteLimit.mutate(entry.api_key)}
                    >
                      Delete
                    </Button>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </Card>
      )}
    </div>
  );
}
