import { useState, useMemo, useRef, useEffect } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import {
  Target,
  Plus,
  X,
  Pause,
  Play,
  XCircle,
  CheckCircle2,
  Clock,
  AlertCircle,
} from 'lucide-react';
import { fetchApi } from '@/api/client';
import { Button } from '@/components/ui/Button';
import { Spinner } from '@/components/ui/Spinner';
import { EmptyState } from '@/components/ui/EmptyState';
import { cn, formatRelative } from '@/lib/utils';

// ─── Types ──────────────────────────────────────────────────────────────────

type GoalStatus =
  | 'pending'
  | 'active'
  | 'paused'
  | 'completed'
  | 'failed'
  | 'cancelled';

interface Goal {
  goal_id: string;
  title: string;
  description: string;
  status: GoalStatus;
  created_at: string;
  completed_at: string | null;
  success_criteria: string[];
}

interface GoalsResponse {
  goals: Goal[];
}

interface CreateGoalRequest {
  title: string;
  description: string;
  success_criteria: string[];
  budget: { max_turns: number };
}

// ─── Status helpers ──────────────────────────────────────────────────────────

const STATUS_DOT_CLASS: Record<GoalStatus, string> = {
  active: 'status-running',
  pending: 'status-idle',
  paused: 'status-idle',
  completed: 'status-running',
  failed: 'status-error',
  cancelled: 'status-stopped',
};

const STATUS_LABEL: Record<GoalStatus, string> = {
  active: 'Active',
  pending: 'Pending',
  paused: 'Paused',
  completed: 'Completed',
  failed: 'Failed',
  cancelled: 'Cancelled',
};

function StatusDot({ status }: { status: GoalStatus }) {
  return (
    <span
      className={cn(
        'inline-block h-2 w-2 flex-shrink-0 rounded-full',
        STATUS_DOT_CLASS[status],
        status === 'completed' && 'opacity-50'
      )}
    />
  );
}

function StatusIcon({ status }: { status: GoalStatus }) {
  const cls = 'h-3.5 w-3.5';
  switch (status) {
    case 'active':
      return <Play className={cn(cls, 'text-emerald-400')} />;
    case 'pending':
      return <Clock className={cn(cls, 'text-sky-400')} />;
    case 'paused':
      return <Pause className={cn(cls, 'text-sky-400')} />;
    case 'completed':
      return <CheckCircle2 className={cn(cls, 'text-emerald-400')} />;
    case 'failed':
      return <AlertCircle className={cn(cls, 'text-red-400')} />;
    case 'cancelled':
      return <XCircle className={cn(cls, 'text-zinc-400')} />;
  }
}

// ─── Filter tabs ─────────────────────────────────────────────────────────────

type FilterTab = 'all' | 'active' | 'completed' | 'failed';

const FILTER_TABS: { label: string; value: FilterTab }[] = [
  { label: 'All', value: 'all' },
  { label: 'Active', value: 'active' },
  { label: 'Completed', value: 'completed' },
  { label: 'Failed', value: 'failed' },
];

function filterGoals(goals: Goal[], tab: FilterTab): Goal[] {
  if (tab === 'all') return goals;
  if (tab === 'active') {
    return goals.filter(
      (g) => g.status === 'active' || g.status === 'pending' || g.status === 'paused'
    );
  }
  if (tab === 'completed') return goals.filter((g) => g.status === 'completed');
  if (tab === 'failed') {
    return goals.filter((g) => g.status === 'failed' || g.status === 'cancelled');
  }
  return goals;
}

// ─── API hooks ───────────────────────────────────────────────────────────────

function useGoals() {
  return useQuery({
    queryKey: ['goals'],
    queryFn: () => fetchApi<GoalsResponse>('/api/goals'),
    refetchInterval: 8000,
  });
}

function useCreateGoal() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (req: CreateGoalRequest) =>
      fetchApi<Goal>('/api/goals', {
        method: 'POST',
        body: JSON.stringify(req),
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['goals'] });
    },
  });
}

function usePauseGoal() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: string) =>
      fetchApi<Goal>(`/api/goals/${id}/pause`, { method: 'POST' }),
    onMutate: async (id) => {
      await queryClient.cancelQueries({ queryKey: ['goals'] });
      const prev = queryClient.getQueryData<GoalsResponse>(['goals']);
      queryClient.setQueryData<GoalsResponse>(['goals'], (old) =>
        old
          ? {
              ...old,
              goals: old.goals.map((g) =>
                g.goal_id === id ? { ...g, status: 'paused' as GoalStatus } : g
              ),
            }
          : old
      );
      return { prev };
    },
    onError: (_err, _id, ctx) => {
      if (ctx?.prev) queryClient.setQueryData(['goals'], ctx.prev);
    },
    onSettled: () => {
      queryClient.invalidateQueries({ queryKey: ['goals'] });
    },
  });
}

