import { chmodSync, mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { resolvePython } from '../gatewayClient.js'

// A stub `python` is a shell script (not a real interpreter) so these tests
// stay hermetic and fast: `resolvePython` only cares that `python -c "import
// psutil"` exits 0 (a real, deps-installed venv) or non-zero (BUILD-412's
// stray uv-created venv with no deps).
const writeStubPython = (dir: string, ok: boolean) => {
  mkdirSync(join(dir, 'bin'), { recursive: true })
  const path = join(dir, 'bin', 'python')

  writeFileSync(path, `#!/bin/sh\nexit ${ok ? 0 : 1}\n`)
  chmodSync(path, 0o755)

  return path
}

describe('resolvePython (BUILD-507)', () => {
  let root: string

  beforeEach(() => {
    root = mkdtempSync(join(tmpdir(), 'hermes-resolvepython-'))
    vi.stubEnv('HERMES_PYTHON', '')
    vi.stubEnv('PYTHON', '')
    vi.stubEnv('VIRTUAL_ENV', '')
    vi.spyOn(console, 'warn').mockImplementation(() => {})
  })

  afterEach(() => {
    vi.unstubAllEnvs()
    vi.restoreAllMocks()
    rmSync(root, { force: true, recursive: true })
  })

  it('prefers the canonical venv/ over .venv/ when both are usable', () => {
    const venvPython = writeStubPython(join(root, 'venv'), true)

    writeStubPython(join(root, '.venv'), true)

    expect(resolvePython(root)).toBe(venvPython)
  })

  it('skips a broken venv/ (no psutil) and falls back to .venv/, warning with path + reason', () => {
    const brokenVenvPython = writeStubPython(join(root, 'venv'), false)
    const dotVenvPython = writeStubPython(join(root, '.venv'), true)

    expect(resolvePython(root)).toBe(dotVenvPython)
    expect(console.warn).toHaveBeenCalledWith(expect.stringContaining(brokenVenvPython))
    expect(console.warn).toHaveBeenCalledWith(expect.stringContaining('cannot import psutil'))
  })

  it('falls back to python3 and warns on both candidates when neither venv is usable', () => {
    writeStubPython(join(root, 'venv'), false)
    writeStubPython(join(root, '.venv'), false)

    expect(resolvePython(root)).toBe('python3')
    expect(console.warn).toHaveBeenCalledTimes(2)
  })

  it('warns "no python executable found" for an empty venv directory', () => {
    mkdirSync(join(root, 'venv'), { recursive: true })

    expect(resolvePython(root)).toBe('python3')
    expect(console.warn).toHaveBeenCalledWith(expect.stringContaining('no python executable found'))
  })

  it('short-circuits on HERMES_PYTHON without touching the filesystem at all', () => {
    vi.stubEnv('HERMES_PYTHON', '/custom/python')

    expect(resolvePython(root)).toBe('/custom/python')
    expect(console.warn).not.toHaveBeenCalled()
  })
})
