import { useState, useRef, useCallback, type KeyboardEvent } from 'react';
import { Send, Square } from 'lucide-react';
import { Button } from '@/components/ui/Button';

interface Props {
  onSend: (text: string) => void;
  onStop: () => void;
  isStreaming: boolean;
  disabled?: boolean;
}

export function ChatComposer({ onSend, onStop, isStreaming, disabled }: Props) {
  const [value, setValue] = useState('');
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  const submit = useCallback(() => {
    const trimmed = value.trim();
    if (!trimmed || isStreaming || disabled) return;
    onSend(trimmed);
    setValue('');
    // Reset textarea height
    if (textareaRef.current) {
      textareaRef.current.style.height = 'auto';
    }
  }, [value, isStreaming, disabled, onSend]);

  const onKeyDown = useCallback(
    (e: KeyboardEvent<HTMLTextAreaElement>) => {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        submit();
      }
    },
    [submit],
  );

  const onInput = useCallback(() => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = 'auto';
    el.style.height = Math.min(el.scrollHeight, 200) + 'px';
  }, []);

  return (
    <div className="border-t border-border bg-background p-3">
      <div className="flex items-end gap-2 rounded-lg border border-border bg-muted/30 px-3 py-2 focus-within:border-primary/50 transition-colors">
        <textarea
          ref={textareaRef}
          rows={1}
          value={value}
          onChange={(e) => setValue(e.target.value)}
          onKeyDown={onKeyDown}
          onInput={onInput}
          placeholder="Send a message… (Enter to send, Shift+Enter for newline)"
          disabled={disabled}
          className="flex-1 resize-none bg-transparent text-sm text-foreground placeholder:text-muted-foreground outline-none disabled:opacity-50 min-h-[24px] max-h-[200px]"
        />
        <div className="flex shrink-0 items-center gap-1 pb-0.5">
          {isStreaming ? (
            <Button
              size="icon"
              variant="ghost"
              className="h-7 w-7 text-destructive hover:text-destructive"
              onClick={onStop}
              title="Stop generation"
            >
              <Square className="h-3.5 w-3.5 fill-current" />
            </Button>
          ) : (
            <Button
              size="icon"
              variant="ghost"
              className="h-7 w-7 text-primary disabled:opacity-40"
              onClick={submit}
              disabled={!value.trim() || disabled}
              title="Send (Enter)"
            >
              <Send className="h-3.5 w-3.5" />
            </Button>
          )}
        </div>
      </div>
      <p className="mt-1.5 text-center text-[10px] text-muted-foreground/50">
        Obscura may make mistakes. Review tool outputs carefully.
      </p>
    </div>
  );
}
