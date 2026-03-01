import { useState, useCallback } from 'react';
import { toast } from 'sonner';
import {
  ShieldCheck,
  Check,
  X,
  ChevronDown,
  ChevronRight,
} from 'lucide-react';
import {
  useToolApprovals,
  useResolveApproval,
} from '@/api/hooks/useToolApprovals';
import type { ToolApproval } from '@/api/types';
import { Tabs, TabsList, TabsTrigger, TabsContent } from '@/components/ui/Tabs';
import { Button } from '@/components/ui/Button';
import { Badge } from '@/components/ui/Badge';
import { Input } from '@/components/ui/Input';
import { Card, CardContent } from '@/components/ui/Card';
import {
  Table,
  TableHeader,
  TableBody,
  TableHead,
  TableRow,
  TableCell,
} from '@/components/ui/Table';
import { JsonViewer } from '@/components/ui/JsonViewer';
import { Spinner } from '@/components/ui/Spinner';
import { EmptyState } from '@/components/ui/EmptyState';
import { StatusBadge } from '@/components/ui/StatusBadge';
import { formatDate, formatRelative } from '@/lib/utils';

// ---------------------------------------------------------------------------
// Collapsible tool input
// ---------------------------------------------------------------------------

function CollapsibleInput({ data }: { data: Record<string, unknown> }) {
  const [open, setOpen] = useState(false);
  return (
    <div>
      <button
        type="button"
        className="flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground"
        onClick={() => setOpen(!open)}
      >
        {open ? (
          <ChevronDown className="h-3 w-3" />
        ) : (
          <ChevronRight className="h-3 w-3" />
        )}
        Tool Input
      </button>
      {open && <JsonViewer data={data} collapsed className="mt-2" />}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Pending tab
// ---------------------------------------------------------------------------

function PendingTab() {
  const pendingQuery = useToolApprovals('pending');
  const resolveApproval = useResolveApproval();
  const [denyReasons, setDenyReasons] = useState<Record<string, string>>({});

  const handleApprove = useCallback(
    (id: string) => {
      resolveApproval.mutate(
        { id, approved: true },
        {
          onSuccess: () => toast.success('Approval granted'),
          onError: (err) => toast.error(`Approve failed: ${String(err)}`),
        },
      );
    },
    [resolveApproval],
  );

  const handleDeny = useCallback(
    (id: string) => {
      resolveApproval.mutate(
        { id, approved: false, reason: denyReasons[id] || undefined },
        {
          onSuccess: () => toast.success('Approval denied'),
          onError: (err) => toast.error(`Deny failed: ${String(err)}`),
        },
      );
    },
    [resolveApproval, denyReasons],
  );

  if (pendingQuery.isLoading) {
    return (
      <div className="flex h-48 items-center justify-center">
        <Spinner size={24} />
      </div>
    );
  }

  if (pendingQuery.isError) {
    return (
      <Card>
        <CardContent className="py-12 text-center text-sm text-destructive">
          Failed to load pending approvals.
        </CardContent>
      </Card>
    );
  }

  const approvals = pendingQuery.data ?? [];

  if (!approvals.length) {
    return (
      <EmptyState
        icon={ShieldCheck}
        title="No Pending Approvals"
        description="All tool approval requests have been resolved."
      />
    );
  }

  return (
    <div className="space-y-4">
      {approvals.map((approval: ToolApproval) => (
        <Card key={approval.approval_id}>
          <CardContent className="space-y-4 py-4">
            <div className="flex items-start justify-between">
              <div className="space-y-1">
                <div className="flex items-center gap-2">
                  <span className="font-medium">{approval.tool_name}</span>
                  <Badge variant="secondary" className="text-xs">
                    {approval.agent_id}
                  </Badge>
                </div>
                <p className="text-xs text-muted-foreground">
                  Requested {formatRelative(approval.created_at)}
                </p>
              </div>
              <StatusBadge status={approval.status} />
            </div>

            <CollapsibleInput data={approval.tool_input} />

            <div className="flex items-center gap-3">
              <Button
                size="sm"
                className="bg-emerald-600 text-white hover:bg-emerald-700"
                onClick={() => handleApprove(approval.approval_id)}
                disabled={resolveApproval.isPending}
              >
                <Check className="mr-1.5 h-3.5 w-3.5" />
                Approve
              </Button>
              <div className="flex flex-1 items-center gap-2">
                <Input
                  placeholder="Reason (optional)"
                  className="h-9 text-sm"
                  value={denyReasons[approval.approval_id] ?? ''}
                  onChange={(e) =>
                    setDenyReasons((prev) => ({
                      ...prev,
                      [approval.approval_id]: e.target.value,
                    }))
                  }
                />
                <Button
                  size="sm"
                  variant="destructive"
                  onClick={() => handleDeny(approval.approval_id)}
                  disabled={resolveApproval.isPending}
                >
                  <X className="mr-1.5 h-3.5 w-3.5" />
                  Deny
                </Button>
              </div>
            </div>
          </CardContent>
        </Card>
      ))}
    </div>
  );
}

// ---------------------------------------------------------------------------
// History tab
// ---------------------------------------------------------------------------

function HistoryTab() {
  const approvedQuery = useToolApprovals('approved');
  const deniedQuery = useToolApprovals('denied');

  const isLoading = approvedQuery.isLoading || deniedQuery.isLoading;
  const isError = approvedQuery.isError || deniedQuery.isError;

  const history = [
    ...(approvedQuery.data ?? []),
    ...(deniedQuery.data ?? []),
  ].sort(
    (a, b) =>
      new Date(b.resolved_at ?? b.created_at).getTime() -
      new Date(a.resolved_at ?? a.created_at).getTime(),
  );

  if (isLoading) {
    return (
      <div className="flex h-48 items-center justify-center">
        <Spinner size={24} />
      </div>
    );
  }

  if (isError) {
    return (
      <Card>
        <CardContent className="py-12 text-center text-sm text-destructive">
          Failed to load approval history.
        </CardContent>
      </Card>
    );
  }

  if (!history.length) {
    return (
      <EmptyState
        icon={ShieldCheck}
        title="No History"
        description="No resolved approvals yet."
      />
    );
  }

  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>Tool</TableHead>
          <TableHead>Agent</TableHead>
          <TableHead>Status</TableHead>
          <TableHead>Resolved</TableHead>
          <TableHead>Reason</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {history.map((a) => (
          <TableRow key={a.approval_id}>
            <TableCell className="font-medium">{a.tool_name}</TableCell>
            <TableCell className="text-sm text-muted-foreground">
              {a.agent_id}
            </TableCell>
            <TableCell>
              <StatusBadge status={a.status} />
            </TableCell>
            <TableCell className="text-sm text-muted-foreground">
              {a.resolved_at ? formatDate(a.resolved_at) : '--'}
            </TableCell>
            <TableCell className="max-w-[200px] truncate text-sm text-muted-foreground">
              {a.reason ?? '--'}
            </TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  );
}

// ---------------------------------------------------------------------------
// Main Page
// ---------------------------------------------------------------------------

export default function ToolApprovalsPage() {
  return (
    <div className="space-y-6">
      <div className="flex items-center gap-3">
        <ShieldCheck className="h-6 w-6 text-primary" />
        <h1 className="text-2xl font-bold tracking-tight">Tool Approvals</h1>
      </div>

      <Tabs defaultValue="pending">
        <TabsList>
          <TabsTrigger value="pending">Pending</TabsTrigger>
          <TabsTrigger value="history">History</TabsTrigger>
        </TabsList>
        <TabsContent value="pending">
          <PendingTab />
        </TabsContent>
        <TabsContent value="history">
          <HistoryTab />
        </TabsContent>
      </Tabs>
    </div>
  );
}
