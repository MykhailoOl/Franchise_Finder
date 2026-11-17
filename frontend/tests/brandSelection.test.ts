import assert from 'node:assert/strict'
import { test } from 'node:test'
import { resolveBrandSelection } from '../src/lib/brandSelection.ts'

const brands = [
  { id: 'zabka', category_id: 'grocery' },
  { id: 'lewiatan', category_id: 'grocery' },
  { id: 'subway', category_id: 'quick-service' },
  { id: 'yasumi', category_id: 'beauty' },
]

test('the initial map shows grocery brands only', () => {
  assert.deepEqual(resolveBrandSelection(brands, null), ['zabka', 'lewiatan'])
})

test('other categories remain available through explicit selection', () => {
  assert.deepEqual(resolveBrandSelection(brands, 'all'), brands.map((brand) => brand.id))
  assert.deepEqual(resolveBrandSelection(brands, ['lewiatan', 'subway']), ['lewiatan', 'subway'])
})

test('hiding all brands stays empty and removed brands do not return', () => {
  assert.deepEqual(resolveBrandSelection(brands, []), [])
  assert.deepEqual(resolveBrandSelection(brands, ['missing-brand', 'zabka']), ['zabka'])
})

test('an unavailable grocery category does not silently select unrelated brands', () => {
  assert.deepEqual(resolveBrandSelection(brands.filter((brand) => brand.category_id !== 'grocery'), null), [])
})
