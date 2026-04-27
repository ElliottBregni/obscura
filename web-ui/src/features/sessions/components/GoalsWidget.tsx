import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { Target, Pause, Play, XCircle, ChevronDown, ChevronUp } from 'lucide-react';
import { useState } from 'react';
import { fetchApi } from '@/api/client';

type GoalStatus = 'pending' | 'active' | 'paused' | 'completed' | 'failed' | 'cancelled';

interface Goal {
  goal_id: string;
  title: string;
  status: GoalStatus;
  priority?: string;
  progress?: number;
}

const STATUS_DOT: Record<GoalStatus, string> = {
  active:    'bg-emerald-500 animate-pulse',
  pending:   'bg-muted-foreground/40',
  paused:    'bg-yellow-500',
  completed: 'bg-emerald-500/50',
  failed:    'bg-red-500',
  cancelled: 'bg-muted-foreground/20',
};

function useGoals() {
  return useQuery({
    queryKey: ['goals', 'widget'],
    queryFn: async () => {
      const res = await fetchApi<{ goals: Goal[] }>('/api/v1/goals?limit=20');
      return res.goals.filter((g) =>
        g.status === 'active' || g.status === 'pending' || g.status === 'paused'
      );
    },
    refetchInterval: 8000,
  });
}

function useGoalAction(action: 'pause' | 'resume' | 'cancel') {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (goalId: string) =>
      fetchApi(`/api/v1/goals/${goalId}/${action}`, { method: 'POST' }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['goals'] }),
  });
}

export function GoalsWidget() {
  const { data: goals = [] } = useGoals();
  const pause = useGoalAction('pause');
  const resume = useGoalAction('resume');
  const cancel = useGoalAction('cancel');
  const [expanded, setExpanded] = useState(false);

  if (goals.length === 0) return null;

  const shown = expanded ? goals : goals.slice(0, 2);

  return (
    <div className="border-t border-border px-2 pt-2 pb-1">
      {/* Header */}
      <div className="flex items-center gap-1.5 mb-1">
        <Target className="h-3 w-3 text-muted-foreground" />
        <span className="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground flex-1">
          Goals
        </span>
        <span className="text-[10px] text-muted-foreground">{goals.length}</span>
        {goals.length > 2 && (
          <button
            onClick={() => setExpanded(!expanded)}
            className="text-muted-foreground hover:text-foreground transition-colors"
          >
            {expanded ? <ChevronUp className="h-3 w-3" /> : <ChevronDown className="h-3 w-3" />}
          </button>
        )}
      </div>

      {/* Goal rows */}
      <div className="space-y-0.5">
        {shown.map((goal) => (
          <div
            key={goal.goal_id}
            className="group flex items-center gap-1.5 rounded px-1 py-1 hover:bg-muted transition-colors"
          >
            <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${STATUS_DOT[goal.status]}`} />
            <span className="flex-1 truncate text-[11px] text-foreground" title={goal.title}>
              {goal.title}
            </span>

            {/* Actions — show on hover */}
            <div className="flex items-center gap-0.5 opacity-0 group-hover:opacity-100 transition-opacity">
              {goal.status === 'active' && (
                <button
                  onClick={() => pause.mutate(goal.goal_id)}
                  className="rounded p-0.5 hover:bg-yellow-500/20 text-muted-foreground hover:text-yellow-500 transition-colors"
                  title="Pause"
                >
                  <Pause className="h-2.5 w-2.5" />
                </button>
              )}
              {goal.status === 'paused' && (
                <button
                  onClick={() => resume.mutate(goal.goal_id)}
                  className="rounded p-0.5 hover:bg-emerald-500/20 text-muted-foreground hover:text-emerald-500 transition-colors"
                  title="Resume"
                >
                  <Play className="h-2.5 w-2.5" />
                </button>
              )}
              <button
                onClick={() => cancel.mutate(goal.goal_id)}
                className="rounded p-0.5 hover:bg-red-500/20 text-muted-foreground hover:text-red-500 transition-colors"
                title="Cancel"
              >
                <XCircle className="h-2.5 w-2.5" />
              </button>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
