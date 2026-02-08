import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useSkills, useSpawnAgent, useMemoryNamespaces } from '@/api/client';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card';
import { Button } from '@/components/ui/Button';
import { Input } from '@/components/ui/Input';
import { Badge } from '@/components/ui/Badge';
import { Bot, ArrowLeft, Plus, Loader2 } from 'lucide-react';
import { toast } from 'sonner';
import { cn } from '@/lib/utils';

// Backend options - these map to the backend's Backend enum
const BACKENDS = [
  { id: 'claude', name: 'Claude', provider: 'anthropic' },
  { id: 'copilot', name: 'GitHub Copilot', provider: 'github' },
];

export function SpawnWizard() {
  const navigate = useNavigate();
  const { data: skills = [], isLoading: skillsLoading } = useSkills();
  const { data: namespaces = [] } = useMemoryNamespaces();
  const spawnAgent = useSpawnAgent();
  const [step, setStep] = useState(1);
  const [formData, setFormData] = useState({
    name: '',
    backend: BACKENDS[0].id,
    model: '',
    systemPrompt: '',
    selectedSkills: [] as string[],
    memoryNamespace: 'default',
  });

  const handleSpawn = async () => {
    try {
      await spawnAgent.mutateAsync({
        name: formData.name,
        backend: formData.backend,
        model: formData.model || undefined,
        system_prompt: formData.systemPrompt || undefined,
        memory_namespace: formData.memoryNamespace,
        skills: formData.selectedSkills.length > 0 ? formData.selectedSkills : undefined,
      });
      toast.success(`Agent "${formData.name}" spawned`);
      navigate('/agents');
    } catch (e: any) {
      toast.error(e.message || 'Failed to spawn agent');
    }
  };

  const toggleSkill = (skillName: string) => {
    setFormData(prev => ({
      ...prev,
      selectedSkills: prev.selectedSkills.includes(skillName)
        ? prev.selectedSkills.filter(id => id !== skillName)
        : [...prev.selectedSkills, skillName]
    }));
  };

  return (
    <div className="p-6 max-w-4xl mx-auto space-y-6">
      {/* Header */}
      <div className="flex items-center gap-4">
        <Button variant="ghost" size="sm" onClick={() => navigate('/agents')}>
          <ArrowLeft className="w-4 h-4 mr-2" /> Back
        </Button>
        <div>
          <h1 className="text-2xl font-semibold text-foreground">Spawn New Agent</h1>
          <p className="text-sm text-muted-foreground">Configure and deploy a new AI agent</p>
        </div>
      </div>

      {/* Progress */}
      <div className="flex items-center gap-2">
        {[1, 2, 3].map((s) => (
          <div
            key={s}
            className={cn(
              'h-1.5 flex-1 rounded-full transition-colors',
              s <= step ? 'bg-primary' : 'bg-muted'
            )}
          />
        ))}
      </div>

      {/* Step Content */}
      <Card>
        <CardHeader>
          <CardTitle>
            {step === 1 && 'Basic Configuration'}
            {step === 2 && 'Select Skills'}
            {step === 3 && 'Review & Spawn'}
          </CardTitle>
        </CardHeader>
        <CardContent>
          {step === 1 && (
            <div className="space-y-4">
              <Input
                label="Agent Name"
                placeholder="e.g., code-reviewer"
                value={formData.name}
                onChange={(e) => setFormData({ ...formData, name: e.target.value })}
              />

              <div className="space-y-1.5">
                <label className="text-sm font-medium text-muted-foreground">Backend</label>
                <div className="grid grid-cols-2 gap-3">
                  {BACKENDS.map((backend) => (
                    <button
                      key={backend.id}
                      onClick={() => setFormData({ ...formData, backend: backend.id, model: '' })}
                      className={cn(
                        'p-4 rounded-lg border text-left transition-colors',
                        formData.backend === backend.id
                          ? 'border-primary bg-primary/10'
                          : 'border-border hover:border-primary/30'
                      )}
                    >
                      <p className="font-medium text-foreground">{backend.name}</p>
                      <p className="text-sm text-muted-foreground capitalize">{backend.provider}</p>
                    </button>
                  ))}
                </div>
              </div>

              <Input
                label="Model (Optional)"
                placeholder="Leave blank for default, or e.g. claude-sonnet-4-5-20250929"
                value={formData.model}
                onChange={(e) => setFormData({ ...formData, model: e.target.value })}
                helper="Leave empty to use the backend's default model"
              />

              <div className="space-y-1.5">
                <label className="text-sm font-medium text-muted-foreground">Memory Namespace</label>
                <Input
                  placeholder="default"
                  value={formData.memoryNamespace}
                  onChange={(e) => setFormData({ ...formData, memoryNamespace: e.target.value })}
                />
                {namespaces.length > 0 && (
                  <div className="flex flex-wrap gap-1 mt-2">
                    {namespaces.map(ns => (
                      <button
                        key={ns}
                        onClick={() => setFormData({ ...formData, memoryNamespace: ns })}
                        className={cn(
                          'text-xs px-2 py-1 rounded transition-colors',
                          formData.memoryNamespace === ns
                            ? 'bg-primary/20 text-primary'
                            : 'bg-muted text-muted-foreground hover:text-foreground'
                        )}
                      >
                        {ns}
                      </button>
                    ))}
                  </div>
                )}
              </div>

              <div className="space-y-1.5">
                <label className="text-sm font-medium text-muted-foreground">System Prompt (Optional)</label>
                <textarea
                  rows={4}
                  placeholder="You are a helpful AI assistant..."
                  value={formData.systemPrompt}
                  onChange={(e) => setFormData({ ...formData, systemPrompt: e.target.value })}
                  className="flex w-full rounded-md border border-input bg-transparent px-3 py-2 text-sm shadow-sm transition-colors placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
                />
              </div>
            </div>
          )}

          {step === 2 && (
            <div className="space-y-4">
              <p className="text-sm text-muted-foreground">Select capabilities for this agent:</p>
              {skillsLoading ? (
                <div className="flex items-center justify-center py-8">
                  <Loader2 className="h-5 w-5 animate-spin text-muted-foreground" />
                </div>
              ) : skills.length === 0 ? (
                <div className="text-center py-8">
                  <p className="text-sm text-muted-foreground">No skills registered yet.</p>
                  <p className="text-xs text-muted-foreground mt-1">Skills will be available once registered with the system.</p>
                </div>
              ) : (
                <div className="grid grid-cols-2 gap-3">
                  {skills.map((skill) => (
                    <button
                      key={skill.name}
                      onClick={() => toggleSkill(skill.name)}
                      className={cn(
                        'p-4 rounded-lg border text-left transition-colors flex items-start gap-3',
                        formData.selectedSkills.includes(skill.name)
                          ? 'border-primary bg-primary/10'
                          : 'border-border hover:border-primary/30'
                      )}
                    >
                      <div>
                        <p className="font-medium text-foreground">{skill.name}</p>
                        <p className="text-xs text-muted-foreground mt-1">{skill.description}</p>
                        {skill.capabilities.length > 0 && (
                          <p className="text-xs text-muted-foreground/70 mt-1">
                            {skill.capabilities.length} capabilities
                          </p>
                        )}
                      </div>
                    </button>
                  ))}
                </div>
              )}
            </div>
          )}

          {step === 3 && (
            <div className="space-y-6">
              <div className="flex items-center gap-4 p-4 rounded-lg bg-muted">
                <div className="w-10 h-10 rounded-lg bg-card border flex items-center justify-center">
                  <Bot className="w-5 h-5 text-muted-foreground" />
                </div>
                <div>
                  <p className="font-medium text-foreground">{formData.name || 'Unnamed Agent'}</p>
                  <p className="text-sm text-muted-foreground">
                    {BACKENDS.find(b => b.id === formData.backend)?.name}
                    {formData.model ? ` / ${formData.model}` : ' (default model)'}
                  </p>
                </div>
              </div>

              <div className="grid grid-cols-2 gap-4">
                <div>
                  <p className="text-sm text-muted-foreground">Backend</p>
                  <p className="text-sm text-foreground mt-0.5">{BACKENDS.find(b => b.id === formData.backend)?.name}</p>
                </div>
                <div>
                  <p className="text-sm text-muted-foreground">Memory Namespace</p>
                  <p className="text-sm text-foreground mt-0.5">{formData.memoryNamespace}</p>
                </div>
                {formData.model && (
                  <div>
                    <p className="text-sm text-muted-foreground">Model</p>
                    <p className="text-sm text-foreground mt-0.5">{formData.model}</p>
                  </div>
                )}
              </div>

              {formData.selectedSkills.length > 0 && (
                <div>
                  <p className="text-sm text-muted-foreground mb-2">Skills</p>
                  <div className="flex flex-wrap gap-2">
                    {formData.selectedSkills.map((skillName) => (
                      <Badge key={skillName}>{skillName}</Badge>
                    ))}
                  </div>
                </div>
              )}

              {formData.systemPrompt && (
                <div>
                  <p className="text-sm text-muted-foreground mb-2">System Prompt</p>
                  <pre className="text-sm text-foreground bg-muted p-3 rounded-lg whitespace-pre-wrap">
                    {formData.systemPrompt}
                  </pre>
                </div>
              )}
            </div>
          )}

          {/* Navigation */}
          <div className="flex items-center justify-between mt-8 pt-6 border-t border-border">
            <Button
              variant="secondary"
              onClick={() => setStep(Math.max(1, step - 1))}
              disabled={step === 1}
            >
              Previous
            </Button>

            {step < 3 ? (
              <Button
                onClick={() => setStep(step + 1)}
                disabled={step === 1 && !formData.name}
              >
                Next
              </Button>
            ) : (
              <Button
                onClick={handleSpawn}
                isLoading={spawnAgent.isPending}
                disabled={!formData.name}
                leftIcon={<Plus className="w-4 h-4" />}
              >
                Spawn Agent
              </Button>
            )}
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
