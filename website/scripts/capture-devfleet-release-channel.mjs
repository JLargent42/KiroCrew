/**
 * Screenshots of the Dev Fleet release-channel worktree rows (PR #10066).
 *
 * Drives the ISOLATED capture entry (website/capture/devfleet-release-channel.html),
 * which mounts the REAL DevFleetPage with `fetch` stubbed at the network seam to
 * serve the `/fleet` payload. Every state under review is decided by that payload,
 * so each page load here IS the scenario the reviewer asked to see.
 *
 * Each scene asserts its headline state — and, where the claim is an ABSENCE,
 * also asserts the thing that must not be there — before shooting, so this can
 * never quietly emit a screenshot of an error boundary, a prerequisite gate, or
 * the wrong row class. The frames are then compared for distinct byte sizes:
 * two identical frames mean the fixture failed, not that the states match.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6812 --strictPort   # in another shell
 *   node scripts/capture-devfleet-release-channel.mjs http://127.0.0.1:6812 ../temp-screenshots/devfleet-release-channel-10066
 */
import { chromium } from 'playwright'
import { mkdirSync, statSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6812'
const OUT = process.argv[3] || '../temp-screenshots/devfleet-release-channel-10066'
mkdirSync(OUT, { recursive: true })

const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 1280, height: 720 } })

const shot = []

async function shoot(scene, mustSee, mustNotSee, name) {
  await page.goto(`${BASE}/capture/devfleet-release-channel.html?scene=${scene}&theme=dark`)
  for (const text of mustSee) {
    await page.waitForSelector(`text=${text}`, { timeout: 15000 })
  }
  // Absence is only meaningful once the row itself is on screen, which the
  // mustSee loop above has just established.
  for (const text of mustNotSee) {
    const n = await page.locator(`text=${text}`).count()
    if (n !== 0) throw new Error(`scene=${scene}: "${text}" must NOT be present, found ${n}`)
  }
  // Park the pointer away from the table: a hovered row raises its action
  // toolbar over the row name in every one of these harnesses.
  await page.mouse.move(4, 4)
  await page.waitForTimeout(400) // let the relative timestamps settle
  const path = `${OUT}/${name}`
  await page.screenshot({ path, fullPage: false })
  shot.push({ name, size: statSync(path).size })
  console.log(`captured ${name}`)
}

// The lane is materialized: the badge carries the release this checkout sits on,
// and the Behind cell names the channel tip as its denominator so it cannot be
// read as the feature row's behind-main figure.
await shoot('adopted', ['release-channel-stable', '0.5.0', 'tip'], [], '01-release-channel-adopted-dark.png')

// The lane exists with no worktree: a muted placeholder row is the only surface
// the feature is discoverable from, and its Create button must read as live.
await shoot('placeholder', ['release-channel-stable', 'no worktree yet', 'Create'], [],
  '02-release-channel-placeholder-create-dark.png')

// A BRANCH checkout occupying the reserved basename is NOT adopted: ordinary
// controls, an ordinary behind-main count, and no version badge.
await shoot('taken', ['release-channel-stable', '↓5'], ['0.5.0'],
  '03-release-channel-name-taken-branch-dark.png')

// Two frames of identical size are what a silently-failed fixture looks like.
const sizes = new Set(shot.map((s) => s.size))
if (sizes.size !== shot.length) {
  throw new Error(`frames are not distinct: ${shot.map((s) => `${s.name}=${s.size}`).join(', ')}`)
}
console.log(shot.map((s) => `${s.name} ${s.size}B`).join('\n'))

await browser.close()
