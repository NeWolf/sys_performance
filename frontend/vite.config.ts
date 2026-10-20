import react from '@vitejs/plugin-react'
import { build, defineConfig } from 'vite'
import { fileURLToPath } from 'node:url'

// A separate IIFE embeds React, charts and styles, so exported reports need no server.
function comparisonReportRuntime() {
  const id = '\0virtual:comparison-report-runtime'
  let cached: Promise<string> | undefined
  return {
    name: 'comparison-report-runtime',
    resolveId(source: string) { if (source === 'virtual:comparison-report-runtime') return id },
    async load(source: string) {
      if (source !== id) return
      cached ??= build({
        configFile: false,
        plugins: [react()],
        define: { 'process.env.NODE_ENV': JSON.stringify('production') },
        build: {
          write: false,
          lib: { entry: fileURLToPath(new URL('./src/compareReportEntry.tsx', import.meta.url)), name: 'ComparisonReport', formats: ['iife'] },
          minify: true,
        },
      }).then((result) => {
        const outputs = Array.isArray(result) ? result : [result]
        const chunks = outputs.flatMap((output) => 'output' in output ? output.output : []).filter((output) => output.type === 'chunk')
        if (chunks.length !== 1) throw new Error('Expected a self-contained comparison report runtime')
        return chunks[0].code
      }).catch((error: unknown) => { cached = undefined; throw error })
      return `export default ${JSON.stringify(await cached)}`
    },
    handleHotUpdate() { cached = undefined },
  }
}

// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), comparisonReportRuntime()],
  server: {
    proxy: {
      '/api': { target: 'http://127.0.0.1:8765', changeOrigin: false },
    },
  },
})
