export type RetryOptions = {
  retries?: number
  minDelayMs?: number
  maxDelayMs?: number
  factor?: number
}

export async function retry<T>(
  fn: () => Promise<T>,
  opts: RetryOptions = {},
): Promise<T> {
  const retries = opts.retries ?? 3
  const minDelay = opts.minDelayMs ?? 100
  const maxDelay = opts.maxDelayMs ?? 2000
  const factor = opts.factor ?? 2

  let attempt = 0
  let lastError: unknown

  while (attempt <= retries) {
    try {
      return await fn()
    } catch (err) {
      lastError = err
      if (attempt === retries) break
      const delay = Math.min(maxDelay, minDelay * Math.pow(factor, attempt))
      await new Promise(res => setTimeout(res, delay))
      attempt++
    }
  }

  throw lastError
}
