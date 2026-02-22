import { cn } from '@/lib/utils';
import { STATUS_COLORS } from '@/lib/constants';
import { Badge } from './Badge';

export interface StatusBadgeProps
  extends React.HTMLAttributes<HTMLDivElement> {
  status: string;
}

const colorMap: Record<string, { bg: string; text: string; border: string }> = {
  emerald: {
    bg: 'bg-emerald-500/15',
    text: 'text-emerald-500',
    border: 'border-emerald-500/20',
  },
  blue: {
    bg: 'bg-blue-500/15',
    text: 'text-blue-500',
    border: 'border-blue-500/20',
  },
  red: {
    bg: 'bg-red-500/15',
    text: 'text-red-500',
    border: 'border-red-500/20',
  },
  zinc: {
    bg: 'bg-zinc-500/15',
    text: 'text-zinc-500',
    border: 'border-zinc-500/20',
  },
  yellow: {
    bg: 'bg-yellow-500/15',
    text: 'text-yellow-500',
    border: 'border-yellow-500/20',
  },
  amber: {
    bg: 'bg-amber-500/15',
    text: 'text-amber-500',
    border: 'border-amber-500/20',
  },
  green: {
    bg: 'bg-green-500/15',
    text: 'text-green-500',
    border: 'border-green-500/20',
  },
};

const defaultColor = colorMap.zinc;

function StatusBadge({ status, className, ...props }: StatusBadgeProps) {
  const colorKey =
    STATUS_COLORS[status as keyof typeof STATUS_COLORS] ?? 'zinc';
  const colors = colorMap[colorKey] ?? defaultColor;

  return (
    <Badge
      variant="outline"
      className={cn(
        colors.bg,
        colors.text,
        colors.border,
        'font-medium capitalize',
        className
      )}
      {...props}
    >
      <span
        className={cn(
          'mr-1.5 inline-block h-1.5 w-1.5 rounded-full',
          colors.text.replace('text-', 'bg-')
        )}
      />
      {status}
    </Badge>
  );
}

export { StatusBadge };
