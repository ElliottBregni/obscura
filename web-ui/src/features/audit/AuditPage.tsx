import { useState, useCallback } from 'react';
import { FileText, ChevronLeft, ChevronRight } from 'lucide-react';
import { useAuditLogs, useAuditSummary } from '@/api/hooks/useAudit';
import { Button } from '@/components/ui/Button';
import { Badge } from '@/components/ui/Badge';
import { Input } from '@/components/ui/Input';
import { Label } from '@/components/ui/Label';
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from '@/components/ui/Card';
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
import { StatusBadge } from '@/components/ui/StatusBadge';
import { formatDate } from '@/lib/utils';

const PAGE_SIZE = 25;

export default function AuditPage() {
  const [offset, setOffset] = useState(0);
  const [actionFilter, setActionFilter] = useState('');
  const [userFilter, setUserFilter] = useState('');

  const summaryQuery = useAuditSummary();
  const logsQuery = useAuditLogs(PAGE_SIZE, offset, {
    action: actionFilter || undefined,
    user_id: userFilter || undefined,
  });

  const handlePrev = useCallback(() => {
    setOffset((prev) => Math.max(0, prev - PAGE_SIZE));
  }, []);

  const handleNext = useCallback(() => {
    setOffset((prev) => prev + PAGE_SIZE);
  }, []);

  const handleApplyFilters = useCallback(() => {
    setOffset(0);
  }, []);

  const summary = summaryQuery.data;
  const logs = logsQuery.data?.logs ?? [];
  const total = logsQuery.data?.total ?? 0;

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center gap-3">
        <FileText className="h-6 w-6 text-primary" />
        <h1 className="text-2xl font-bold tracking-tight">Audit Log</h1>
      </div>

      {/* Summary cards */}
      {summaryQuery.isLoading ? (
        <div className="flex h-24 items-center justify-center">
          <Spinner size={20} />
        </div>
      ) : summary ? (
        <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
          <Card>
            <CardHeader className="pb-2">
              <CardTitle className="text-xs font-medium text-muted-foreground">
                Total Events
              </CardTitle>
            </CardHeader>
            <CardContent>
              <p className="text-2xl font-bold">{summary.total_logs}</p>
            </CardContent>
          </Card>
          <Card>
            <CardHeader className="pb-2">
              <CardTitle className="text-xs font-medium text-muted-foreground">
                Successes
              </CardTitle>
            </CardHeader>
            <CardContent>
              <p className="text-2xl font-bold text-emerald-500">
                {summary.outcomes?.success ?? 0}
              </p>
            </CardContent>
          </Card>
          <Card>
            <CardHeader className="pb-2">
              <CardTitle className="text-xs font-medium text-muted-foreground">
                Failures
              </CardTitle>
            </CardHeader>
            <CardContent>
              <p className="text-2xl font-bold text-red-500">
                {summary.outcomes?.denied ?? 0}
              </p>
            </CardContent>
          </Card>
          <Card>
            <CardHeader className="pb-2">
              <CardTitle className="text-xs font-medium text-muted-foreground">
                Last 24h
              </CardTitle>
            </CardHeader>
            <CardContent>
              <p className="text-2xl font-bold">
                {summary.last_24h}
              </p>
            </CardContent>
          </Card>
        </div>
      ) : null}

      {/* Filter bar */}
      <Card>
        <CardContent className="pt-6">
          <div className="flex flex-wrap items-end gap-4">
            <div className="min-w-[180px] space-y-1">
              <Label htmlFor="filter-action" className="text-xs">
                Action
              </Label>
              <Input
                id="filter-action"
                value={actionFilter}
                onChange={(e) => setActionFilter(e.target.value)}
                placeholder="e.g. agent.spawn"
                className="h-9"
              />
            </div>
            <div className="min-w-[180px] space-y-1">
              <Label htmlFor="filter-user" className="text-xs">
                User
              </Label>
              <Input
                id="filter-user"
                value={userFilter}
                onChange={(e) => setUserFilter(e.target.value)}
                placeholder="user_id"
                className="h-9"
              />
            </div>
            <Button size="sm" onClick={handleApplyFilters}>
              Apply
            </Button>
          </div>
        </CardContent>
      </Card>

      {/* Table */}
      {logsQuery.isLoading ? (
        <div className="flex h-48 items-center justify-center">
          <Spinner size={24} />
        </div>
      ) : logsQuery.isError ? (
        <Card>
          <CardContent className="py-12 text-center text-sm text-destructive">
            Failed to load audit logs.
          </CardContent>
        </Card>
      ) : !logs.length ? (
        <EmptyState
          icon={FileText}
          title="No Audit Events"
          description="No audit events match the current filters."
        />
      ) : (
        <>
          <Card>
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Timestamp</TableHead>
                  <TableHead>User</TableHead>
                  <TableHead>Event</TableHead>
                  <TableHead>Resource</TableHead>
                  <TableHead>Action</TableHead>
                  <TableHead>Outcome</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {logs.map((entry, i) => (
                  <TableRow key={`${entry.timestamp}-${i}`}>
                    <TableCell className="whitespace-nowrap text-sm text-muted-foreground">
                      {formatDate(entry.timestamp)}
                    </TableCell>
                    <TableCell className="text-sm">{entry.user_id}</TableCell>
                    <TableCell>
                      <Badge variant="outline" className="text-xs">
                        {entry.event_type}
                      </Badge>
                    </TableCell>
                    <TableCell className="max-w-[160px] truncate font-mono text-xs text-muted-foreground">
                      {entry.resource}
                    </TableCell>
                    <TableCell className="text-sm">
                      {entry.action}
                    </TableCell>
                    <TableCell>
                      <StatusBadge status={entry.outcome} />
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </Card>

          {/* Pagination */}
          <div className="flex items-center justify-between">
            <p className="text-sm text-muted-foreground">
              Showing {offset + 1}&ndash;{Math.min(offset + PAGE_SIZE, total)}{' '}
              of {total}
            </p>
            <div className="flex items-center gap-2">
              <Button
                variant="outline"
                size="sm"
                onClick={handlePrev}
                disabled={offset === 0}
              >
                <ChevronLeft className="mr-1 h-4 w-4" />
                Prev
              </Button>
              <Button
                variant="outline"
                size="sm"
                onClick={handleNext}
                disabled={offset + PAGE_SIZE >= total}
              >
                Next
                <ChevronRight className="ml-1 h-4 w-4" />
              </Button>
            </div>
          </div>
        </>
      )}
    </div>
  );
}