function useResumeGoal() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: string) =>
      fetchApi<Goal>(`/api/goals/${id}/resume`, { method: 'POST' }),
    onMutate: async (id) => {
      await queryClient.cancelQueries({ queryKey: ['goals'] });
      const prev = queryClient.getQueryData<GoalsResponse>(['goals']);
      queryClient.setQueryData<GoalsResponse>(['goals'], (old) =>
        old
          ? {
              ...old,
              goals: old.goals.map((g) =>
                g.goal_id === id ? { ...g, status: 'active' as GoalStatus } : g
              ),
            }
          : old
      );
      return { prev };
    },
    onError: (_err, _id, ctx) => {
      if (ctx?.prev) queryClient.setQueryData(['goals'], ctx.prev);
    },
    onSettled: () => {
      queryClient.invalidateQueries({ queryKey: ['goals'] });
    },
  });
}

function useCancelGoal() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: string) =>
      fetchApi<Goal>(`/api/goals/${id}/cancel`, { method: 'POST' }),
    onMutate: async (id) => {
      await queryClient.cancelQueries({ queryKey: ['goals'] });
      const prev = queryClient.getQueryData<GoalsResponse>(['goals']);
      queryClient.setQueryData<GoalsResponse>(['goals'], (old) =>
        old
          ? {
              ...old,
              goals: old.goals.map((g) =>
                g.goal_id === id ? { ...g, status: 'cancelled' as GoalStatus } : g
              ),
            }
          : old
      );
      return { prev };
    },
    onError: (_err, _id, ctx) => {
      if (ctx?.prev) queryClient.setQueryData(['goals'], ctx.prev);
    },
    onSettled: () => {
      queryClient.invalidateQueries({ queryKey: ['goals'] });
    },
  });
}

// ─── Create Goal Form ─────────────────────────────────────────────────────────

interface CreateGoalFormProps {
  onClose: () => void;
}

