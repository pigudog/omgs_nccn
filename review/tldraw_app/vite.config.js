import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import { fileURLToPath } from 'node:url'
import path from 'node:path'

const repoRoot = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  '..',
  '..'
)

export default defineConfig({
  plugins: [react()],
  define: {
    __OMGS_REPO_ROOT__: JSON.stringify(repoRoot),
  },
  server: {
    fs: {
      allow: [repoRoot],
    },
  },
})
