import * as React from 'react';
import { Search, X } from 'lucide-react';
import { cn } from '@/lib/utils';
import { Input } from './Input';

export interface SearchInputProps
  extends Omit<React.InputHTMLAttributes<HTMLInputElement>, 'onChange'> {
  value: string;
  onChange: (value: string) => void;
  debounceMs?: number;
}

function SearchInput({
  value,
  onChange,
  debounceMs = 300,
  placeholder = 'Search...',
  className,
  ...props
}: SearchInputProps) {
  const [internalValue, setInternalValue] = React.useState(value);
  const timerRef = React.useRef<ReturnType<typeof setTimeout>>();

  React.useEffect(() => {
    setInternalValue(value);
  }, [value]);

  const handleChange = React.useCallback(
    (e: React.ChangeEvent<HTMLInputElement>) => {
      const newValue = e.target.value;
      setInternalValue(newValue);

      if (timerRef.current) {
        clearTimeout(timerRef.current);
      }

      timerRef.current = setTimeout(() => {
        onChange(newValue);
      }, debounceMs);
    },
    [onChange, debounceMs]
  );

  const handleClear = React.useCallback(() => {
    setInternalValue('');
    onChange('');
    if (timerRef.current) {
      clearTimeout(timerRef.current);
    }
  }, [onChange]);

  React.useEffect(() => {
    return () => {
      if (timerRef.current) {
        clearTimeout(timerRef.current);
      }
    };
  }, []);

  return (
    <div className={cn('relative', className)}>
      <Search className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
      <Input
        value={internalValue}
        onChange={handleChange}
        placeholder={placeholder}
        className="pl-9 pr-9"
        {...props}
      />
      {internalValue && (
        <button
          type="button"
          onClick={handleClear}
          className="absolute right-3 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-foreground"
        >
          <X className="h-4 w-4" />
          <span className="sr-only">Clear search</span>
        </button>
      )}
    </div>
  );
}

export { SearchInput };
