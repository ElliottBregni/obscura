import { useState, useCallback } from 'react';
import { useNavigate, Link } from 'react-router-dom';
import { toast } from 'sonner';
import { ArrowLeft, Webhook } from 'lucide-react';
import { useCreateWebhook } from '@/api/hooks/useWebhooks';
import { Button } from '@/components/ui/Button';
import { Input } from '@/components/ui/Input';
import { Label } from '@/components/ui/Label';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card';
import { Spinner } from '@/components/ui/Spinner';

const AVAILABLE_EVENTS = [
  'agent.spawned',
  'agent.stopped',
  'agent.error',
  'workflow.completed',
  'workflow.failed',
  'tool.approved',
  'tool.denied',
] as const;

export default function WebhookCreatePage() {
  const navigate = useNavigate();
  const createWebhook = useCreateWebhook();

  const [url, setUrl] = useState('');
  const [events, setEvents] = useState<string[]>([]);
  const [active, setActive] = useState(true);

  const toggleEvent = useCallback((event: string) => {
    setEvents((prev) =>
      prev.includes(event)
        ? prev.filter((e) => e !== event)
        : [...prev, event],
    );
  }, []);

  const handleSubmit = useCallback(
    (e: React.FormEvent) => {
      e.preventDefault();
      if (!url.trim()) {
        toast.error('URL is required');
        return;
      }
      if (events.length === 0) {
        toast.error('Select at least one event');
        return;
      }
      createWebhook.mutate(
        { url: url.trim(), events },
        {
          onSuccess: () => {
            toast.success('Webhook created');
            navigate('/webhooks');
          },
          onError: (err) => toast.error(`Create failed: ${String(err)}`),
        },
      );
    },
    [url, events, active, createWebhook, navigate],
  );

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center gap-3">
        <Button variant="ghost" size="icon" asChild>
          <Link to="/webhooks">
            <ArrowLeft className="h-4 w-4" />
          </Link>
        </Button>
        <Webhook className="h-6 w-6 text-primary" />
        <h1 className="text-2xl font-bold tracking-tight">Create Webhook</h1>
      </div>

      <form onSubmit={handleSubmit} className="space-y-6">
        {/* URL */}
        <Card>
          <CardHeader>
            <CardTitle className="text-base">Endpoint</CardTitle>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="space-y-2">
              <Label htmlFor="wh-url">URL</Label>
              <Input
                id="wh-url"
                type="url"
                value={url}
                onChange={(e) => setUrl(e.target.value)}
                placeholder="https://example.com/webhook"
                required
              />
            </div>
            <div className="flex items-center gap-3">
              <Label htmlFor="wh-active" className="cursor-pointer">
                Active
              </Label>
              <button
                type="button"
                id="wh-active"
                role="switch"
                aria-checked={active}
                onClick={() => setActive(!active)}
                className={`relative inline-flex h-6 w-11 items-center rounded-full transition-colors ${
                  active ? 'bg-primary' : 'bg-muted'
                }`}
              >
                <span
                  className={`inline-block h-4 w-4 transform rounded-full bg-white transition-transform ${
                    active ? 'translate-x-6' : 'translate-x-1'
                  }`}
                />
              </button>
            </div>
          </CardContent>
        </Card>

        {/* Events */}
        <Card>
          <CardHeader>
            <CardTitle className="text-base">Events</CardTitle>
          </CardHeader>
          <CardContent>
            <div className="space-y-3">
              {AVAILABLE_EVENTS.map((event) => (
                <label
                  key={event}
                  className="flex cursor-pointer items-center gap-3"
                >
                  <input
                    type="checkbox"
                    checked={events.includes(event)}
                    onChange={() => toggleEvent(event)}
                    className="h-4 w-4 rounded border-input accent-primary"
                  />
                  <span className="text-sm">{event}</span>
                </label>
              ))}
            </div>
          </CardContent>
        </Card>

        {/* Submit */}
        <div className="flex justify-end gap-3">
          <Button type="button" variant="outline" asChild>
            <Link to="/webhooks">Cancel</Link>
          </Button>
          <Button type="submit" disabled={createWebhook.isPending}>
            {createWebhook.isPending ? (
              <Spinner size={16} className="mr-2" />
            ) : null}
            Create Webhook
          </Button>
        </div>
      </form>
    </div>
  );
}
