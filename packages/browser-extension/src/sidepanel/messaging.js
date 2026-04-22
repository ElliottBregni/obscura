// Pure helpers around the panel ↔ service-worker wire. Kept DOM- and
// chrome.*-free so Vitest can exercise them without shimming the browser.

/**
 * Return a new object with ``profile_id`` attached when provided. If
 * ``profileId`` is falsy, return the message unchanged (so boot-time
 * messages before we've generated an id aren't polluted with ``undefined``).
 */
export function withProfileId(msg, profileId) {
  if (!profileId) return msg;
  return { ...msg, profile_id: profileId };
}
