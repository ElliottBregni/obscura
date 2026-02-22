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
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/Select';
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
  const [scope, setScope] = useState<string>('PUBLIC');
  const [ttl, setTtl] = useState('');

  // Validate form state
  const [tokenInput, setTokenInput] = useState('');

  const handleGenerate = (e: React.FormEvent) => {
    e.preventDefault();
    generateToken.mutate({
      scope,
      ...(ttl ? { ttl: Number(ttl) } : {}),
    });
  };

  const handleValidate = (e: React.FormEvent) => {
    e.preventDefault();
    if (!tokenInput.trim()) return;
    validateToken.mutate(tokenInput.trim());
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
            Generate a scoped access token for API consumers.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <form onSubmit={handleGenerate} className="flex flex-wrap items-end gap-4">
            <div className="space-y-1.5">
              <Label>Tier</Label>
              <Select value={scope} onValueChange={setScope}>
                <SelectTrigger className="w-44">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="PUBLIC">PUBLIC</SelectItem>
                  <SelectItem value="PRIVILEGED">PRIVILEGED</SelectItem>
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="cap-ttl">TTL (seconds)</Label>
              <Input
                id="cap-ttl"
                type="number"
                min={0}
                placeholder="3600"
                value={ttl}
                onChange={(e) => setTtl(e.target.value)}
                className="w-36"
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
                Expires at:{' '}
                <span className="font-mono text-foreground">
                  {generateToken.data.expires_at}
                </span>
              </p>
              <CodeBlock code={generateToken.data.token} language="token" />
            </div>
          )}
        </CardContent>
      </Card>

      {/* Token Validator */}
      <Card>
        <CardHeader>
          <CardTitle className="text-lg">Token Validator</CardTitle>
          <CardDescription>
            Paste a token to verify its validity and inspect its claims.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <form onSubmit={handleValidate} className="flex items-end gap-4">
            <div className="flex-1 space-y-1.5">
              <Label htmlFor="cap-validate">Token</Label>
              <Input
                id="cap-validate"
                placeholder="Paste token here..."
                value={tokenInput}
                onChange={(e) => setTokenInput(e.target.value)}
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
                {validateToken.data.tier && (
                  <span className="text-sm">
                    Tier:{' '}
                    <span className="font-semibold">
                      {validateToken.data.tier}
                    </span>
                  </span>
                )}
                {validateToken.data.roles &&
                  validateToken.data.roles.length > 0 && (
                    <div className="flex flex-wrap gap-1">
                      {validateToken.data.roles.map((role) => (
                        <Badge key={role} variant="secondary">
                          {role}
                        </Badge>
                      ))}
                    </div>
                  )}
              </CardContent>
            </Card>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
