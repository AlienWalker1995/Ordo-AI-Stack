// Each of the five pages, at a phone width and a desktop width: it renders its content, logs no
// console errors, calls no endpoint the fixtures do not cover, and never scrolls sideways.
import { expect, test } from '@playwright/test'
import { mockApi } from './mockApi.js'

const WIDTHS = [
  { name: 'phone', width: 414, height: 896 },
  { name: 'desktop', width: 1280, height: 900 },
]

// What each page must show once its data has loaded.
const PAGES = [
  { id: 'overview', headings: ['GPUs right now', 'Needs attention', 'Recent activity'], text: 'RTX 5090' },
  { id: 'services', headings: [], text: '1 needs attention' },
  { id: 'models', headings: ['Running now', 'Model files on disk'], text: 'qwen3.8-27b-turbo' },
  { id: 'media', headings: ['ComfyUI', 'Recent outputs', 'GPU leases', 'Installed ComfyUI models'], text: 'Rendering now' },
  { id: 'performance', headings: [], text: 'Open in Grafana' },
]

async function openPage(page, id) {
  const problems = []
  page.on('console', (msg) => { if (msg.type() === 'error') problems.push(`console: ${msg.text()}`) })
  page.on('pageerror', (err) => problems.push(`page error: ${err.message}`))
  const unhandled = await mockApi(page)
  await page.goto(`/#${id}`)
  await expect(page.getByRole('tab', { name: new RegExp(`^${id}$`, 'i') })).toHaveAttribute('aria-selected', 'true')
  return { problems, unhandled }
}

async function horizontalOverflow(page) {
  return page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)
}

for (const viewport of WIDTHS) {
  test.describe(`${viewport.name} (${viewport.width} px)`, () => {
    test.use({ viewport: { width: viewport.width, height: viewport.height } })

    for (const { id, headings, text } of PAGES) {
      test(`${id} renders cleanly`, async ({ page }) => {
        const { problems, unhandled } = await openPage(page, id)

        await expect(page.getByText(text).filter({ visible: true }).first()).toBeVisible()
        for (const name of headings) {
          await expect(page.getByRole('heading', { name, exact: true })).toBeVisible()
        }
        // No skeleton left once the data is in.
        await expect(page.locator('#page .skeleton')).toHaveCount(0)

        expect(await horizontalOverflow(page)).toBe(0)
        expect(unhandled).toEqual([])
        expect(problems).toEqual([])
      })
    }
  })
}

test.describe('Services layout', () => {
  test('a phone gets cards with every action on screen', async ({ page }) => {
    await page.setViewportSize({ width: 414, height: 896 })
    const { problems } = await openPage(page, 'services')

    const stop = page.getByRole('button', { name: 'Stop Open WebUI' })
    await expect(stop).toHaveCount(1)
    await expect(stop).toBeVisible()
    await expect(page.getByRole('listitem').filter({ has: stop })).toBeVisible()
    await expect(page.locator('table').first()).toBeAttached()
    await expect(page.getByRole('table')).toHaveCount(0)

    const box = await stop.boundingBox()
    expect(box.x + box.width).toBeLessThanOrEqual(414)
    expect(problems).toEqual([])
  })

  test('a desktop gets the table', async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 900 })
    const { problems } = await openPage(page, 'services')

    const stop = page.getByRole('button', { name: 'Stop Open WebUI' })
    await expect(stop).toHaveCount(1)
    await expect(page.getByRole('row').filter({ has: stop })).toBeVisible()
    await expect(page.getByRole('columnheader', { name: 'Service' }).first()).toBeVisible()
    expect(problems).toEqual([])
  })
})
