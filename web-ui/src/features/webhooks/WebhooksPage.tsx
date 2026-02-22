import { useCallback } from 'react';
import { Link } from 'react-router-dom';
import { toast } from 'sonner';
import {
  Webhook,
  Plus,
  Trash2,
  Zap,
  ToggleLeft,
  ToggleRight,
} from 'lucide-react';
import {
  useWebhooks,
  useDeleteWebhook,
  useTestWebhook,
} from '@/api/hooks/useWebhooks';
import type { Webhook as WebhookType } from '@/api/types';
import { Button } from '@/components/ui/Button';
import { Badge } from '@/components/ui/Badge';
import { Card, CardContent } from '@/components/ui/Card';
import {
  Table,
  TableHeader,
  TableBody,
  TableHead,
  TableRow,
  TableCell,
} from '@/components/ui/Table';
import { Spinner } from '@/components/ui/Spinner';
import { EmptyState } from '@/components/ui/EmptyState';
import { formatDate } from '@/lib/utils';

export default function WebhooksPage() {
  const webhooksQuery = useWebhooks();
  const deleteWebhook = useDeleteWebhook();
  const testWebhook = useTestWebhook();

  const handleDelete = useCallback(
    (wh: WebhookType) => {
      if (!confirm(`Delete webhook to "${wh.url}"?`)) return;
      deleteWebhook.mutate(wh.webhook_id, {
        onSuccess: () => toast.success('Webhook deleted'),
        onError: (err) => toast.error(`Delete failed: ${String(err)}`),
      });
    },
    [deleteWebhook],
  );

  const handleTest = useCallback(
    (wh: WebhookType) => {
      testWebhook.mutate(wh.webhook_id, {
        onSuccess: (result) => {
          if (result.success) {
            toast.success(
              `Test passed (status ${result.status_code ?? 'ok'})`,
            );
          } else {
            toast.error(
              `Test failed${result.status_code ? ` (status ${result.status_code})` : ''}`,
            );
          }
        },
        onError: (err) => toast.error(`Test failed: ${String(err)}`),
      });
    },
    [testWebhook],
  );

  if (webhooksQuery.isLoading) {
    return (
      <div className="flex h-64 items-center justify-center">
        <Spinner size={24} />
      </div>
    );
  }

  if (webhooksQuery.isError) {
    return (
      <Card>
        <CardContent className="py-12 text-center text-sm text-destructive">
          Failed to load webhooks.
        </CardContent>
      </Card>
    );
  }

  const webhooks = webhooksQuery.data ?? [];

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <Webhook className="h-6 w-6 text-primary" />
          <h1 className="text-2xl font-bold tracking-tight">Webhooks</h1>
          <Badge variant="secondary">{webhooks.length}</Badge>
        </div>
        <Button asChild>
          <Link to="/webhooks/create">
            <Plus className="mr-1.5 h-4 w-4" />
            Create
          </Link>
        </Button>
      </div>

      {/* Table */}
      {!webhooks.length ? (
        <EmptyState
          icon={Webhook}
          title="No Webhooks"
          description="Create a webhook to receive event notifications."
          action={
            <Button asChild>
              <Link to="/webhooks/create">
                <Plus className="mr-1.5 h-4 w-4" />
                Create Webhook
              </Link>
            </Button>
          }
        />
      ) : (
        <Card>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>URL</TableHead>
                <TableHead>Events</TableHead>
                <TableHead>Active</TableHead>
                <TableHead>Created</TableHead>
                <TableHead className="text-right">Actions</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {webhooks.map((wh) => (
                <TableRow key={wh.webhook_id}>
                  <TableCell className="max-w-[300px] truncate font-mono text-sm">
                    {wh.url}
                  </TableCell>
                  <TableCell>
                    <div className="flex flex-wrap gap-1">
                      {wh.events.map((event) => (
                        <Badge
                          key={event}
                          variant="secondary"
                          className="text-xs"
                        >
                          {event}
                        </Badge>
                      ))}
                    </div>
                  </TableCell>
                  <TableCell>
                    {wh.active ? (
                      <span className="flex items-center gap-1 text-sm text-emerald-500">
                        <ToggleRight className="h-4 w-4" /> Active
                      </span>
                    ) : (
                      <span className="flex items-center gap-1 text-sm text-muted-foreground">
                        <ToggleLeft className="h-4 w-4" /> Inactive
                      </span>
                    )}
                  </TableCell>
                  <TableCell className="text-sm text-muted-foreground">
                    {formatDate(wh.created_at)}
                  </TableCell>
                  <TableCell>
                    <div className="flex items-center justify-end gap-1">
                      <Button
                        variant="outline"
                        size="sm"
                        onClick={() => handleTest(wh)}
                        disabled={testWebhook.isPending}
                      >
                        <Zap className="mr-1.5 h-3.5 w-3.5" />
                        Test
                      </Button>
                      <Button
                        variant="ghost"
                        size="icon"
                        className="h-8 w-8"
                        onClick={() => handleDelete(wh)}
                      >
                        <Trash2 className="h-4 w-4 text-destructive" />
                      </Button>
                    </div>
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
