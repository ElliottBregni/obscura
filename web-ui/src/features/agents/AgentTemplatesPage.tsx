import { useState } from 'react';
import { Plus, Pencil, Trash2 } from 'lucide-react';
import {
  useAgentTemplates,
  useCreateTemplate,
  useUpdateTemplate,
  useDeleteTemplate,
} from '@/api/hooks/useAgents';
import type { AgentTemplate } from '@/api/types';
import { BACKENDS } from '@/lib/constants';
import { Button } from '@/components/ui/Button';
import { Badge } from '@/components/ui/Badge';
import { Input } from '@/components/ui/Input';
import { Label } from '@/components/ui/Label';
import {
  Select,
  SelectTrigger,
  SelectValue,
  SelectContent,
  SelectItem,
} from '@/components/ui/Select';
import {
  Table,
  TableHeader,
  TableHead,
  TableBody,
  TableRow,
  TableCell,
} from '@/components/ui/Table';
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogFooter,
  DialogDescription,
} from '@/components/ui/Dialog';
import { Spinner } from '@/components/ui/Spinner';
import { Skeleton } from '@/components/ui/Skeleton';

interface TemplateFormState {
  name: string;
  backend: string;
  model: string;
  system_prompt: string;
  tools: string;
}

const emptyForm: TemplateFormState = {
  name: '',
  backend: '',
  model: '',
  system_prompt: '',
  tools: '',
};

