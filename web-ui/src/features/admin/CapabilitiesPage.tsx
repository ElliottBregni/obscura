import { useState } from 'react';
import {
  useCapabilityTier,
  useGenerateToken,
  useValidateToken,
} from '@/api/hooks/useCapabilities';
import { Button } from '@/components/ui/Button';
import { Input } from '@/components/ui/Input';
import { Label } from '@/components/ui/Label';
import {
  Card,
  CardHeader,
  CardTitle,
  CardDescription,
  CardContent,
} from '@/components/ui/Card';
import { Badge } from '@/components/ui/Badge';
import { CodeBlock } from '@/components/ui/CodeBlock';
import { Spinner } from '@/components/ui/Spinner';

export default function CapabilitiesPage() {
  const { data: tier, isLoading, error } = useCapabilityTier();
  const generateToken = useGenerateToken();
  const validateToken = useValidateToken();

  // Generate form state
  const [sessionId, setSessionId] = useState('');

  // Validate form state
  const [tokenInput, setTokenInput] = useState('');

  const handleGenerate = (e: React.FormEvent) => {
    e.preventDefault();
    if (!sessionId.trim()) return;
    generateToken.mutate({ session_id: sessionId.trim() });
  };

  const handleValidate = (e: React.FormEvent) => {
    e.preventDefault();
    if (!tokenInput.trim()) return;
    try {
      const parsed = JSON.parse(tokenInput.trim());
      validateToken.mutate(parsed);
    } catch {
      // If it's not JSON, show error
    }
  };

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
        Failed to load capability tier: {(error as Error).message}
      </p>
    );
  }

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-3xl font-bold tracking-tight">Capabilities</h1>
        <p className="mt-1 text-muted-foreground">
          Manage capability tiers, generate tokens, and validate credentials.
        </p>
      </div>

      {/* Current Tier */}
      {tier && (
        <Card>
          <CardHeader>
            <CardTitle className="text-lg">Current Tier</CardTitle>
            <CardDescription>
              The active capability tier for this deployment.
            </CardDescription>
          </CardHeader>
          <CardContent className="flex items-center gap-4">
            <span className="text-2xl font-bold">{tier.tier}</span>
            <div className="flex flex-wrap gap-1.5">
              {tier.roles.map((role) => (
                <Badge key={role} variant="secondary">
                  {role}
                </Badge>
              ))}
            </div>
          </CardContent>
        </Card>
      )}

      {/* Token Generator */}
      <Card>
        <CardHeader>
          <CardTitle className="text-lg">Token Generator</CardTitle>
          <CardDescription>
            Generate a capability token for a session.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <form onSubmit={handleGenerate} className="flex flex-wrap items-end gap-4">
            <div className="flex-1 space-y-1.5">
              <Label htmlFor="cap-session">Session ID</Label>
              <Input
                id="cap-session"
                placeholder="Enter session ID..."
                value={sessionId}
                onChange={(e) => setSessionId(e.target.value)}
              />
            </div>
            <Button type="submit" disabled={generateToken.isPending}>
              {generateToken.isPending ? 'Generating...' : 'Generate'}
            </Button>
          </form>

          {generateToken.isError && (
            <p className="text-sm text-red-500">
              {(generateToken.error as Error).message}
            </p>
          )}

          {generateToken.data && (
            <div className="space-y-2">
              <p className="text-sm text-muted-foreground">
                Tier: <span className="font-mono text-foreground">{generateToken.data.tier}</span>
                {' | '}
                Expires at:{' '}
                <span className="font-mono text-foreground">
                  {new Date(generateToken.data.expires_at * 1000).toLocaleString()}
                </span>
              </p>
              <CodeBlock
                code={JSON.stringify(generateToken.data.token, null, 2)}
                language="json"
              />
            </div>
          )}
        </CardContent>
      </Card>

      {/* Token Validator */}
      <Card>
        <CardHeader>
          <CardTitle className="text-lg">Token Validator</CardTitle>
          <CardDescription>
            Paste a token object (JSON) to verify its validity.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <form onSubmit={handleValidate} className="space-y-4">
            <div className="space-y-1.5">
              <Label htmlFor="cap-validate">Token (JSON)</Label>
              <textarea
                id="cap-validate"
                placeholder='{"tier":"PUBLIC","user_id":"...","session_id":"...","issued_at":0,"expires_at":0,"nonce":"...","signature":"..."}'
                value={tokenInput}
                onChange={(e) => setTokenInput(e.target.value)}
                className="h-32 w-full rounded-md border border-input bg-background p-3 font-mono text-sm focus:outline-none focus:ring-2 focus:ring-ring"
              />
            </div>
            <Button type="submit" disabled={validateToken.isPending}>
              {validateToken.isPending ? 'Validating...' : 'Validate'}
            </Button>
          </form>

          {validateToken.isError && (
            <p className="text-sm text-red-500">
              {(validateToken.error as Error).message}
            </p>
          )}

          {validateToken.data && (
            <Card className="border-dashed">
              <CardContent className="flex items-center gap-4 p-4">
                <Badge
                  variant={
                    validateToken.data.valid ? 'default' : 'destructive'
                  }
                >
                  {validateToken.data.valid ? 'Valid' : 'Invalid'}
                </Badge>
                <span className="text-sm">
                  Tier:{' '}
                  <span className="font-semibold">
                    {validateToken.data.tier}
                  </span>
                </span>
                {validateToken.data.expired && (
                  <Badge variant="destructive">Expired</Badge>
                )}
              </CardContent>
            </Card>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
