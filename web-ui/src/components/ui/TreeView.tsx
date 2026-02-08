import { cn } from '@/lib/utils';
import { ChevronRight, File, Folder } from 'lucide-react';
import { useState } from 'react';

interface TreeNode {
  id: string;
  name: string;
  type: 'file' | 'folder';
  children?: TreeNode[];
}

interface TreeViewProps {
  data: TreeNode[];
  onSelect?: (node: TreeNode) => void;
  selectedId?: string;
}

function TreeNodeComponent({
  node,
  level = 0,
  onSelect,
  selectedId
}: {
  node: TreeNode;
  level?: number;
  onSelect?: (node: TreeNode) => void;
  selectedId?: string;
}) {
  const [isExpanded, setIsExpanded] = useState(true);
  const isSelected = selectedId === node.id;
  const hasChildren = node.children && node.children.length > 0;

  return (
    <div>
      <button
        onClick={() => {
          if (hasChildren) setIsExpanded(!isExpanded);
          onSelect?.(node);
        }}
        className={cn(
          'w-full flex items-center gap-2 px-2 py-1.5 rounded-md text-sm transition-colors',
          isSelected
            ? 'bg-primary/10 text-primary'
            : 'text-muted-foreground hover:text-foreground hover:bg-accent'
        )}
        style={{ paddingLeft: `${level * 16 + 8}px` }}
      >
        {hasChildren ? (
          <ChevronRight
            className={cn('w-3.5 h-3.5 transition-transform', isExpanded && 'rotate-90')}
          />
        ) : (
          <span className="w-3.5" />
        )}

        {node.type === 'folder' ? (
          <Folder className={cn('w-4 h-4', isSelected ? 'text-primary' : 'text-yellow-500')} />
        ) : (
          <File className="w-4 h-4 text-blue-400" />
        )}

        <span className="truncate">{node.name}</span>
      </button>

      {hasChildren && isExpanded && (
        <div>
          {node.children!.map((child) => (
            <TreeNodeComponent
              key={child.id}
              node={child}
              level={level + 1}
              onSelect={onSelect}
              selectedId={selectedId}
            />
          ))}
        </div>
      )}
    </div>
  );
}

export function TreeView({ data, onSelect, selectedId }: TreeViewProps) {
  return (
    <div className="space-y-0.5">
      {data.map((node) => (
        <TreeNodeComponent
          key={node.id}
          node={node}
          onSelect={onSelect}
          selectedId={selectedId}
        />
      ))}
    </div>
  );
}