export default function AgentTemplatesPage() {
  const { data: templates, isLoading, error } = useAgentTemplates();
  const createTemplate = useCreateTemplate();
  const updateTemplate = useUpdateTemplate();
  const deleteTemplate = useDeleteTemplate();

  const [dialogOpen, setDialogOpen] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [form, setForm] = useState<TemplateFormState>(emptyForm);
  const [deleteConfirmId, setDeleteConfirmId] = useState<string | null>(null);

  const openCreate = () => {
    setEditingId(null);
    setForm(emptyForm);
    setDialogOpen(true);
  };

  const openEdit = (template: AgentTemplate) => {
    setEditingId(template.template_id);
    setForm({
      name: template.name,
      backend: template.backend,
      model: template.model ?? '',
      system_prompt: template.system_prompt ?? '',
      tools: template.tools?.join(', ') ?? '',
    });
    setDialogOpen(true);
  };

  const handleSave = () => {
    const payload = {
      name: form.name,
      backend: form.backend,
      model: form.model || undefined,
      system_prompt: form.system_prompt || undefined,
      tools: form.tools
        ? form.tools
            .split(',')
            .map((t) => t.trim())
            .filter(Boolean)
        : undefined,
    };

    if (editingId) {
      updateTemplate.mutate(
        { id: editingId, ...payload },
        {
          onSuccess: () => {
            setDialogOpen(false);
            setEditingId(null);
          },
        }
      );
    } else {
      createTemplate.mutate(payload, {
        onSuccess: () => {
          setDialogOpen(false);
        },
      });
    }
  };

  const handleDelete = (id: string) => {
    deleteTemplate.mutate(id, {
      onSuccess: () => setDeleteConfirmId(null),
    });
  };

  const isSaving = createTemplate.isPending || updateTemplate.isPending;

  if (error) {
    return (
      <div className="flex h-64 items-center justify-center">
        <p className="text-sm text-destructive">
          Failed to load templates: {String(error)}
        </p>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold tracking-tight">
            Agent Templates
          </h1>
          <p className="text-sm text-muted-foreground">
            Reusable agent configurations for quick spawning
          </p>
        </div>
        <Button size="sm" onClick={openCreate}>
          <Plus className="mr-2 h-4 w-4" />
          Create Template
        </Button>
      </div>

      {/* Table */}
      {isLoading ? (
        <div className="space-y-2">
          {Array.from({ length: 4 }).map((_, i) => (
            <Skeleton key={i} className="h-12 w-full rounded" />
          ))}
        </div>
      ) : templates && templates.length > 0 ? (
        <div className="rounded-md border">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Name</TableHead>
                <TableHead>Backend</TableHead>
                <TableHead>Model</TableHead>
                <TableHead>Tools</TableHead>
                <TableHead className="w-[100px]">Actions</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {templates.map((tmpl) => (
                <TableRow key={tmpl.template_id}>
                  <TableCell className="font-medium">{tmpl.name}</TableCell>
                  <TableCell>
                    <Badge variant="secondary" className="text-[10px]">
                      {BACKENDS.find((b) => b.value === tmpl.backend)?.label ??
                        tmpl.backend}
                    </Badge>
                  </TableCell>
                  <TableCell className="text-muted-foreground">
                    {tmpl.model ?? '-'}
                  </TableCell>
                  <TableCell>
                    {tmpl.tools && tmpl.tools.length > 0 ? (
                      <div className="flex flex-wrap gap-1">
                        {tmpl.tools.slice(0, 3).map((t) => (
                          <Badge
                            key={t}
                            variant="outline"
                            className="text-[10px]"
                          >
                            {t}
                          </Badge>
                        ))}
                        {tmpl.tools.length > 3 && (
                          <Badge variant="outline" className="text-[10px]">
                            +{tmpl.tools.length - 3}
                          </Badge>
                        )}
                      </div>
                    ) : (
                      <span className="text-muted-foreground">-</span>
                    )}
                  </TableCell>
                  <TableCell>
                    <div className="flex items-center gap-1">
                      <Button
                        variant="ghost"
                        size="icon"
                        className="h-8 w-8"
                        onClick={() => openEdit(tmpl)}
                      >
                        <Pencil className="h-3.5 w-3.5" />
                      </Button>
                      <Button
                        variant="ghost"
                        size="icon"
                        className="h-8 w-8 text-destructive hover:text-destructive"
                        onClick={() => setDeleteConfirmId(tmpl.template_id)}
                      >
                        <Trash2 className="h-3.5 w-3.5" />
                      </Button>
                    </div>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      ) : (
        <div className="flex h-48 items-center justify-center rounded-lg border border-dashed border-border">
          <div className="text-center">
            <p className="text-sm text-muted-foreground">
              No templates created yet
            </p>
            <Button
              variant="link"
              size="sm"
              className="mt-2"
              onClick={openCreate}
            >
              Create your first template
            </Button>
          </div>
        </div>
      )}

      {/* Create/Edit Dialog */}
      <Dialog open={dialogOpen} onOpenChange={setDialogOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>
              {editingId ? 'Edit Template' : 'Create Template'}
            </DialogTitle>
            <DialogDescription>
              {editingId
                ? 'Update the template configuration.'
                : 'Define a reusable agent configuration.'}
            </DialogDescription>
          </DialogHeader>

          <div className="space-y-4">
            <div className="space-y-2">
              <Label htmlFor="tmpl-name">Name</Label>
              <Input
                id="tmpl-name"
                value={form.name}
                onChange={(e) => setForm({ ...form, name: e.target.value })}
                placeholder="research-template"
              />
            </div>

            <div className="space-y-2">
              <Label htmlFor="tmpl-backend">Backend</Label>
              <Select
                value={form.backend}
                onValueChange={(v) => setForm({ ...form, backend: v })}
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
            </div>

            <div className="space-y-2">
              <Label htmlFor="tmpl-model">Model (optional)</Label>
              <Input
                id="tmpl-model"
                value={form.model}
                onChange={(e) => setForm({ ...form, model: e.target.value })}
                placeholder="gpt-4o"
              />
            </div>

            <div className="space-y-2">
              <Label htmlFor="tmpl-prompt">System Prompt (optional)</Label>
              <textarea
                id="tmpl-prompt"
                rows={3}
                value={form.system_prompt}
                onChange={(e) =>
                  setForm({ ...form, system_prompt: e.target.value })
                }
                className="w-full rounded-md border border-input bg-background px-3 py-2 text-sm ring-offset-background placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                placeholder="You are a helpful assistant..."
              />
            </div>

            <div className="space-y-2">
              <Label htmlFor="tmpl-tools">Tools (comma-separated)</Label>
              <Input
                id="tmpl-tools"
                value={form.tools}
                onChange={(e) => setForm({ ...form, tools: e.target.value })}
                placeholder="read_file, write_file, web_search"
              />
            </div>
          </div>

          {(createTemplate.error || updateTemplate.error) && (
            <div className="rounded-md border border-destructive/20 bg-destructive/10 px-3 py-2 text-xs text-destructive">
              {String(createTemplate.error || updateTemplate.error)}
            </div>
          )}

          <DialogFooter>
            <Button variant="outline" onClick={() => setDialogOpen(false)}>
              Cancel
            </Button>
            <Button
              onClick={handleSave}
              disabled={!form.name || !form.backend || isSaving}
            >
              {isSaving && <Spinner className="mr-2 h-4 w-4" />}
              {editingId ? 'Update' : 'Create'}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Delete Confirmation Dialog */}
      <Dialog
        open={!!deleteConfirmId}
        onOpenChange={() => setDeleteConfirmId(null)}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Delete Template</DialogTitle>
            <DialogDescription>
              Are you sure you want to delete this template? This action cannot
              be undone.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => setDeleteConfirmId(null)}
            >
              Cancel
            </Button>
            <Button
              variant="destructive"
              onClick={() => deleteConfirmId && handleDelete(deleteConfirmId)}
              disabled={deleteTemplate.isPending}
            >
              {deleteTemplate.isPending && (
                <Spinner className="mr-2 h-4 w-4" />
              )}
              Delete
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
