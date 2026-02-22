import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useForm, type SubmitHandler } from 'react-hook-form';
import { zodResolver } from '@hookform/resolvers/zod';
import { z } from 'zod';
import { ChevronLeft, ChevronRight, Rocket, Check } from 'lucide-react';
import { useSpawnAgent } from '@/api/hooks/useAgents';
import { useSkills } from '@/api/hooks/useSkills';
import { BACKENDS } from '@/lib/constants';
import { Button } from '@/components/ui/Button';
import { Input } from '@/components/ui/Input';
import { Label } from '@/components/ui/Label';
import { Badge } from '@/components/ui/Badge';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card';
import {
  Select,
  SelectTrigger,
  SelectValue,
  SelectContent,
  SelectItem,
} from '@/components/ui/Select';
import { Spinner } from '@/components/ui/Spinner';
import { Separator } from '@/components/ui/Separator';
import { cn } from '@/lib/utils';

const spawnSchema = z.object({
  name: z.string().min(1, 'Name is required').max(100),
  backend: z.string().min(1, 'Backend is required'),
  model: z.string().optional(),
  system_prompt: z.string().optional(),
  memory_namespace: z.string().optional(),
  tools: z.array(z.string()).default([]),
  skills: z.array(z.string()).default([]),
});

type SpawnFormValues = z.infer<typeof spawnSchema>;

const STEPS = [
  { id: 1, label: 'Configure' },
  { id: 2, label: 'Tools & Skills' },
  { id: 3, label: 'Review' },
] as const;

const COMMON_TOOLS = [
  'read_file',
  'write_file',
  'execute_command',
  'web_search',
  'http_request',
  'memory_get',
  'memory_set',
  'memory_delete',
  'list_agents',
  'send_message',
];

