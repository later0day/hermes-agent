/** Unsaved memory edits survive a profile switch or reload: the editor remounts per profile, which used to
 *  drop them silently. Session-scoped (sessionStorage) and keyed by profile + document. */
const key = (profile: string, doc: string) => `hermes-memory-draft:${profile}:${doc}`

export function readMemoryDraft(profile: string, doc: string): string | null {
  try {
    return sessionStorage.getItem(key(profile, doc))
  } catch {
    return null
  }
}

export function writeMemoryDraft(profile: string, doc: string, text: string | null): void {
  try {
    if (text === null) sessionStorage.removeItem(key(profile, doc))
    else sessionStorage.setItem(key(profile, doc), text)
  } catch {
    // storage full or disabled: the in-memory edit still works, it just won't survive a remount
  }
}
