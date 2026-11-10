import assert from 'node:assert/strict'
import { test, type TestContext } from 'node:test'
import { getGroceryAssessment, type GroceryAssessmentData } from '../src/lib/groceryAssessment.ts'

const buildingId = 17

function fixture(): GroceryAssessmentData {
  return {
    building_id: buildingId,
    category: 'grocery',
    status: 'evidence_only',
    reason: 'A reliable recommendation is not available yet.',
    computed_at: '2026-11-10T12:00:00Z',
    evidence: [
      {
        id: 'office_count_500m', label: 'Offices', value: 4, unit: 'count', status: 'available',
        reason: null, radius_m: 500, coverage_fraction: 1, source_refs: ['warsaw-osm-activity'],
        method: 'Count of mapped places outside the selected building', limitation: 'Not customer traffic',
      },
      {
        id: 'school_count_500m', label: 'Schools', value: 0, unit: 'count', status: 'available',
        reason: null, radius_m: 500, coverage_fraction: 1, source_refs: ['warsaw-osm-activity'],
        method: 'Count of mapped places outside the selected building', limitation: 'Not customer traffic',
      },
      {
        id: 'resident_subtotal_500m', label: 'Residents in published cells within 500 m',
        value: 123.456, unit: 'people', status: 'partial', reason: 'Uncovered cells remain unknown',
        radius_m: 500, coverage_fraction: .4, source_refs: ['warsaw-population-2021'],
        method: 'Area-weighted published subtotal', limitation: '2021 census, not current footfall',
      },
    ],
    evidence_highlights: ['office_count_500m', 'school_count_500m'],
    requirements: [
      {
        brand_id: 'zabka', brand_name: 'Żabka', status: 'unknown', reviewed_at: '2026-11-01',
        formats: [{
          id: 'standard-store', name: 'Standard store', requirements: [{
            id: 'total-area', label: 'Total premises area above 70 m²', strength: 'mandatory', operator: 'gt',
            assessment_status: 'unknown', assessment_reason: 'The unit size is unconfirmed.',
            source_url: 'https://example.org/zabka-property',
          }],
        }],
        sources: [{ id: 'zabka-property', url: 'https://example.org/zabka-property', retrieved_at: '2026-11-01T12:00:00Z', published_at: null }],
        notes: ['Unit size is separate from building footprint area.'],
      },
      {
        brand_id: 'lewiatan', brand_name: 'Lewiatan', status: 'unknown', reviewed_at: '2026-11-01',
        formats: [{
          id: 'retail-store', name: 'Retail store', requirements: [{
            id: 'unit-area', label: 'Unit-area threshold not published', strength: 'unknown', operator: 'not_published',
            assessment_status: 'unknown', assessment_reason: 'Confirm with the brand.',
            source_url: 'https://example.org/lewiatan-conditions',
          }],
        }],
        sources: [{ id: 'lewiatan-conditions', url: 'https://example.org/lewiatan-conditions', retrieved_at: '2026-11-01T12:00:00Z', published_at: null }],
        notes: [],
      },
    ],
    missing_details: [{ id: 'usable_unit_area_m2', label: 'Usable commercial unit area', status: 'unknown' }],
    sources: [
      {
        id: 'warsaw-osm-activity', name: 'OpenStreetMap activity data', attribution: '© OpenStreetMap contributors',
        url: 'https://www.openstreetmap.org/copyright', observed_at: null,
        retrieved_at: '2026-11-01T12:00:00Z', snapshot_at: '2026-11-01T11:55:00Z',
        limitations: ['Observation date is unknown'], available: true,
      },
      {
        id: 'warsaw-population-2021', name: 'GUS 2021 census', attribution: 'GUS',
        url: 'https://example.org/census', observed_at: '2021-03-31',
        retrieved_at: '2026-10-23T12:00:00Z', snapshot_at: null, limitations: [], available: true,
      },
      {
        id: 'warsaw-buildings', name: 'Municipal building register', attribution: 'City of Warsaw',
        url: 'https://example.org/buildings', observed_at: null, retrieved_at: null, snapshot_at: null,
        limitations: ['Observation and retrieval dates are not supplied'], available: true,
      },
    ],
    ranking: { status: 'withheld', score: null, rank: null, reason: 'A reliable recommendation is not available yet.' },
    model: { status: 'validation_withheld', reason: 'More validation is required.', version: 'example-v1', evaluated_at: '2026-11-10T10:00:00Z' },
  }
}