function CreateGoalForm({ onClose }: CreateGoalFormProps) {
  const createGoal = useCreateGoal();

  const [title, setTitle] = useState('');
  const [description, setDescription] = useState('');
  const [criteria, setCriteria] = useState<string[]>([]);
  const [criterionInput, setCriterionInput] = useState('');
  const [maxTurns, setMaxTurns] = useState('');
  const [titleError, setTitleError] = useState('');

  const titleRef = useRef<HTMLInputElement>(null);
  useEffect(() => {
    titleRef.current?.focus();
  }, []);

  function addCriterion() {
    const val = criterionInput.trim();
    if (!val) return;
    setCriteria((prev) => [...prev, val]);
    setCriterionInput('');
  }

  function removeCriterion(idx: number) {
    setCriteria((prev) => prev.filter((_, i) => i !== idx));
  }

  function handleCriterionKeyDown(e: React.KeyboardEvent<HTMLInputElement>) {
    if (e.key === 'Enter') {
      e.preventDefault();
      addCriterion();
    }
  }

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!title.trim()) {
      setTitleError('Title is required.');
      titleRef.current?.focus();
      return;
    }
    setTitleError('');
    const parsed = parseInt(maxTurns, 10);
    const turnsNum = maxTurns === '' || isNaN(parsed) ? 0 : parsed;
    createGoal.mutate(
      {
        title: title.trim(),
        description: description.trim(),
        success_criteria: criteria,
        budget: { max_turns: turnsNum },
      },
      {
        onSuccess: () => {
          onClose();
        },
      }
    );
  }

  return (
    <form
      onSubmit={handleSubmit}
      className="animate-in rounded-xl border border-border bg-card p-5 shadow-md"
    >
      <div className="mb-4 flex items-center justify-between">
        <h2 className="text-sm font-semibold text-foreground">New Goal</h2>
        <button
          type="button"
          onClick={onClose}
          className="rounded p-1 text-muted-foreground hover:text-foreground focus:outline-none"
          aria-label="Close form"
        >
          <X className="h-4 w-4" />
        </button>
      </div>

      <div className="space-y-4">
        {/* Title */}
        <div>
          <label
            htmlFor="goal-title"
            className="mb-1.5 block text-xs font-medium text-foreground"
          >
            Title <span className="text-destructive">*</span>
          </label>
          <input
            id="goal-title"
            ref={titleRef}
            type="text"
            value={title}
            onChange={(e) => {
              setTitle(e.target.value);
              if (titleError) setTitleError('');
            }}
            placeholder="e.g. Analyse Q1 performance data"
            className={cn(
              'w-full rounded-md border bg-background px-3 py-2 text-sm text-foreground',
              'placeholder:text-muted-foreground focus:outline-none focus:ring-1 focus:ring-ring',
              titleError ? 'border-destructive' : 'border-input'
            )}
          />
          {titleError && (
            <p className="mt-1 text-xs text-destructive">{titleError}</p>
          )}
        </div>

        {/* Description */}
        <div>
          <label
            htmlFor="goal-desc"
            className="mb-1.5 block text-xs font-medium text-foreground"
          >
            Description
          </label>
          <textarea
            id="goal-desc"
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            rows={3}
            placeholder="Optional — describe the goal in more detail"
            className="w-full resize-none rounded-md border border-input bg-background px-3 py-2 text-sm text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-1 focus:ring-ring"
          />
        </div>

        {/* Success criteria */}
        <div>
          <label className="mb-1.5 block text-xs font-medium text-foreground">
            Success Criteria
          </label>
          {criteria.length > 0 && (
            <div className="mb-2 flex flex-wrap gap-1.5">
              {criteria.map((c, i) => (
                <span
                  key={i}
                  className="inline-flex items-center gap-1 rounded-full border border-border bg-secondary px-2.5 py-0.5 text-xs text-secondary-foreground"
                >
                  {c}
                  <button
                    type="button"
                    onClick={() => removeCriterion(i)}
                    className="ml-0.5 rounded-full text-muted-foreground hover:text-foreground focus:outline-none"
                    aria-label={`Remove criterion: ${c}`}
                  >
                    <X className="h-3 w-3" />
                  </button>
                </span>
              ))}
            </div>
          )}
          <div className="flex gap-2">
            <input
              type="text"
              value={criterionInput}
              onChange={(e) => setCriterionInput(e.target.value)}
              onKeyDown={handleCriterionKeyDown}
              placeholder="Add a criterion and press Enter"
              className="flex-1 rounded-md border border-input bg-background px-3 py-2 text-sm text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-1 focus:ring-ring"
            />
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={addCriterion}
              disabled={!criterionInput.trim()}
            >
              Add
            </Button>
          </div>
        </div>

        {/* Max turns budget */}
        <div>
          <label
            htmlFor="goal-budget"
            className="mb-1.5 block text-xs font-medium text-foreground"
          >
            Max Turns Budget
          </label>
          <input
            id="goal-budget"
            type="number"
            min={0}
            value={maxTurns}
            onChange={(e) => setMaxTurns(e.target.value)}
            placeholder="0 = unlimited"
            className="w-40 rounded-md border border-input bg-background px-3 py-2 text-sm text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-1 focus:ring-ring"
          />
          <p className="mt-1 text-xs text-muted-foreground">
            Leave blank or set 0 for unlimited turns.
          </p>
        </div>
      </div>

      {/* Actions */}
      <div className="mt-5 flex items-center justify-end gap-2">
        <Button type="button" variant="ghost" size="sm" onClick={onClose}>
          Cancel
        </Button>
        <Button type="submit" size="sm" disabled={createGoal.isPending}>
          {createGoal.isPending ? (
            <>
              <Spinner size={14} className="mr-1.5" />
              Creating...
            </>
          ) : (
            <>
              <Plus className="mr-1.5 h-3.5 w-3.5" />
              Create Goal
            </>
          )}
        </Button>
      </div>

      {createGoal.isError && (
        <p className="mt-3 text-xs text-destructive">{String(createGoal.error)}</p>
      )}
    </form>
  );
}

// ─── Goal Card ────────────────────────────────────────────────────────────────

interface GoalCardProps {
  goal: Goal;
  pauseMutation: ReturnType<typeof usePauseGoal>;
  resumeMutation: ReturnType<typeof useResumeGoal>;
  cancelMutation: ReturnType<typeof useCancelGoal>;
}

