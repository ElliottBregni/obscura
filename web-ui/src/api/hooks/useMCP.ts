import { useQuery } from '@tanstack/react-query';
import { fetchApi } from '@/api/client';

interface MCPTool {
  name: string;
  description?: string;
  inputSchema?: Record<string, unknown>;
}

interface MCPResource {
  uri: string;
  name: string;
  description?: string;
  mimeType?: string;
}

interface MCPPrompt {
  name: string;
  description?: string;
  arguments?: { name: string; description?: string; required?: boolean }[];
}

interface JSONRPCResponse<T> {
  jsonrpc: '2.0';
  id: number;
  result: T;
}

function rpcBody(method: string) {
  return {
    method: 'POST',
    body: JSON.stringify({
      jsonrpc: '2.0',
      id: 1,
      method,
      params: {},
    }),
  };
}

export function useMCPTools() {
  return useQuery({
    queryKey: ['mcp', 'tools'],
    queryFn: async () => {
      const res = await fetchApi<JSONRPCResponse<{ tools: MCPTool[] }>>(
        '/mcp/rpc',
        rpcBody('tools/list')
      );
      return res.result.tools;
    },
  });
}

export function useMCPResources() {
  return useQuery({
    queryKey: ['mcp', 'resources'],
    queryFn: async () => {
      const res = await fetchApi<
        JSONRPCResponse<{ resources: MCPResource[] }>
      >('/mcp/rpc', rpcBody('resources/list'));
      return res.result.resources;
    },
  });
}

export function useMCPPrompts() {
  return useQuery({
    queryKey: ['mcp', 'prompts'],
    queryFn: async () => {
      const res = await fetchApi<JSONRPCResponse<{ prompts: MCPPrompt[] }>>(
        '/mcp/rpc',
        rpcBody('prompts/list')
      );
      return res.result.prompts;
    },
  });
}
