import { useSystemMetrics } from '@/api/hooks/useMetrics';
import { MetricCard } from '@/components/charts/MetricCard';
import { Spinner } from '@/components/ui/Spinner';
import {
  Bot,
  Database,
  LayoutTemplate,
  Workflow,
  Webhook,
} from 'lucide-react';

export default function MetricsPage() {
  const { data: metrics, isLoading, error } = useSystemMetrics();

  if (isLoading) {
    return (
      <div className="flex items-center justify-center py-24">
        <Spinner size={32} />
      </div>
    );
  }

  if (error) {
    return (
      <p className="py-12 text-center text-sm text-red-500">
        Failed to load metrics: {(error as Error).message}
      </p>
    );
  }

  if (!metrics) return null;

  const namespaceCount = Object.keys(metrics.memory.namespaces).length;
  const runningAgents = metrics.agents.by_status?.running ?? 0;
  const idleAgents = metrics.agents.by_status?.idle ?? 0;
  const errorAgents = metrics.agents.by_status?.error ?? 0;

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-3xl font-bold tracking-tight">System Metrics</h1>
        <p className="mt-1 text-muted-foreground">
          Live system metrics. Auto-refreshes every 10 seconds.
        </p>
      </div>

      {/* Agents */}
      <section className="space-y-2">
        <h2 className="text-lg font-semibold">Agents</h2>
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
          <MetricCard label="Total" value={metrics.agents.total} icon={Bot} />
          <MetricCard label="Running" value={runningAgents} icon={Bot} />
          <MetricCard label="Idle" value={idleAgents} icon={Bot} />
          <MetricCard label="Error" value={errorAgents} icon={Bot} />
        </div>
      </section>

      {/* Memory */}
      <section className="space-y-2">
        <h2 className="text-lg font-semibold">Memory</h2>
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
          <MetricCard
            label="Namespaces"
            value={namespaceCount}
            icon={Database}
          />
          <MetricCard
            label="Total Keys"
            value={metrics.memory.total_keys}
            icon={Database}
          />
        </div>
      </section>

      {/* Templates */}
      <section className="space-y-2">
        <h2 className="text-lg font-semibold">Templates</h2>
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
          <MetricCard
            label="Total"
            value={metrics.templates.total_templates}
            icon={LayoutTemplate}
          />
        </div>
      </section>

      {/* Workflows */}
      <section className="space-y-2">
        <h2 className="text-lg font-semibold">Workflows</h2>
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
          <MetricCard
            label="Total"
            value={metrics.workflows.total_workflows}
            icon={Workflow}
          />
          <MetricCard
            label="Executions"
            value={metrics.workflows.total_executions}
            icon={Workflow}
          />
        </div>
      </section>

      {/* Webhooks */}
      <section className="space-y-2">
        <h2 className="text-lg font-semibold">Webhooks</h2>
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
          <MetricCard
            label="Total"
            value={metrics.webhooks.total}
            icon={Webhook}
          />
          <MetricCard
            label="Active"
            value={metrics.webhooks.active}
            icon={Webhook}
          />
        </div>
      </section>

      <p className="text-xs text-muted-foreground">
        Last updated: {new Date(metrics.timestamp).toLocaleString()}
      </p>
    </div>
  );
}
