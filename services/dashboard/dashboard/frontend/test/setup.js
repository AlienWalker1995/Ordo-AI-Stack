// Runs before every test file: unmount what a test rendered and forget what it stored, so each
// test starts from an empty page and empty browser storage.
import { cleanup } from '@testing-library/react'
import { afterEach } from 'vitest'

// Node 22+ declares its own `localStorage` global (an empty accessor until --localstorage-file),
// and vitest's jsdom environment does not overwrite globals that already exist, so the browser's
// storage never reaches the tests. Install a small in-memory stand-in with the same surface the
// app and the tests use: fresh per test file, cleared after every test.
function createMemoryStorage() {
  const map = new Map()
  return {
    get length() { return map.size },
    key: (i) => [...map.keys()][i] ?? null,
    getItem: (k) => (map.has(k) ? map.get(k) : null),
    setItem: (k, v) => { map.set(String(k), String(v)) },
    removeItem: (k) => { map.delete(k) },
    clear: () => { map.clear() },
  }
}
if (typeof window !== 'undefined') {
  Object.defineProperty(globalThis, 'localStorage', { value: createMemoryStorage(), configurable: true, writable: true })
}

afterEach(() => {
  cleanup()
  localStorage.clear()
})
