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
  const [rph, setRph] = useState('');

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (!apiKey || !rpm || !rph) return;
    setLimit.mutate(
      {
        api_key: apiKey,
        requests_per_minute: Number(rpm),
        requests_per_hour: Number(rph),
      },
      {
        onSuccess: () => {
          setApiKey('');
          setRpm('');
          setRph('');
        },
      }
    );
  };

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-3xl font-bold tracking-tight">Rate Limits</h1>
        <p className="mt-1 text-muted-foreground">
          Configure per-key request throttling for the API.
        </p>
      </div>

      <Card>
        <CardHeader>
          <CardTitle className="text-lg">Set Rate Limit</CardTitle>
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
              <Label htmlFor="rl-rpm">Requests / min</Label>
              <Input
                id="rl-rpm"
                type="number"
                min={0}
                placeholder="60"
                value={rpm}
                onChange={(e) => setRpm(e.target.value)}
                className="w-36"
              />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="rl-rph">Requests / hr</Label>
              <Input
                id="rl-rph"
                type="number"
                min={0}
                placeholder="1000"
                value={rph}
                onChange={(e) => setRph(e.target.value)}
                className="w-36"
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
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>API Key</TableHead>
                <TableHead className="text-right">Req / min</TableHead>
                <TableHead className="text-right">Req / hr</TableHead>
                <TableHead className="w-24" />
              </TableRow>
            </TableHeader>
            <TableBody>
              {limits.length === 0 && (
                <TableRow>
                  <TableCell
                    colSpan={4}
                    className="text-center text-muted-foreground"
                  >
                    No rate limits configured.
                  </TableCell>
                </TableRow>
              )}
              {limits.map((limit) => (
                <TableRow key={limit.api_key}>
                  <TableCell className="font-mono text-xs">
                    {limit.api_key}
                  </TableCell>
                  <TableCell className="text-right">
                    {limit.requests_per_minute}
                  </TableCell>
                  <TableCell className="text-right">
                    {limit.requests_per_hour}
                  </TableCell>
                  <TableCell>
                    <Button
                      variant="destructive"
                      size="sm"
                      disabled={deleteLimit.isPending}
                      onClick={() => deleteLimit.mutate(limit.api_key)}
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
