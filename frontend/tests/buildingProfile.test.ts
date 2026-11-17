import assert from 'node:assert/strict'
import { test, type TestContext } from 'node:test'
import { getBuildingProfile, type BuildingProfileData, type PopulationContext } from '../src/lib/buildingProfile.ts'

function fixture(): BuildingProfileData {
  return {
    building_id: 17,
    radius_m: 500,
    available_radii_m: [300, 500, 1000],
    origin: { coordinates: [21, 52.2], method: 'building_point_on_surface' },
    computed_at: '2026-11-17T12:00:00Z',
    context_status: 'partial',
    population: {
      status: 'partial', estimated_residents: 123.456, coverage_fraction: .4,
      observation_year: 2021, resolution_m: 125, method: 'area_weighted_grid', source: null,
    },
    competition: {
      status: 'partial', categories: [{ id: 'grocery', name: 'Grocery', count: null, official_count: null, osm_count: null, nearest: [] }],
      onsite_outlet_count: 0, deduplicated_count: 0, sources: [],
    },
    access: { status: 'unavailable', method: 'straight_line', items: [], source: null },
    premises: { status: 'unknown', unknown_fields: [{ id: 'usable_unit_area', label: 'Unit size' }] },
    quality_notes: [],
  }
}

function censusSquare(): PopulationContext {
  return {
    status: 'available', scope: 'containing_census_cell', population: 1000,
    resolution_m: 500, observation_year: 2021, cell_id: 'fixture-cell', method: 'official_national_population_grid',
    source: {
      id: 'census-500', name: 'Official 2021 census', url: 'https://example.org/census', attribution: 'GUS',
      license: null, retrieved_at: '2026-11-17T10:00:00Z', observed_at: '2021-03-31', notes: null,
    },
  }
}

function respond(context: TestContext, payload: unknown) {
  return context.mock.method(globalThis, 'fetch', async () => new Response(JSON.stringify(payload), {
    status: 200, headers: { 'Content-Type': 'application/json' },
  }))
}

test('older profiles remain readable without the optional census square', async (context) => {
  respond(context, fixture())
  const result = await getBuildingProfile(17, 500, new AbortController().signal)
  assert.equal(result.population_context, undefined)
  assert.equal(result.population.estimated_residents, 123.456)
})

test('the official census square stays distinct from the published neighborhood subtotal', async (context) => {
  respond(context, { ...fixture(), population_context: censusSquare() })
  const result = await getBuildingProfile(17, 500, new AbortController().signal)
  assert.equal(result.population.estimated_residents, 123.456)
  assert.equal(result.population.resolution_m, 125)
  assert.equal(result.population_context?.population, 1000)
  assert.equal(result.population_context?.resolution_m, 500)
  assert.equal(result.population_context?.source?.observed_at, '2021-03-31')
  assert.equal(result.population_context?.source?.retrieved_at, '2026-11-17T10:00:00Z')
})

test('an explicitly observed census zero is preserved', async (context) => {
  respond(context, { ...fixture(), population_context: { ...censusSquare(), population: 0 } })
  const result = await getBuildingProfile(17, 500, new AbortController().signal)
  assert.equal(result.population_context?.population, 0)
  assert.equal(result.population_context?.status, 'available')
})

test('unavailable census context remains unknown without hiding other facts', async (context) => {
  respond(context, { ...fixture(), population_context: { ...censusSquare(), status: 'unavailable', population: null, cell_id: null, source: null } })
  const result = await getBuildingProfile(17, 500, new AbortController().signal)
  assert.equal(result.population_context?.population, null)
  assert.equal(result.population.estimated_residents, 123.456)
  assert.equal(result.competition.categories[0].count, null)
})

test('changing the neighborhood radius does not relabel the census square', async (context) => {
  const payload = { ...fixture(), radius_m: 300, population_context: censusSquare() }
  const request = respond(context, payload)
  const result = await getBuildingProfile(17, 300, new AbortController().signal)
  assert.equal(result.population_context?.scope, 'containing_census_cell')
  assert.equal(result.population_context?.resolution_m, 500)
  assert.match(String(request.mock.calls[0].arguments[0]), /radius_m=300$/)
})

for (const [name, change] of Object.entries({
  'an unavailable square containing a count': { status: 'unavailable', population: 0 },
  'an available square with a missing count': { status: 'available', population: null },
  'a negative count': { population: -1 },
  'an estimated fractional cell count': { population: 1.5 },
  'a numeric string': { population: '12' },
  'a different cell resolution': { resolution_m: 125 },
  'a different observation year': { observation_year: 2026 },
  'a radius masquerading as a census square': { scope: 'selected_radius' },
  'a derived estimate masquerading as an official count': { method: 'area_weighted_grid' },
  'a malformed source': { source: { name: 'GUS' } },
})) {
  test(`rejects ${name}`, async (context) => {
    respond(context, { ...fixture(), population_context: { ...censusSquare(), ...change } })
    await assert.rejects(getBuildingProfile(17, 500, new AbortController().signal), /response could not be read/)
  })
}
