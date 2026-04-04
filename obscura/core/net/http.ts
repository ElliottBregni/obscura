import { getTokenMaybe, TokenProvider } from './auth.js'
import { retry } from './retry.js'

export type HttpOptions = RequestInit & { tokenProvider?: TokenProvider }

export interface HttpClient {
  get<T = unknown>(url: string, opts?: HttpOptions): Promise<T>
  post<T = unknown>(url: string, body?: unknown, opts?: HttpOptions): Promise<T>
}

export function makeDefaultHttpClient(): HttpClient {
  async function request<T = unknown>(method: string, url: string, body?: unknown, opts: HttpOptions = {}): Promise<T> {
    const token = await getTokenMaybe(opts.tokenProvider)
    const headers: Record<string, string> = {
      'Content-Type': 'application/json',
      ...((opts.headers as Record<string, string>) || {}),
    }
    if (token) headers['Authorization'] = `Bearer ${token}`

    const init: RequestInit = {
      method,
      headers,
      ...opts,
      body: body !== undefined ? JSON.stringify(body) : undefined,
    }

    const res = await retry(() => fetch(url, init).then(async r => {
      if (!r.ok) {
        const text = await r.text().catch(() => '')
        throw new Error(`HTTP ${r.status} ${r.statusText} ${text}`)
      }
      if (r.status === 204) return undefined as unknown as T
      return r.json() as Promise<T>
    }))

    return res
  }

  return {
    get: (url, opts) => request('GET', url, undefined, opts),
    post: (url, body, opts) => request('POST', url, body, opts),
  }
}
