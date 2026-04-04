import { getTokenMaybe, TokenProvider } from './auth.js'

export type WsCallbacks = {
  onOpen?: () => void
  onMessage?: (data: unknown) => void
  onClose?: (code?: number, reason?: string) => void
  onError?: (err: Error) => void
}

export class WsClient {
  private ws: WebSocket | null = null
  private reconnectTimer: NodeJS.Timeout | null = null

  constructor(private readonly url: string, private readonly tokenProvider?: TokenProvider, private readonly callbacks: WsCallbacks = {}) {}

  async connect(): Promise<void> {
    const token = await getTokenMaybe(this.tokenProvider)
    const url = token ? `${this.url}${this.url.includes('?') ? '&' : '?'}token=${encodeURIComponent(token)}` : this.url

    if (typeof WebSocket !== 'undefined') {
      this.ws = new WebSocket(url)
      this.ws.addEventListener('open', () => this.callbacks.onOpen?.())
      this.ws.addEventListener('message', e => {
        try { this.callbacks.onMessage?.(JSON.parse(String(e.data))) } catch { this.callbacks.onMessage?.(String(e.data)) }
      })
      this.ws.addEventListener('close', ev => this.callbacks.onClose?.(ev.code, ev.reason?.toString()))
      this.ws.addEventListener('error', err => this.callbacks.onError?.(err as Error))
    } else {
      // Node environment: dynamic import ws package
      const { default: WebSocketLib } = await import('ws') as any
      this.ws = new WebSocketLib(url) as unknown as WebSocket
      this.ws.onopen = () => this.callbacks.onOpen?.()
      // @ts-ignore
      this.ws.onmessage = (ev: any) => { try { this.callbacks.onMessage?.(JSON.parse(String(ev.data))) } catch { this.callbacks.onMessage?.(String(ev.data)) } }
      // @ts-ignore
      this.ws.onclose = (ev: any) => this.callbacks.onClose?.(ev.code, ev.reason?.toString())
      // @ts-ignore
      this.ws.onerror = (err: any) => this.callbacks.onError?.(err)
    }
  }

  send(data: unknown): void {
    if (!this.ws) throw new Error('WebSocket not connected')
    const str = typeof data === 'string' ? data : JSON.stringify(data)
    this.ws.send(str)
  }

  close(): void {
    this.reconnectTimer && clearTimeout(this.reconnectTimer)
    this.ws?.close()
    this.ws = null
  }
}
