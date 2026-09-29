// Runs before every test file: unmount what a test rendered and forget what it stored, so each
// test starts from an empty page and empty browser storage.
import { cleanup } from '@testing-library/react'
import { afterEach } from 'vitest'

afterEach(() => {
  cleanup()
  localStorage.clear()
})
