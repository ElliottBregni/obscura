import { type ClassValue, clsx } from 'clsx';
import { twMerge } from 'tailwind-merge';
import { formatDistanceToNow, format } from 'date-fns';

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

function toDate(date: string | number | Date): Date {
  if (date instanceof Date) return date;
  // Handle Unix timestamps (seconds since epoch)
  const num = typeof date === 'number' ? date : Number(date);
  if (!isNaN(num) && num > 1e9 && num < 1e12) return new Date(num * 1000);
  if (!isNaN(num) && num >= 1e12) return new Date(num);
  return new Date(date);
}

export function formatDate(date: string | number | Date): string {
  const d = toDate(date);
  if (isNaN(d.getTime())) return String(date);
  return format(d, 'MMM d, yyyy HH:mm');
}

export function formatRelative(date: string | number | Date): string {
  const d = toDate(date);
  if (isNaN(d.getTime())) return String(date);
  return formatDistanceToNow(d, { addSuffix: true });
}

export function formatBytes(bytes: number): string {
  if (bytes === 0) return '0 B';
  const k = 1024;
  const sizes = ['B', 'KB', 'MB', 'GB'];
  const i = Math.floor(Math.log(bytes) / Math.log(k));
  return `${parseFloat((bytes / Math.pow(k, i)).toFixed(1))} ${sizes[i]}`;
}

export function formatDuration(ms: number): string {
  if (ms < 1000) return `${ms}ms`;
  if (ms < 60000) return `${(ms / 1000).toFixed(1)}s`;
  return `${Math.floor(ms / 60000)}m ${Math.floor((ms % 60000) / 1000)}s`;
}

export function truncate(str: string, length: number): string {
  if (str.length <= length) return str;
  return str.slice(0, length) + '...';
}