function GoalCard({ goal, pauseMutation, resumeMutation, cancelMutation }: GoalCardProps) {
  const isPending =
    pauseMutation.isPending || resumeMutation.isPending || cancelMutation.isPending;

  const canPause = goal.status === 'active';
  const canResume = goal.status === 'paused';
  const canCancel =
    goal.status === 'pending' || goal.status === 'active' || goal.status === 'paused';

  return (
    <div className="bg-card border border-border rounded-xl p-4 flex flex-col gap-3 hover:border-primary/40 transition-colors">
      {/* Header row */}
      <div className="flex items-start gap-3">
        <div className="mt-1 flex-shrink-0">
          <StatusDot status={goal.status} />
        </div>
        <div className="min-w-0 flex-1">
          <p
            className="truncate text-sm font-semibold text-foreground leading-snug"
            title={goal.title}
          >
            {goal.title}
          </p>
          {goal.description && (
            <p
              className="mt-0.5 line-clamp-2 text-xs text-muted-foreground leading-relaxed"
              title={goal.description}
            >
              {goal.description}
            </p>
          )}
        </div>
        {/* Status pill */}
        <span className="inline-flex flex-shrink-0 items-center gap-1 rounded-full border border-border px-2 py-0.5 text-[10px] font-medium text-muted-foreground">
          <StatusIcon status={goal.status} />
          {STATUS_LABEL[goal.status]}
        </span>
      </div>

      {/* Success criteria chips */}
      {goal.success_criteria.length > 0 && (
        <div className="flex flex-wrap gap-1">
          {goal.success_criteria.slice(0, 3).map((c, i) => (
            <span
              key={i}
              className="inline-flex items-center rounded-full border border-border bg-secondary px-2 py-0.5 text-[10px] text-secondary-foreground"
              title={c}
            >
              <CheckCircle2 className="mr-1 h-3 w-3 flex-shrink-0 text-emerald-400" />
              <span className="max-w-[160px] truncate">{c}</span>
            </span>
          ))}
          {goal.success_criteria.length > 3 && (
            <span className="inline-flex items-center rounded-full border border-border bg-secondary px-2 py-0.5 text-[10px] text-muted-foreground">
              +{goal.success_criteria.length - 3} more
            </span>
          )}
        </div>
      )}

      {/* Footer row */}
      <div className="flex items-center justify-between gap-2 border-t border-border pt-3">
        <span className="flex items-center gap-1 text-xs text-muted-foreground">
          <Clock className="h-3 w-3 flex-shrink-0" />
          {formatRelative(goal.created_at)}
        </span>

        {/* Action buttons */}
        <div className="flex items-center gap-1.5">
          {canPause && (
            <Button
              variant="outline"
              size="sm"
              className="h-7 px-2.5 text-xs"
              disabled={isPending}
              onClick={() => pauseMutation.mutate(goal.goal_id)}
            >
              <Pause className="mr-1 h-3 w-3" />
              Pause
            </Button>
          )}
          {canResume && (
            <Button
              variant="outline"
              size="sm"
              className="h-7 px-2.5 text-xs"
              disabled={isPending}
              onClick={() => resumeMutation.mutate(goal.goal_id)}
            >
              <Play className="mr-1 h-3 w-3" />
              Resume
            </Button>
          )}
          {canCancel && (
            <Button
              variant="ghost"
              size="sm"
              className="h-7 px-2.5 text-xs text-destructive hover:bg-destructive/10 hover:text-destructive"
              disabled={isPending}
              onClick={() => cancelMutation.mutate(goal.goal_id)}
            >
              <XCircle className="mr-1 h-3 w-3" />
              Cancel
            </Button>
          )}
        </div>
      </div>
    </div>
  );
}

// ─── Main Page ────────────────────────────────────────────────────────────────

