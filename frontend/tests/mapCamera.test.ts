import assert from 'node:assert/strict'
import { test } from 'node:test'
import { focusPadding, isFootprintClipped } from '../src/map/camera.ts'

test('address focus keeps usable map space in small and landscape viewports', () => {
  for (const [width, height] of [[320, 144], [390, 360], [844, 130], [1, 1]]) {
    const padding = focusPadding(width, height)
    assert.ok(width - padding.left - padding.right >= width * 0.6)
    assert.ok(height - padding.top - padding.bottom >= height * 0.6)
    assert.equal(padding.left, padding.right)
    assert.equal(padding.top, padding.bottom)
  }
})

test('desktop framing leaves room for map controls without oversized padding', () => {
  assert.deepEqual(focusPadding(1440, 900), { top: 96, right: 64, bottom: 96, left: 64 })
})

test('a selected footprint is reframed when the mobile preview reduces its visible area', () => {
  const footprint = [{ x: 120, y: 350 }, { x: 160, y: 350 }, { x: 160, y: 380 }, { x: 120, y: 380 }]
  assert.equal(isFootprintClipped(footprint, 390, 420), false)
  assert.equal(isFootprintClipped(footprint, 390, 340), true)
})

test('visible footprints retain their existing camera including at viewport edges', () => {
  assert.equal(isFootprintClipped([{ x: 0, y: 0 }, { x: 1000, y: 700 }], 1000, 700), false)
  for (const point of [{ x: -1, y: 30 }, { x: 1001, y: 30 }, { x: 30, y: -1 }, { x: 30, y: 701 }]) {
    assert.equal(isFootprintClipped([point], 1000, 700), true)
  }
})

test('an unmeasured viewport does not request a camera change', () => {
  assert.equal(isFootprintClipped([{ x: 10, y: 10 }], 0, 0), false)
  assert.equal(isFootprintClipped([], 390, 420), false)
})
