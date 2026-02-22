import { PieChart, Pie, Cell, ResponsiveContainer } from 'recharts';

interface HealthRingProps {
  healthy: number;
  warning: number;
  critical: number;
  unknown: number;
}

const COLORS = {
  healthy: '#22c55e',
  warning: '#eab308',
  critical: '#ef4444',
  unknown: '#71717a',
};

export function HealthRing({ healthy, warning, critical, unknown }: HealthRingProps) {
  const data = [
    { name: 'Healthy', value: healthy },
    { name: 'Warning', value: warning },
    { name: 'Critical', value: critical },
    { name: 'Unknown', value: unknown },
  ].filter((d) => d.value > 0);

  const total = healthy + warning + critical + unknown;

  if (total === 0) {
    return (
      <div className="flex h-32 items-center justify-center text-sm text-muted-foreground">
        No agents
      </div>
    );
  }

  return (
    <div className="relative h-32 w-32">
      <ResponsiveContainer width="100%" height="100%">
        <PieChart>
          <Pie
            data={data}
            cx="50%"
            cy="50%"
            innerRadius={36}
            outerRadius={56}
            paddingAngle={2}
            dataKey="value"
            strokeWidth={0}
          >
            {data.map((entry) => (
              <Cell
                key={entry.name}
                fill={COLORS[entry.name.toLowerCase() as keyof typeof COLORS]}
              />
            ))}
          </Pie>
        </PieChart>
      </ResponsiveContainer>
      <div className="absolute inset-0 flex flex-col items-center justify-center">
        <span className="text-lg font-bold">{total}</span>
        <span className="text-[10px] text-muted-foreground">agents</span>
      </div>
    </div>
  );
}