export default function SpawnWizardPage() {
  const navigate = useNavigate();
  const spawnAgent = useSpawnAgent();
  const { data: skillsList } = useSkills();
  const [step, setStep] = useState(1);

  const {
    register,
    handleSubmit,
    watch,
    setValue,
    formState: { errors },
    trigger,
  } = useForm<SpawnFormValues>({
    resolver: zodResolver(spawnSchema),
    defaultValues: {
      name: '',
      backend: '',
      model: '',
      system_prompt: '',
      memory_namespace: '',
      tools: [],
      skills: [],
    },
  });

  const values = watch();

  const handleNext = async () => {
    if (step === 1) {
      const valid = await trigger(['name', 'backend']);
      if (!valid) return;
    }
    setStep((s) => Math.min(s + 1, 3));
  };

  const handleBack = () => {
    setStep((s) => Math.max(s - 1, 1));
  };

  const toggleTool = (tool: string) => {
    const current = values.tools;
    if (current.includes(tool)) {
      setValue(
        'tools',
        current.filter((t) => t !== tool)
      );
    } else {
      setValue('tools', [...current, tool]);
    }
  };

  const toggleSkill = (skill: string) => {
    const current = values.skills;
    if (current.includes(skill)) {
      setValue(
        'skills',
        current.filter((s) => s !== skill)
      );
    } else {
      setValue('skills', [...current, skill]);
    }
  };

  const onSubmit: SubmitHandler<SpawnFormValues> = (data) => {
    spawnAgent.mutate(
      {
        name: data.name,
        backend: data.backend,
        model: data.model || undefined,
        system_prompt: data.system_prompt || undefined,
        memory_namespace: data.memory_namespace || undefined,
        tools: data.tools.length > 0 ? data.tools : undefined,
        skills: data.skills.length > 0 ? data.skills : undefined,
      },
      {
        onSuccess: () => navigate('/agents'),
      }
    );
  };

  return (
    <div className="mx-auto max-w-2xl space-y-6">
      {/* Header */}
      <div>
        <h1 className="text-2xl font-bold tracking-tight">Spawn Agent</h1>
        <p className="text-sm text-muted-foreground">
          Configure and deploy a new agent
        </p>
      </div>

      {/* Step indicator */}
      <div className="flex items-center gap-2">
        {STEPS.map((s, i) => (
          <div key={s.id} className="flex items-center gap-2">
            <div
              className={cn(
                'flex h-8 w-8 items-center justify-center rounded-full text-xs font-semibold transition-colors',
                step >= s.id
                  ? 'bg-primary text-primary-foreground'
                  : 'bg-muted text-muted-foreground'
              )}
            >
              {step > s.id ? <Check className="h-4 w-4" /> : s.id}
            </div>
            <span
              className={cn(
                'text-sm',
                step >= s.id ? 'text-foreground' : 'text-muted-foreground'
              )}
            >
              {s.label}
            </span>
            {i < STEPS.length - 1 && (
              <div
                className={cn(
                  'mx-2 h-px w-12',
                  step > s.id ? 'bg-primary' : 'bg-border'
                )}
              />
            )}
          </div>
        ))}
      </div>

      <form onSubmit={handleSubmit(onSubmit)}>
        {/* Step 1: Configure */}
        {step === 1 && (
          <Card>
            <CardHeader>
              <CardTitle className="text-base">Configure</CardTitle>
            </CardHeader>
            <CardContent className="space-y-4">
              <div className="space-y-2">
                <Label htmlFor="name">Agent Name</Label>
                <Input
                  id="name"
                  placeholder="my-research-agent"
                  {...register('name')}
                />
                {errors.name && (
                  <p className="text-xs text-destructive">
                    {errors.name.message}
                  </p>
                )}
              </div>

              <div className="space-y-2">
                <Label htmlFor="backend">Backend</Label>
                <Select
                  value={values.backend}
                  onValueChange={(v) => setValue('backend', v)}
                >
                  <SelectTrigger>
                    <SelectValue placeholder="Select a backend" />
                  </SelectTrigger>
                  <SelectContent>
                    {BACKENDS.map((b) => (
                      <SelectItem key={b.value} value={b.value}>
                        {b.label}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
                {errors.backend && (
                  <p className="text-xs text-destructive">
                    {errors.backend.message}
                  </p>
                )}
              </div>

              <div className="space-y-2">
                <Label htmlFor="model">Model (optional)</Label>
                <Input
                  id="model"
                  placeholder="gpt-4o, claude-sonnet-4-20250514, etc."
                  {...register('model')}
                />
              </div>

              <div className="space-y-2">
                <Label htmlFor="system_prompt">System Prompt (optional)</Label>
                <textarea
                  id="system_prompt"
                  rows={4}
                  placeholder="You are a helpful research assistant..."
                  className="w-full rounded-md border border-input bg-background px-3 py-2 text-sm ring-offset-background placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                  {...register('system_prompt')}
                />
              </div>
            </CardContent>
          </Card>
        )}

        {/* Step 2: Tools & Skills */}
        {step === 2 && (
          <Card>
            <CardHeader>
              <CardTitle className="text-base">Tools & Skills</CardTitle>
            </CardHeader>
            <CardContent className="space-y-6">
              <div className="space-y-3">
                <Label>Tools</Label>
                <p className="text-xs text-muted-foreground">
                  Select the tools this agent can use
                </p>
                <div className="flex flex-wrap gap-2">
                  {COMMON_TOOLS.map((tool) => (
                    <button
                      key={tool}
                      type="button"
                      onClick={() => toggleTool(tool)}
                      className={cn(
                        'rounded-md border px-3 py-1.5 text-xs transition-colors',
                        values.tools.includes(tool)
                          ? 'border-primary bg-primary/10 text-primary'
                          : 'border-border bg-background text-muted-foreground hover:bg-muted/50'
                      )}
                    >
                      {tool}
                    </button>
                  ))}
                </div>
              </div>

              <Separator />

              <div className="space-y-3">
                <Label>Skills</Label>
                <p className="text-xs text-muted-foreground">
                  Select high-level skills to enable
                </p>
                {skillsList && skillsList.length > 0 ? (
                  <div className="flex flex-wrap gap-2">
                    {skillsList.map((skill) => (
                      <button
                        key={skill.name}
                        type="button"
                        onClick={() => toggleSkill(skill.name)}
                        className={cn(
                          'rounded-md border px-3 py-1.5 text-xs transition-colors',
                          values.skills.includes(skill.name)
                            ? 'border-primary bg-primary/10 text-primary'
                            : 'border-border bg-background text-muted-foreground hover:bg-muted/50'
                        )}
                      >
                        {skill.name}
                      </button>
                    ))}
                  </div>
                ) : (
                  <p className="text-xs text-muted-foreground">
                    No skills available
                  </p>
                )}
              </div>

              <Separator />

              <div className="space-y-2">
                <Label htmlFor="memory_namespace">
                  Memory Namespace (optional)
                </Label>
                <Input
                  id="memory_namespace"
                  placeholder="research-ns"
                  {...register('memory_namespace')}
                />
                <p className="text-xs text-muted-foreground">
                  Isolate this agent's memory to a specific namespace
                </p>
              </div>
            </CardContent>
          </Card>
        )}

        {/* Step 3: Review */}
        {step === 3 && (
          <Card>
            <CardHeader>
              <CardTitle className="text-base">Review & Spawn</CardTitle>
            </CardHeader>
            <CardContent className="space-y-4">
              <div className="space-y-3 text-sm">
                <div className="flex justify-between">
                  <span className="text-muted-foreground">Name</span>
                  <span className="font-medium">{values.name}</span>
                </div>
                <div className="flex justify-between">
                  <span className="text-muted-foreground">Backend</span>
                  <span className="font-medium">
                    {BACKENDS.find((b) => b.value === values.backend)?.label ??
                      values.backend}
                  </span>
                </div>
                {values.model && (
                  <div className="flex justify-between">
                    <span className="text-muted-foreground">Model</span>
                    <span className="font-medium">{values.model}</span>
                  </div>
                )}
                {values.system_prompt && (
                  <div>
                    <span className="text-muted-foreground">System Prompt</span>
                    <p className="mt-1 rounded border border-border bg-muted/30 p-2 text-xs">
                      {values.system_prompt}
                    </p>
                  </div>
                )}
                {values.memory_namespace && (
                  <div className="flex justify-between">
                    <span className="text-muted-foreground">
                      Memory Namespace
                    </span>
                    <Badge variant="secondary">{values.memory_namespace}</Badge>
                  </div>
                )}

                {values.tools.length > 0 && (
                  <div>
                    <span className="text-muted-foreground">Tools</span>
                    <div className="mt-1 flex flex-wrap gap-1">
                      {values.tools.map((t) => (
                        <Badge key={t} variant="outline" className="text-[10px]">
                          {t}
                        </Badge>
                      ))}
                    </div>
                  </div>
                )}

                {values.skills.length > 0 && (
                  <div>
                    <span className="text-muted-foreground">Skills</span>
                    <div className="mt-1 flex flex-wrap gap-1">
                      {values.skills.map((s) => (
                        <Badge key={s} variant="outline" className="text-[10px]">
                          {s}
                        </Badge>
                      ))}
                    </div>
                  </div>
                )}
              </div>

              {spawnAgent.error && (
                <div className="rounded-md border border-destructive/20 bg-destructive/10 px-3 py-2 text-xs text-destructive">
                  Failed to spawn: {String(spawnAgent.error)}
                </div>
              )}
            </CardContent>
          </Card>
        )}

        {/* Navigation buttons */}
        <div className="mt-6 flex items-center justify-between">
          <Button
            type="button"
            variant="outline"
            onClick={handleBack}
            disabled={step === 1}
          >
            <ChevronLeft className="mr-2 h-4 w-4" />
            Back
          </Button>

          {step < 3 ? (
            <Button type="button" onClick={handleNext}>
              Next
              <ChevronRight className="ml-2 h-4 w-4" />
            </Button>
          ) : (
            <Button type="submit" disabled={spawnAgent.isPending}>
              {spawnAgent.isPending ? (
                <Spinner className="mr-2 h-4 w-4" />
              ) : (
                <Rocket className="mr-2 h-4 w-4" />
              )}
              Spawn Agent
            </Button>
          )}
        </div>
      </form>
    </div>
  );
}