function respond(context: TestContext, payload: unknown, status = 200) {
  return context.mock.method(globalThis, 'fetch', async () => new Response(JSON.stringify(payload), {
    status, headers: { 'Content-Type': 'application/json' },
  }))
}

test('accepts an evidence-only response while preserving source dates and published population precision', async (context) => {
  const payload = fixture()
  const fetch = respond(context, payload)
  const controller = new AbortController()
  const result = await getGroceryAssessment(buildingId, controller.signal)
  assert.deepEqual(result, payload)
  assert.equal(result.evidence[2].value, 123.456)
  assert.equal(result.evidence[2].status, 'partial')
  assert.equal(result.sources[2].observed_at, null)
  assert.equal(result.sources[2].retrieved_at, null)
  const [url, options] = fetch.mock.calls[0].arguments
  assert.equal(url, `/api/v1/map/buildings/${buildingId}/grocery-assessment`)
  assert.equal(options.signal, controller.signal)
  assert.equal(options.cache, 'no-store')
})

test('missing optional source metadata preserves the available evidence and null counts', async (context) => {
  const payload = fixture()
  payload.sources[0] = {
    ...payload.sources[0], name: null, attribution: null, url: null, observed_at: null,
    retrieved_at: null, snapshot_at: null, available: false, limitations: ['Source data is unavailable'],
  }
  for (const item of payload.evidence.filter((item) => item.source_refs.includes('warsaw-osm-activity'))) {
    item.status = 'unavailable'
    item.value = null
    item.reason = 'Source data is unavailable'
    item.coverage_fraction = null
  }
  respond(context, payload)
  const result = await getGroceryAssessment(buildingId, new AbortController().signal)
  assert.equal(result.evidence[0].value, null)
  assert.equal(result.evidence[1].value, null)
  assert.equal(result.evidence[2].value, 123.456)
  assert.equal(result.sources[0].name, null)
  assert.equal(result.requirements.length, 2)
})

test('a measured zero remains distinct from an unavailable count', async (context) => {
  const payload = fixture()
  payload.evidence[0] = { ...payload.evidence[0], value: null, status: 'missing', reason: 'No observation' }
  respond(context, payload)
  const result = await getGroceryAssessment(buildingId, new AbortController().signal)
  assert.equal(result.evidence[0].value, null)
  assert.equal(result.evidence[1].value, 0)
})

for (const status of ['validation_withheld', 'unavailable', 'stale', 'integrity_error', 'review_required'] as const) {
  test(`keeps facts available when model state is ${status}`, async (context) => {
    const payload = fixture()
    payload.model = { ...payload.model, status, version: null, evaluated_at: null }
    respond(context, payload)
    assert.equal((await getGroceryAssessment(buildingId, new AbortController().signal)).evidence[0].value, 4)
  })
}

test('rejects a response for a previously selected building', async (context) => {
  respond(context, { ...fixture(), building_id: buildingId + 1 })
  await assert.rejects(getGroceryAssessment(buildingId, new AbortController().signal), /could not be read/)
})

for (const position of ['ranking', 'model', 'top-level'] as const) {
  for (const field of ['score', 'rank'] as const) {
    for (const value of [0, .8]) {
      test(`rejects ${position} ${field}=${value} while recommendations are withheld`, async (context) => {
        const payload = fixture()
        const unsafe = position === 'top-level' ? { ...payload, [field]: value }
          : { ...payload, [position]: { ...payload[position], [field]: value } }
        respond(context, unsafe)
        await assert.rejects(getGroceryAssessment(buildingId, new AbortController().signal), /could not be read/)
      })
    }
  }
}

