// Screenshots for a crewmate DM whose newest history is all machinery: the
// scoped hint over a widened page that still has older rows behind it, and
// the whole-history hint once the widened page reaches the start.
//   node capture/shoot-crewmate-quiet-recent.mjs [outDir] [port]
import { createServer } from 'vite'
import { chromium } from 'playwright-core'
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'

const OUT = process.argv[2] || 'crewmate-quiet-recent-shots'
const PORT = Number(process.argv[3] || 5179)
const CHROMIUM_PATH = process.env.CHROMIUM_PATH
mkdirSync(OUT, { recursive: true })

const server = await createServer({
  configFile: 'vite.config.ts',
  server: { port: PORT, strictPort: true, host: '127.0.0.1' },
})
await server.listen()
console.log(`vite up on ${PORT}`)

const browser = await chromium.launch({ executablePath: CHROMIUM_PATH, args: ['--no-sandbox'] })

async function shoot(theme, scope, name) {
  const ctx = await browser.newContext({ viewport: { width: 720, height: 520 }, reducedMotion: 'reduce' })
  const page = await ctx.newPage()
  await page.goto(`http://127.0.0.1:${PORT}/capture/crewmate-quiet-recent.html?theme=${theme}&scope=${scope}`)
  await page.waitForSelector('[data-testid="crewmate-quiet-hint"]', { timeout: 30000 })
  await page.waitForTimeout(300)
  await page.screenshot({ path: join(OUT, `${name}.png`) })
  console.log('shot', name)
  await ctx.close()
}

await shoot('dark', 'recent', '01-dark-recent')
await shoot('light', 'recent', '02-light-recent')
await shoot('dark', 'whole', '03-dark-whole')

await browser.close()
await server.close()
