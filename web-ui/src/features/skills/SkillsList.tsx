import { useState } from 'react';
import { useSkills } from '@/api/client';
import { Card, CardContent } from '@/components/ui/Card';
import { Button } from '@/components/ui/Button';
import { Badge } from '@/components/ui/Badge';
import { Input } from '@/components/ui/Input';
import { Puzzle, Play, Search, Loader2 } from 'lucide-react';

export function SkillsList() {
  const { data: skills = [], isLoading } = useSkills();
  const [search, setSearch] = useState('');

  const filteredSkills = skills.filter(skill =>
    skill.name.toLowerCase().includes(search.toLowerCase()) ||
    skill.description.toLowerCase().includes(search.toLowerCase())
  );

  return (
    <div className="p-6 space-y-6">
      {/* Header */}
      <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold text-foreground">Skills</h1>
          <p className="text-sm text-muted-foreground mt-1">Manage and execute agent skills</p>
        </div>
      </div>

      {/* Search */}
      <div className="max-w-md">
        <div className="relative">
          <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-muted-foreground" />
          <Input
            type="text"
            placeholder="Search skills..."
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            className="pl-9"
          />
        </div>
      </div>

      {/* Skills Grid */}
      {isLoading ? (
        <div className="flex items-center justify-center py-12">
          <Loader2 className="h-5 w-5 animate-spin text-muted-foreground" />
        </div>
      ) : filteredSkills.length === 0 ? (
        <Card>
          <CardContent className="p-12 text-center">
            <Puzzle className="w-12 h-12 mx-auto mb-4 text-muted-foreground" />
            <p className="text-muted-foreground">No skills registered</p>
            <p className="text-sm text-muted-foreground mt-1">
              Skills will appear here when registered with the system
            </p>
          </CardContent>
        </Card>
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
          {filteredSkills.map((skill) => (
            <Card key={skill.name} hover>
              <CardContent className="p-5">
                <div className="flex items-start justify-between mb-4">
                  <div className="w-12 h-12 rounded-xl bg-accent flex items-center justify-center">
                    <Puzzle className="w-6 h-6 text-purple-400" />
                  </div>
                  <Badge variant="primary">v{skill.version}</Badge>
                </div>

                <h3 className="font-semibold text-foreground text-lg">{skill.name}</h3>
                <p className="text-sm text-muted-foreground mt-1">{skill.description}</p>

                <div className="mt-4 pt-4 border-t border-border">
                  <p className="text-xs text-muted-foreground mb-2">
                    {skill.capabilities.length} capabilities
                  </p>
                  <div className="flex flex-wrap gap-1">
                    {skill.capabilities.slice(0, 3).map((cap) => (
                      <Badge key={cap.name} variant="default">
                        {cap.name}
                      </Badge>
                    ))}
                    {skill.capabilities.length > 3 && (
                      <Badge variant="default">
                        +{skill.capabilities.length - 3}
                      </Badge>
                    )}
                  </div>
                </div>

                <Button
                  variant="secondary"
                  className="w-full mt-4"
                  leftIcon={<Play className="w-4 h-4" />}
                >
                  Execute
                </Button>
              </CardContent>
            </Card>
          ))}
        </div>
      )}
    </div>
  );
}