export default function GoalsPage() {
  const { data, isLoading, isError, error } = useGoals();
  const pauseMutation = usePauseGoal();
  const resumeMutation = useResumeGoal();
  const cancelMutation = useCancelGoal();

  const [showCreateForm, setShowCreateForm] = useState(false);
  const [activeTab, setActiveTab] = useState<FilterTab>('all');

  const goals = useMemo(() => data?.goals ?? [], [data]);
  const filtered = useMemo(() => filterGoals(goals, activeTab), [goals, activeTab]);

  const tabCounts = useMemo(
    () => ({
      all: goals.length,
      active: goals.filter(
        (g) => g.status === 'active' || g.status === 'pending' || g.status === 'paused'
      ).length,
      completed: goals.filter((g) => g.status === 'completed').length,
      failed: goals.filter((g) => g.status === 'failed' || g.status === 'cancelled').length,
    }),
    [goals]
  );

  return (
    <div className="space-y-6">
      {/* ── Header ──────────────────────────────────────────────────────── */}
      <div className="flex items-start justify-between gap-4">
        <div className="flex items-center gap-3">
          <Target className="h-6 w-6 text-primary flex-shrink-0" />
          <div>
            <h1 className="text-2xl font-bold tracking-tight text-gradient">
              Goals
            </h1>
            <p className="text-sm text-muted-foreground">
              Kairos autonomous goal runtime
            </p>
          </div>
        </div>
        <Button
          size="sm"
          onClick={() => setShowCreateForm((v) => !v)}
          aria-expanded={showCreateForm}
        >
          {showCreateForm ? (
            <>
              <X className="mr-1.5 h-4 w-4" />
              Close
            </>
          ) : (
            <>
              <Plus className="mr-1.5 h-4 w-4" />
              New Goal
            </>
          )}
        </Button>
      </div>

      {/* ── Inline Create Form ───────────────────────────────────────────── */}
      {showCreateForm && (
        <CreateGoalForm onClose={() => setShowCreateForm(false)} />
      )}

      {/* ── Loading ──────────────────────────────────────────────────────── */}
      {isLoading && (
        <div className="flex h-64 items-center justify-center">
          <Spinner size={28} />
        </div>
      )}

      {/* ── Error ────────────────────────────────────────────────────────── */}
      {isError && (
        <div className="flex h-48 items-center justify-center rounded-xl border border-destructive/40 bg-destructive/5">
          <div className="text-center">
            <AlertCircle className="mx-auto mb-2 h-6 w-6 text-destructive" />
            <p className="text-sm text-destructive">
              Failed to load goals: {String(error)}
            </p>
          </div>
        </div>
      )}

      {/* ── Content ───────────────────────────────────────────────────────── */}
      {!isLoading && !isError && (
        <>
          {/* Filter tabs — only shown when there are goals */}
          {goals.length > 0 && (
            <div className="flex flex-wrap gap-1" role="tablist" aria-label="Filter goals">
              {FILTER_TABS.map((tab) => (
                <button
                  key={tab.value}
                  role="tab"
                  aria-selected={activeTab === tab.value}
                  onClick={() => setActiveTab(tab.value)}
                  className={cn(
                    'inline-flex items-center gap-1.5 rounded-full px-3 py-1 text-xs font-medium transition-colors',
                    'focus:outline-none focus-visible:ring-2 focus-visible:ring-ring',
                    activeTab === tab.value
                      ? 'bg-primary text-primary-foreground'
                      : 'border border-border text-muted-foreground hover:border-primary/40 hover:text-foreground'
                  )}
                >
                  {tab.label}
                  <span
                    className={cn(
                      'rounded-full px-1.5 py-0.5 text-[10px] font-semibold',
                      activeTab === tab.value
                        ? 'bg-white/20 text-primary-foreground'
                        : 'bg-muted text-muted-foreground'
                    )}
                  >
                    {tabCounts[tab.value]}
                  </span>
                </button>
              ))}
            </div>
          )}

          {/* Empty state — no goals at all */}
          {goals.length === 0 ? (
            <EmptyState
              icon={Target}
              title="No goals yet"
              description="Run your first goal to let Kairos autonomously work towards it."
              action={
                <Button size="sm" onClick={() => setShowCreateForm(true)}>
                  <Plus className="mr-1.5 h-4 w-4" />
                  Run your first goal
                </Button>
              }
            />
          ) : filtered.length === 0 ? (
            /* Empty state — filter has no results */
            <div className="flex h-40 items-center justify-center rounded-xl border border-dashed border-border">
              <p className="text-sm text-muted-foreground">
                No goals match the current filter.
              </p>
            </div>
          ) : (
            /* Goal grid */
            <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
              {filtered.map((goal) => (
                <GoalCard
                  key={goal.goal_id}
                  goal={goal}
                  pauseMutation={pauseMutation}
                  resumeMutation={resumeMutation}
                  cancelMutation={cancelMutation}
                />
              ))}
            </div>
          )}

          {/* Count summary */}
          {goals.length > 0 && (
            <p className="text-xs text-muted-foreground">
              Showing {filtered.length} of {goals.length} goal
              {goals.length !== 1 ? 's' : ''}
            </p>
          )}
        </>
      )}
    </div>
  );
}
