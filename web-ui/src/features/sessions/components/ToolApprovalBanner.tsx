import { useState } from 'react';
import { ShieldAlert, Check, X, ChevronDown, ChevronUp } from 'lucide-react';
import { Button } from '@/components/ui/Button';
import { useToolApprovals, useResolveApproval } from '@/api/hooks/useToolApprovals';

/**
 * Polls for pending tool approvals and renders an inline banner in the chat.
 * Approvals are scoped to the current session via agent_id (best effort — we
 * show all pending if agent_id isn't known yet, which is harmless).
 */
export function ToolApprovalBanner() {
  const { data: approvals = [] } = useToolApprovals('pending');
  const resolve = useResolveApproval();
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [denyingId, setDenyingId] = useState<string | null>(null);
  const [denyReason, setDenyReason] = useState('');

  if (approvals.length === 0) return null;

  return (
    <div className="mx-4 mb-3 space-y-2">
      {approvals.map((approval) => {
        const isExpanded = expandedId === approval.approval_id;
        const isDenying = denyingId === approval.approval_id;

        return (
          <div
            key={approval.approval_id}
            className="rounded-lg border border-amber-500/40 bg-amber-500/10 text-sm"
          >
            {/* Header row */}
            <div className="flex items-center gap-2 px-3 py-2.5">
              <ShieldAlert className="h-4 w-4 shrink-0 text-amber-500" />
              <span className="flex-1 font-medium text-amber-200">
                Tool approval required:{' '}
                <code className="rounded bg-amber-500/20 px-1 py-0.5 font-mono text-xs">
                  {approval.tool_name}
                </code>
              </span>
              <button
                onClick={() => setExpandedId(isExpanded ? null : approval.approval_id)}
                className="text-amber-400 hover:text-amber-200 transition-colors"
                title={isExpanded ? 'Collapse' : 'Show input'}
              >
                {isExpanded ? (
                  <ChevronUp className="h-4 w-4" />
                ) : (
                  <ChevronDown className="h-4 w-4" />
                )}
              </button>
            </div>

            {/* Expanded input */}
            {isExpanded && (
              <div className="border-t border-amber-500/20 px-3 py-2">
                <pre className="max-h-40 overflow-y-auto whitespace-pre-wrap break-words rounded bg-black/30 p-2 font-mono text-[11px] text-amber-100/80">
                  {JSON.stringify(approval.tool_input, null, 2)}
                </pre>
              </div>
            )}

            {/* Deny reason input */}
            {isDenying && (
              <div className="border-t border-amber-500/20 px-3 py-2">
                <input
                  type="text"
                  placeholder="Reason (optional)"
                  value={denyReason}
                  onChange={(e) => setDenyReason(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter') {
                      resolve.mutate(
                        { id: approval.approval_id, approved: false, reason: denyReason || undefined },
                        { onSuccess: () => { setDenyingId(null); setDenyReason(''); } }
                      );
                    }
                    if (e.key === 'Escape') { setDenyingId(null); setDenyReason(''); }
                  }}
                  autoFocus
                  className="w-full rounded border border-amber-500/30 bg-black/30 px-2 py-1 text-xs text-amber-100 placeholder-amber-500/50 outline-none focus:border-amber-400"
                />
              </div>
            )}

            {/* Action buttons */}
            <div className="flex items-center gap-2 border-t border-amber-500/20 px-3 py-2">
              <Button
                size="sm"
                className="h-7 bg-emerald-600 text-xs text-white hover:bg-emerald-500"
                disabled={resolve.isPending}
                onClick={() =>
                  resolve.mutate({ id: approval.approval_id, approved: true })
                }
              >
                <Check className="mr-1 h-3 w-3" />
                Approve
              </Button>
              {isDenying ? (
                <Button
                  size="sm"
                  variant="destructive"
                  className="h-7 text-xs"
                  disabled={resolve.isPending}
                  onClick={() =>
                    resolve.mutate(
                      { id: approval.approval_id, approved: false, reason: denyReason || undefined },
                      { onSuccess: () => { setDenyingId(null); setDenyReason(''); } }
                    )
                  }
                >
                  Deny
                </Button>
              ) : (
                <Button
                  size="sm"
                  variant="ghost"
                  className="h-7 text-xs text-amber-400 hover:text-amber-200 hover:bg-amber-500/10"
                  onClick={() => setDenyingId(approval.approval_id)}
                >
                  <X className="mr-1 h-3 w-3" />
                  Deny
                </Button>
              )}
              <span className="ml-auto text-[10px] text-amber-500/60">
                agent: {approval.agent_id.slice(0, 12)}…
              </span>
            </div>
          </div>
        );
      })}
    </div>
  );
}