test('rejects a response claiming a recommendation is available', async (context) => {
  const payload = fixture()
  respond(context, { ...payload, ranking: { ...payload.ranking, status: 'available' } })
  await assert.rejects(getGroceryAssessment(buildingId, new AbortController().signal), /could not be read/)
})

for (const evidence of [
  { ...fixture().evidence[0], status: 'missing', value: 0 },
  { ...fixture().evidence[0], status: 'unavailable', value: 4 },
  { ...fixture().evidence[0], status: 'available', value: null },
  { ...fixture().evidence[0], status: 'partial', value: null },
  { ...fixture().evidence[0], value: -1 },
  { ...fixture().evidence[0], value: 1.5 },
  { ...fixture().evidence[0], coverage_fraction: 1.1 },
]) {
  test(`rejects inconsistent evidence ${JSON.stringify({ status: evidence.status, value: evidence.value, coverage: evidence.coverage_fraction })}`, async (context) => {
    const payload = fixture()
    respond(context, { ...payload, evidence: [evidence, ...payload.evidence.slice(1)] })
    await assert.rejects(getGroceryAssessment(buildingId, new AbortController().signal), /could not be read/)
  })
}

test('rejects a falsely confirmed premises requirement', async (context) => {
  const payload = fixture()
  const brand = payload.requirements[0]
  const format = brand.formats[0]
  const requirement = format.requirements[0]
  respond(context, {
    ...payload,
    requirements: [{ ...brand, formats: [{ ...format, requirements: [{ ...requirement, assessment_status: 'pass' }] }] }, ...payload.requirements.slice(1)],
  })
  await assert.rejects(getGroceryAssessment(buildingId, new AbortController().signal), /could not be read/)
})

for (const invalidHighlights of [false, true]) {
  test(`rejects ${invalidHighlights ? 'an unknown highlighted fact' : 'duplicate evidence identifiers'}`, async (context) => {
    const payload = fixture()
    respond(context, invalidHighlights ? { ...payload, evidence_highlights: ['absent_fact'] }
      : { ...payload, evidence: [payload.evidence[0], payload.evidence[0]] })
    await assert.rejects(getGroceryAssessment(buildingId, new AbortController().signal), /could not be read/)
  })
}

for (const status of [404, 500, 503]) {
  test(`HTTP ${status} fails without exposing server diagnostics`, async (context) => {
    respond(context, { detail: 'Internal diagnostic with source identifiers' }, status)
    await assert.rejects(getGroceryAssessment(buildingId, new AbortController().signal), {
      message: 'Grocery information is temporarily unavailable.',
    })
  })
}

test('rejects invalid JSON instead of returning an empty assessment', async (context) => {
  context.mock.method(globalThis, 'fetch', async () => new Response('not-json', { status: 200 }))
  await assert.rejects(getGroceryAssessment(buildingId, new AbortController().signal), SyntaxError)
})

test('propagates cancellation through the supplied signal', async (context) => {
  const controller = new AbortController()
  context.mock.method(globalThis, 'fetch', async (_url: unknown, options: RequestInit) => {
    assert.equal(options.signal, controller.signal)
    await new Promise((_resolve, reject) => options.signal?.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')), { once: true }))
    return new Response('{}')
  })
  const request = getGroceryAssessment(buildingId, controller.signal)
  controller.abort()
  await assert.rejects(request, { name: 'AbortError' })
})

test('propagates network errors for the panel retry state', async (context) => {
  context.mock.method(globalThis, 'fetch', async () => { throw new TypeError('Network unavailable') })
  await assert.rejects(getGroceryAssessment(buildingId, new AbortController().signal), TypeError)
})
