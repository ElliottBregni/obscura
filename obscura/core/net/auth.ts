export type TokenProvider = () => string | Promise<string | undefined> | undefined

export function asAsyncTokenProvider(fn: TokenProvider): () => Promise<string | undefined> {
  return async () => Promise.resolve(fn())
}

export async function getTokenMaybe(provider?: TokenProvider): Promise<string | undefined> {
  if (!provider) return undefined
  return Promise.resolve(provider())
}
