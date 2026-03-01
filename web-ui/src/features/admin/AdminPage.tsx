import { Link } from 'react-router-dom';
import {
  Card,
  CardHeader,
  CardTitle,
  CardDescription,
  CardContent,
} from '@/components/ui/Card';

const tabs = [
  {
    label: 'Rate Limits',
    to: '/admin/rate-limits',
    description:
      'Configure per-key request throttling. Set requests-per-minute and requests-per-hour limits for each API key.',
  },
  {
    label: 'Capabilities',
    to: '/admin/capabilities',
    description:
      'Manage capability tiers, generate scoped tokens, and validate existing tokens against the RBAC policy.',
  },
  {
    label: 'Metrics',
    to: '/admin/metrics',
    description:
      'Live system metrics across agents, memory, templates, workflows, and webhooks. Auto-refreshes every 10 seconds.',
  },
] as const;

export default function AdminPage() {
  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-3xl font-bold tracking-tight">Administration</h1>
        <p className="mt-1 text-muted-foreground">
          System configuration, token management, and live metrics.
        </p>
      </div>

      <nav className="flex gap-2 border-b pb-3">
        {tabs.map((tab) => (
          <Link
            key={tab.to}
            to={tab.to}
            className="rounded-md px-4 py-2 text-sm font-medium text-muted-foreground transition-colors hover:bg-accent hover:text-accent-foreground"
          >
            {tab.label}
          </Link>
        ))}
      </nav>

      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
        {tabs.map((tab) => (
          <Link key={tab.to} to={tab.to} className="group">
            <Card className="h-full transition-colors group-hover:border-primary/40">
              <CardHeader>
                <CardTitle className="text-lg">{tab.label}</CardTitle>
                <CardDescription>{tab.description}</CardDescription>
              </CardHeader>
              <CardContent>
                <span className="text-sm font-medium text-primary">
                  Open &rarr;
                </span>
              </CardContent>
            </Card>
          </Link>
        ))}
      </div>
    </div>
  );
}
