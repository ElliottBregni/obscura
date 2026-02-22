import { useState, useCallback } from 'react';
import { useNavigate, Link } from 'react-router-dom';
import { toast } from 'sonner';
import { ArrowLeft, Plus, Trash2, GitBranch } from 'lucide-react';
import { useCreateWorkflow } from '@/api/hooks/useWorkflows';
import type { WorkflowStep } from '@/api/types';
import { Button } from '@/components/ui/Button';
import { Input } from '@/components/ui/Input';
import { Label } from '@/components/ui/Label';
import { Badge } from '@/components/ui/Badge';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card';
import { Spinner } from '@/components/ui/Spinner';

interface StepDraft {
  name: string;
  agent_config_json: string;
  depends_on: string[];
}

export default function WorkflowCreatePage() {
  const navigate = useNavigate();
  const createWorkflow = useCreateWorkflow();

  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [steps, setSteps] = useState<StepDraft[]>([]);

  const addStep = useCallback(() => {
    setSteps((prev) => [
      ...prev,
      { name: '', agent_config_json: '{}', depends_on: [] },
    ]);
  }, []);

  const removeStep = useCallback((index: number) => {
    setSteps((prev) => prev.filter((_, i) => i !== index));
  }, []);

  const updateStep = useCallback(
    (index: number, field: keyof StepDraft, value: string | string[]) => {
      setSteps((prev) =>
        prev.map((s, i) => (i === index ? { ...s, [field]: value } : s)),
      );
    },
    [],
  );

  const toggleDependency = useCallback(
    (stepIndex: number, depName: string) => {
      setSteps((prev) =>
        prev.map((s, i) => {
          if (i !== stepIndex) return s;
          const has = s.depends_on.includes(depName);
          return {
            ...s,
            depends_on: has
              ? s.depends_on.filter((d) => d !== depName)
              : [...s.depends_on, depName],
          };
        }),
      );
    },
    [],
  );

  const handleSubmit = useCallback(
    (e: React.FormEvent) => {
      e.preventDefault();
      if (!name.trim()) {
        toast.error('Workflow name is required');
        return;
      }
      if (steps.length === 0) {
        toast.error('Add at least one step');
        return;
      }

      const parsedSteps: WorkflowStep[] = [];
      for (const step of steps) {
        if (!step.name.trim()) {
          toast.error('All steps must have a name');
          return;
        }
        try {
          const config = JSON.parse(step.agent_config_json);
          parsedSteps.push({
            name: step.name.trim(),
            agent_config: config,
            depends_on: step.depends_on.length ? step.depends_on : undefined,
          });
        } catch {
          toast.error(`Invalid JSON in step "${step.name}"`);
          return;
        }
      }

      createWorkflow.mutate(
        {
          name: name.trim(),
          description: description.trim(),
          steps: parsedSteps,
        },
        {
          onSuccess: () => {
            toast.success('Workflow created');
            navigate('/workflows');
          },
          onError: (err) => toast.error(`Create failed: ${String(err)}`),
        },
      );
    },
    [name, description, steps, createWorkflow, navigate],
  );

  const stepNames = steps.map((s) => s.name).filter(Boolean);

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center gap-3">
        <Button variant="ghost" size="icon" asChild>
          <Link to="/workflows">
            <ArrowLeft className="h-4 w-4" />
          </Link>
        </Button>
        <GitBranch className="h-6 w-6 text-primary" />
        <h1 className="text-2xl font-bold tracking-tight">Create Workflow</h1>
      </div>

      <form onSubmit={handleSubmit} className="space-y-6">
        {/* Basic info */}
        <Card>
          <CardHeader>
            <CardTitle className="text-base">Basic Information</CardTitle>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="space-y-2">
              <Label htmlFor="wf-name">Name</Label>
              <Input
                id="wf-name"
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="my-workflow"
                required
              />
            </div>
            <div className="space-y-2">
              <Label htmlFor="wf-desc">Description</Label>
              <Input
                id="wf-desc"
                value={description}
                onChange={(e) => setDescription(e.target.value)}
                placeholder="Describe what this workflow does..."
              />
            </div>
          </CardContent>
        </Card>

        {/* Steps */}
        <Card>
          <CardHeader className="flex flex-row items-center justify-between space-y-0">
            <CardTitle className="text-base">
              Steps
              <Badge variant="secondary" className="ml-2">
                {steps.length}
              </Badge>
            </CardTitle>
            <Button type="button" variant="outline" size="sm" onClick={addStep}>
              <Plus className="mr-1.5 h-3.5 w-3.5" />
              Add Step
            </Button>
          </CardHeader>
          <CardContent className="space-y-4">
            {steps.length === 0 && (
              <p className="py-6 text-center text-sm text-muted-foreground">
                No steps added yet. Click &quot;Add Step&quot; to start building
                your workflow.
              </p>
            )}
            {steps.map((step, index) => (
              <Card key={index} className="bg-muted/30">
                <CardContent className="space-y-4 pt-4">
                  <div className="flex items-start justify-between">
                    <Badge variant="outline">Step {index + 1}</Badge>
                    <Button
                      type="button"
                      variant="ghost"
                      size="icon"
                      className="h-7 w-7"
                      onClick={() => removeStep(index)}
                    >
                      <Trash2 className="h-3.5 w-3.5 text-destructive" />
                    </Button>
                  </div>
                  <div className="space-y-2">
                    <Label>Step Name</Label>
                    <Input
                      value={step.name}
                      onChange={(e) =>
                        updateStep(index, 'name', e.target.value)
                      }
                      placeholder="step-name"
                    />
                  </div>
                  <div className="space-y-2">
                    <Label>Agent Config (JSON)</Label>
                    <textarea
                      className="h-24 w-full rounded-md border border-input bg-background p-3 font-mono text-sm focus:outline-none focus:ring-2 focus:ring-ring"
                      value={step.agent_config_json}
                      onChange={(e) =>
                        updateStep(index, 'agent_config_json', e.target.value)
                      }
                    />
                  </div>
                  {stepNames.length > 0 && (
                    <div className="space-y-2">
                      <Label>Depends On</Label>
                      <div className="flex flex-wrap gap-2">
                        {stepNames
                          .filter((n) => n !== step.name)
                          .map((depName) => (
                            <button
                              key={depName}
                              type="button"
                              onClick={() =>
                                toggleDependency(index, depName)
                              }
                            >
                              <Badge
                                variant={
                                  step.depends_on.includes(depName)
                                    ? 'default'
                                    : 'outline'
                                }
                                className="cursor-pointer"
                              >
                                {depName}
                              </Badge>
                            </button>
                          ))}
                      </div>
                    </div>
                  )}
                </CardContent>
              </Card>
            ))}
          </CardContent>
        </Card>

        {/* Submit */}
        <div className="flex justify-end gap-3">
          <Button type="button" variant="outline" asChild>
            <Link to="/workflows">Cancel</Link>
          </Button>
          <Button type="submit" disabled={createWorkflow.isPending}>
            {createWorkflow.isPending ? (
              <Spinner size={16} className="mr-2" />
            ) : null}
            Create Workflow
          </Button>
        </div>
      </form>
    </div>
  );
}
