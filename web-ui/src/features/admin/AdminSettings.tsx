import { useHealth, useMetrics, useRateLimits, useWebhooks } from '@/api/client';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card';
import { Badge } from '@/components/ui/Badge';
import { Separator } from '@/components/ui/Separator';
import { Server, Shield, Bell, Database, Loader2 } from 'lucide-react';

export function AdminSettings() {
  const { data: healthData, isLoading: healthLoading } = useHealth();
  const { data: metrics } = useMetrics();
  const { data: rateLimits } = useRateLimits();
  const { data: webhooks = [] } = useWebhooks();

  const isLoading = healthLoading;

  return (
    <div className="p-6 space-y-6">
      {/* Header */}
      <div>
        <h1 className="text-2xl font-semibold text-foreground">Settings</h1>
        <p className="text-sm text-muted-foreground mt-1">System configuration and status</p>
      </div>

      {isLoading ? (
        <div className="flex items-center justify-center py-12">
          <Loader2 className="h-5 w-5 animate-spin text-muted-foreground" />
        </div>
      ) : (
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
          <Card>
            <CardHeader>
              <CardTitle className="flex items-center gap-2">
                <Server className="w-5 h-5 text-blue-400" />
                Server Configuration
              </CardTitle>
            </CardHeader>
            <CardContent className="space-y-0">
              <div className="flex items-center justify-between py-3">
                <span className="text-muted-foreground">API Status</span>
                <Badge variant={healthData ? 'success' : 'danger'}>
                  {healthData ? 'Operational' : 'Unavailable'}
                </Badge>
              </div>
              <Separator />
              <div className="flex items-center justify-between py-3">
                <span className="text-muted-foreground">Active Agents</span>
                <Badge>{metrics?.agents?.total ?? 0}</Badge>
              </div>
              <Separator />
              <div className="flex items-center justify-between py-3">
                <span className="text-muted-foreground">Running Agents</span>
                <Badge variant="success">{metrics?.agents?.running ?? 0}</Badge>
              </div>
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle className="flex items-center gap-2">
                <Database className="w-5 h-5 text-primary" />
                Storage
              </CardTitle>
            </CardHeader>
            <CardContent className="space-y-0">
              <div className="flex items-center justify-between py-3">
                <span className="text-muted-foreground">Memory Namespaces</span>
                <Badge>{metrics?.memory?.namespaces ?? 0}</Badge>
              </div>
              <Separator />
              <div className="flex items-center justify-between py-3">
                <span className="text-muted-foreground">Total Keys</span>
                <Badge>{metrics?.memory?.total_keys ?? 0}</Badge>
              </div>
              <Separator />
              <div className="flex items-center justify-between py-3">
                <span className="text-muted-foreground">Templates</span>
                <Badge>{metrics?.templates?.total ?? 0}</Badge>
              </div>
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle className="flex items-center gap-2">
                <Shield className="w-5 h-5 text-emerald-400" />
                Security
              </CardTitle>
            </CardHeader>
            <CardContent className="space-y-0">
              <div className="flex items-center justify-between py-3">
                <span className="text-muted-foreground">Authentication</span>
                <Badge variant={healthData?.auth_enabled !== false ? 'success' : 'warning'}>
                  {healthData?.auth_enabled !== false ? 'Enabled' : 'Disabled'}
                </Badge>
              </div>
              <Separator />
              <div className="flex items-center justify-between py-3">
                <span className="text-muted-foreground">Rate Limiting</span>
                <Badge variant={rateLimits ? 'success' : 'default'}>
                  {rateLimits ? 'Configured' : 'Default'}
                </Badge>
              </div>
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle className="flex items-center gap-2">
                <Bell className="w-5 h-5 text-yellow-400" />
                Webhooks
              </CardTitle>
            </CardHeader>
            <CardContent className="space-y-0">
              <div className="flex items-center justify-between py-3">
                <span className="text-muted-foreground">Total Webhooks</span>
                <Badge>{metrics?.webhooks?.total ?? webhooks.length}</Badge>
              </div>
              <Separator />
              <div className="flex items-center justify-between py-3">
                <span className="text-muted-foreground">Active Webhooks</span>
                <Badge variant={
                  (metrics?.webhooks?.active ?? webhooks.filter(w => w.active).length) > 0
                    ? 'success' : 'default'
                }>
                  {metrics?.webhooks?.active ?? webhooks.filter(w => w.active).length}
                </Badge>
              </div>
              <Separator />
              <div className="flex items-center justify-between py-3">
                <span className="text-muted-foreground">Workflows</span>
                <Badge>{metrics?.workflows?.total ?? 0}</Badge>
              </div>
            </CardContent>
          </Card>
        </div>
      )}
    </div>
  );
}
