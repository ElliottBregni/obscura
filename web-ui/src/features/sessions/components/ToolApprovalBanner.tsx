import { useState, useEffect } from 'react';
import { ShieldAlert, Check, X, ChevronDown, ChevronUp, Shield } from 'lucide-react';
import { Button } from '@/components/ui/Button';
import { useToolApprovals, useResolveApproval } from '@/api/hooks/useToolApprovals';

// ── Per-tool policy persistence (localStorage) ───────────────────────────────

const POLICY_KEY = 'obscura:tool_policy';

type ToolDecision = 'always_allow' | 'always_deny';

function loadPolicy(): Record<string, ToolDecision> {
  try {
    return JSON.parse(localStorage.getItem(POLICY_KEY) ?? '{}');
  } catch {
    return {};
  }
}

function savePolicy(policy: Record<string, ToolDecision>) {
  localStorage.setItem(POLICY_KEY, JSON.stringify(policy));
}

// ── Component ─────────────────────────────────────────────────────────────────

export function ToolApprovalBanner() {
  const { data: approvals = [], refetch } = useToolApprovals('pending');
  const resolve = useResolveApproval();
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [denyingId, setDenyingId] = useState<string | null>(null);
  const [denyReason, setDenyReason] = useState('');
  const [policy, setPolicy] = useState<Record<string, ToolDecision>>(loadPolicy);

  // Auto-resolve any approval whose tool has a stored policy
  useEffect(() => {
    for (const approval of approvals) {
      const decision = policy[approval.tool_name];
      if (decision === 'always_allow') {
        resolve.mutate({ id: approval.approval_id, approved: true });
      } else if (decision === 'always_deny') {
        resolve.mutate({
          id: approval.approval_id,
          approved: false,
          reason: 'Auto-denied by tool policy',
        });
      }
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [approvals.map((a) => a.approval_id).join(',')]);

  const updatePolicy = (toolName: string, decision: ToolDecision | null) => {
    const next = { ...policy };
    if (decision === null) {
      delete next[toolName];
    } else {
      next[toolName] = decision;
    }
    savePolicy(next);
    setPolicy(next);
    refetch();
  };

  // Filter out approvals that will be auto-resolved
  const visible = approvals.filter((a) => !policy[a.tool_name]);

  if (visible.length === 0) return null;

  return (
    <div className="mx-4 mb-3 space-y-2">
      {visible.map((approval) => {
        const isExpanded = expandedId === approval.approval_id;
        const isDenying = denyingId === approval.approval_id;

        return (
          <div
            key={approval.approval_id}
            className="rounded-lg border border-amber-500/40 bg-amber-500/10 text-sm"
          >
            {/* Header */}
            <div className="flex items-center gap-2 px-3 py-2.5">
              <ShieldAlert className="h-4 w-4 shrink-0 text-amber-500" />
              <span className="flex-1 font-medium text-amber-200">
                Approval required:{' '}
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
            <div className="flex flex-wrap items-center gap-2 border-t border-amber-500/20 px-3 py-2">
              <Button
                size="sm"
                className="h-7 bg-emerald-600 text-xs text-white hover:bg-emerald-500"
                disabled={resolve.isPending}
                onClick={() => resolve.mutate({ id: approval.approval_id, approved: true })}
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

              {/* Policy persistence */}
              <div className="ml-auto flex items-center gap-1">
                <Shield className="h-3 w-3 text-amber-500/60" />
                <span className="text-[10px] text-amber-500/60">Always:</span>
                <button
                  className="rounded px-1.5 py-0.5 text-[10px] text-emerald-400 hover:bg-emerald-500/20 transition-colors"
                  onClick={() => {
                    updatePolicy(approval.tool_name, 'always_allow');
                    resolve.mutate({ id: approval.approval_id, approved: true });
                  }}
                  title={`Always allow ${approval.tool_name}`}
                >
                  allow
                </button>
                <span className="text-amber-500/30">/</span>
                <button
                  className="rounded px-1.5 py-0.5 text-[10px] text-red-400 hover:bg-red-500/20 transition-colors"
                  onClick={() => {
                    updatePolicy(approval.tool_name, 'always_deny');
                    resolve.mutate({ id: approval.approval_id, approved: false, reason: 'Auto-denied by tool policy' });
                  }}
                  title={`Always deny ${approval.tool_name}`}
                >
                  deny
                </button>
              </div>
            </div>
          </div>
        );
      })}
    </div>
  );
}
