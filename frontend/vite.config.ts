import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import { execFileSync } from 'node:child_process'
import { fileURLToPath } from 'node:url'
import { resolve } from 'node:path'

const repositoryRoot = resolve(
  fileURLToPath(new URL('.', import.meta.url)),
  '..',
)

function gitOutput(...args: string[]): string | null {
  try {
    return execFileSync('git', ['-C', repositoryRoot, ...args], {
      encoding: 'utf8',
      timeout: 5000,
    }).trim()
  } catch {
    return null
  }
}

const discoveredCommit = gitOutput('rev-parse', 'HEAD')?.toLowerCase()
const frontendRepositoryCommit = (
  discoveredCommit && /^[0-9a-f]{40}$/.test(discoveredCommit)
    ? discoveredCommit
    : null
)
const repositoryStatus = gitOutput(
  'status',
  '--porcelain',
  '--untracked-files=normal',
)
const frontendRepositoryDirty = (
  repositoryStatus === null ? null : repositoryStatus.length > 0
)

// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), tailwindcss()],
  define: {
    __S2S_FRONTEND_REPOSITORY_COMMIT__: JSON.stringify(
      frontendRepositoryCommit,
    ),
    __S2S_FRONTEND_REPOSITORY_DIRTY__: JSON.stringify(
      frontendRepositoryDirty,
    ),
  },
  server: {
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
      '/ws': {
        target: 'http://127.0.0.1:8000',
        ws: true,
        changeOrigin: true,
      },
    },
  },
})
