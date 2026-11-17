import assert from 'node:assert/strict'
import { after, afterEach, before, beforeEach, test } from 'node:test'
import { setTimeout as delay } from 'node:timers/promises'
import { JSDOM } from 'jsdom'
import { act, createElement } from 'react'
import { createServer } from 'vite'
import react from '@vitejs/plugin-react'
import type { Root } from 'react-dom/client'
import type { AddressSearchResponse, AddressSearchResult, BuildingFeature, FranchiseCatalog } from '../src/lib/api.ts'
import type { BuildingProfileData, ProfileRadius } from '../src/lib/buildingProfile.ts'
import type { GroceryAssessmentData } from '../src/lib/groceryAssessment.ts'

const dom = new JSDOM('<!doctype html><html><body><main></main></body></html>', { url: 'http://localhost:5173/' })
for (const name of ['window', 'document', 'HTMLElement', 'HTMLInputElement', 'Node', 'Event', 'MouseEvent', 'KeyboardEvent']) {
  Object.defineProperty(globalThis, name, { configurable: true, value: dom.window[name] })
}
Object.defineProperty(globalThis, 'IS_REACT_ACT_ENVIRONMENT', { configurable: true, value: true })
Object.defineProperty(dom.window, 'matchMedia', {
  value: (query: string) => ({
    matches: query === '(max-width: 760px)', media: query,
    addEventListener() {}, removeEventListener() {},
  }),
})

const nativeFetch = globalThis.fetch
const requests: { url: string; signal: AbortSignal | null; resolve: (response: Response) => void; reject: (reason: Error) => void }[] = []
let server: Awaited<ReturnType<typeof createServer>>
let root: Root
let AddressSearch: typeof import('../src/components/AddressSearch.tsx').AddressSearch
let BuildingProfile: typeof import('../src/components/BuildingProfile.tsx').BuildingProfile
let App: typeof import('../src/App.tsx').default

before(async () => {
  server = await createServer({
    configFile: false,
    plugins: [{
      name: 'map-interaction-test-double',
      enforce: 'pre',
      load(id) {
        if (id.replaceAll('\\', '/').endsWith('/src/map/WarsawMap.tsx')) {
          return `import { createElement } from 'react'
export function WarsawMap(props) {
  return createElement('section', { 'aria-label': 'Map interaction test double' },
    createElement('button', { onClick: () => props.onSelect(109106) }, 'Select second building on map'),
    createElement('output', { 'aria-label': 'Map selected building' }, props.selectedId ?? 'None'))
}`
        }
      },
    }, react()],
    server: { middlewareMode: true, watch: null, hmr: false },
    appType: 'custom',
    logLevel: 'error',
    optimizeDeps: { noDiscovery: true, include: [] },
  })
  AddressSearch = (await server.ssrLoadModule('/src/components/AddressSearch.tsx')).AddressSearch
  BuildingProfile = (await server.ssrLoadModule('/src/components/BuildingProfile.tsx')).BuildingProfile
  App = (await server.ssrLoadModule('/src/App.tsx')).default
})

beforeEach(async () => {
  requests.length = 0
  globalThis.fetch = (input, options) => new Promise((resolve, reject) => {
    requests.push({ url: String(input), signal: options?.signal ?? null, resolve, reject })
  })
  const { createRoot } = await import('react-dom/client')
  root = createRoot(document.querySelector('main')!)
})

afterEach(async () => {
  await act(async () => root.unmount())
  globalThis.fetch = nativeFetch
})

after(async () => {
  await server?.close()
  dom.window.close()
})

function input() {
  return document.querySelector<HTMLInputElement>('[role="combobox"]')!
}

async function typeQuery(value: string) {
  await act(async () => {
    input().focus()
    Object.getOwnPropertyDescriptor(dom.window.HTMLInputElement.prototype, 'value')!.set!.call(input(), value)
    input().dispatchEvent(new dom.window.Event('input', { bubbles: true }))
  })
}

async function submit() {
  await act(async () => {
    document.querySelector('form')!.dispatchEvent(new dom.window.Event('submit', { bubbles: true, cancelable: true }))
  })
  await act(async () => { await delay(10) })
}

async function key(value: string) {
  await act(async () => {
    input().dispatchEvent(new dom.window.KeyboardEvent('keydown', { key: value, bubbles: true, cancelable: true }))
  })
}

function address(id: number, street: string): AddressSearchResult {
  return {
    id, address: `${street} 47, Warszawa`, street, house_number: '47', postal_code: '03-801',
    coordinates: [21.049578, 52.247677], building_id: id, match_status: 'matched',
    match_method: 'point_inside_building', distance_m: 0,
  }
}

function searchPayload(query: string, result: AddressSearchResult): AddressSearchResponse {
  return { query, status: 'ready', results: [result], best_match_id: result.id, source: null }
}

async function respond(index: number, payload: unknown) {
  assert.ok(requests[index], `Request ${index} exists`)
  await act(async () => {
    requests[index].resolve(new Response(JSON.stringify(payload), { headers: { 'Content-Type': 'application/json' } }))
  })
}

function profile(buildingId: number, radius: ProfileRadius): BuildingProfileData {
  return {
    building_id: buildingId, radius_m: radius, available_radii_m: [300, 500, 1000],
    origin: { coordinates: [21.049578, 52.247677], method: 'building_point_on_surface' },
    computed_at: '2026-11-17T12:00:00Z', context_status: 'partial',
    population: {
      status: 'partial', estimated_residents: radius, coverage_fraction: .4,
      observation_year: 2021, resolution_m: 125, method: 'area_weighted_grid', source: null,
    },
    population_context: {
      status: 'available', scope: 'containing_census_cell', population: 1985,
      resolution_m: 500, observation_year: 2021, cell_id: 'fixture-cell', source: null,
      method: 'official_national_population_grid',
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

function profileRequest(buildingId: number, radius: ProfileRadius) {
  const index = requests.findIndex((request) => request.url === `/api/v1/map/buildings/${buildingId}/profile?radius_m=${radius}`)
  assert.notEqual(index, -1)
  return index
}

function source() {
  return { name: 'Municipal public data', attribution: 'City of Warsaw', url: 'https://example.org/buildings', updated_at: null }
}

function catalog(): FranchiseCatalog {
  return {
    status: 'ready', categories: [{ id: 'grocery', name: 'Grocery' }],
    brands: ['zabka', 'lewiatan'].map((id) => ({
      id, name: id === 'zabka' ? 'Żabka' : 'Lewiatan', category_id: 'grocery',
      website: `https://example.org/${id}`, source_url: `https://example.org/${id}/locations`,
      retrieved_at: '2026-11-17T12:00:00Z', coverage_status: 'partial', coverage_note: 'Public listings',
      tile_property: id, outlet_count: 1, mapped_count: 1, matched_count: 1,
    })),
    summary: { total_outlets: 2, mapped_outlets: 2, matched_outlets: 2 },
  }
}

function building(buildingId: number, street: string): BuildingFeature {
  return {
    type: 'Feature', id: buildingId,
    geometry: { type: 'MultiPolygon', coordinates: [[[[21, 52], [21.001, 52], [21.001, 52.001], [21, 52]]]] },
    properties: {
      source_id: `building-${buildingId}`, source: source(), building_type: null,
      address: `${street} 47, Warszawa`, addresses: [], franchises: [], area_m2: 120,
      imported_at: '2026-11-17T12:00:00Z', attributes: {},
    },
  }
}

function assessment(buildingId: number): GroceryAssessmentData {
  return {
    building_id: buildingId, category: 'grocery', status: 'evidence_only',
    reason: 'A reliable recommendation is not available yet.', computed_at: '2026-11-17T12:00:00Z',
    evidence: [], evidence_highlights: [], requirements: [], missing_details: [], sources: [],
    ranking: { status: 'withheld', score: null, rank: null, reason: 'A reliable recommendation is not available yet.' },
    model: { status: 'stale', reason: 'Public data changed', version: 'test-internal-model', evaluated_at: null },
  }
}

async function respondAt(path: string, payload: unknown) {
  const index = requests.findIndex((request) => request.url === path)
  assert.notEqual(index, -1, path)
  await respond(index, payload)
}

async function loadApp() {
  await act(async () => root.render(createElement(App)))
  await respondAt('/api/v1/map/metadata', {
    status: 'ready', bounds: [20.8, 52.1, 21.3, 52.4], minzoom: 13, maxzoom: 18,
    feature_count: 2, last_imported_at: '2026-11-17T12:00:00Z', source: source(),
  })
  await respondAt('/api/v1/map/franchises', catalog())
  await respondAt('/api/v1/map/outlets', { type: 'FeatureCollection', features: [] })
}

async function selectDemoAddress() {
  await typeQuery('Jana Zamoyskiego 47')
  await submit()
  const index = requests.findIndex((request) => request.url.startsWith('/api/v1/map/addresses/search?'))
  await respond(index, searchPayload('Jana Zamoyskiego 47', address(58109, 'Jana Zamoyskiego')))
  await respondAt('/api/v1/map/buildings/58109', building(58109, 'Jana Zamoyskiego'))
  await respond(profileRequest(58109, 500), profile(58109, 500))
  await respondAt('/api/v1/map/buildings/58109/grocery-assessment', assessment(58109))
}

async function clickButton(label: string) {
  const button = Array.from(document.querySelectorAll('button')).find((item) => item.textContent?.trim() === label)
  assert.ok(button, label)
  await act(async () => button.click())
}

async function chooseRadius(radius: ProfileRadius) {
  await act(async () => {
    document.querySelector<HTMLInputElement>(`input[type="radio"][value="${radius}"]`)!.click()
  })
}

test('an explicit address search selects the matching building and dismisses the search keyboard', async () => {
  const selected: AddressSearchResult[] = []
  await act(async () => root.render(createElement(AddressSearch, { onSelect: (result) => selected.push(result) })))
  await typeQuery('Jana Zamoyskiego 47')
  await submit()
  const result = address(58109, 'Jana Zamoyskiego')
  await respond(0, searchPayload('Jana Zamoyskiego 47', result))
  assert.deepEqual(selected, [result])
  assert.equal(input().value, result.address)
  assert.equal(input().getAttribute('aria-expanded'), 'false')
  assert.notEqual(document.activeElement, input())
})

test('a late response cannot replace a newer submitted address', async () => {
  const selected: AddressSearchResult[] = []
  await act(async () => root.render(createElement(AddressSearch, { onSelect: (result) => selected.push(result) })))
  await typeQuery('Stanisława Kazury 2F')
  await submit()
  await typeQuery('Jana Zamoyskiego 47')
  await submit()
  assert.equal(requests[0].signal?.aborted, true)
  const result = address(58109, 'Jana Zamoyskiego')
  await respond(1, searchPayload('Jana Zamoyskiego 47', result))
  await respond(0, searchPayload('Stanisława Kazury 2F', address(109106, 'Stanisława Kazury')))
  assert.deepEqual(selected, [result])
  assert.equal(input().value, result.address)
  assert.equal(input().getAttribute('aria-expanded'), 'false')
})

test('Escape cancels pending address selection without reopening suggestions on response', async () => {
  const selected: AddressSearchResult[] = []
  await act(async () => root.render(createElement(AddressSearch, { onSelect: (result) => selected.push(result) })))
  await typeQuery('Jana Zamoyskiego 47')
  await submit()
  await key('Escape')
  await respond(0, searchPayload('Jana Zamoyskiego 47', address(58109, 'Jana Zamoyskiego')))
  assert.deepEqual(selected, [])
  assert.equal(input().getAttribute('aria-expanded'), 'false')
  assert.equal(document.querySelector('[role="option"]'), null)
})

test('leaving the address search cancels pending selection before a late response', async () => {
  const selected: AddressSearchResult[] = []
  await act(async () => root.render(createElement(AddressSearch, { onSelect: (result) => selected.push(result) })))
  await typeQuery('Jana Zamoyskiego 47')
  await submit()
  await act(async () => input().blur())
  await respond(0, searchPayload('Jana Zamoyskiego 47', address(58109, 'Jana Zamoyskiego')))
  assert.deepEqual(selected, [])
  assert.equal(input().getAttribute('aria-expanded'), 'false')
  assert.equal(document.querySelector('[role="option"]'), null)
})

test('tapping an address suggestion selects it without submitting another search', async () => {
  const selected: AddressSearchResult[] = []
  await act(async () => root.render(createElement(AddressSearch, { onSelect: (result) => selected.push(result) })))
  await typeQuery('Jana Zamoyskiego')
  await act(async () => { await delay(300) })
  const result = address(58109, 'Jana Zamoyskiego')
  await respond(0, searchPayload('Jana Zamoyskiego', result))
  const suggestion = document.querySelector<HTMLElement>('[role="option"]')!
  assert.ok(suggestion)
  await act(async () => {
    suggestion.dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true, cancelable: true }))
  })
  assert.deepEqual(selected, [result])
  assert.equal(requests.length, 1)
  assert.equal(input().getAttribute('aria-expanded'), 'false')
  assert.notEqual(document.activeElement, input())
})

test('keyboard selection scrolls only the suggestion list and chooses the highlighted address', async () => {
  const selected: AddressSearchResult[] = []
  await act(async () => root.render(createElement(AddressSearch, { onSelect: (result) => selected.push(result) })))
  await typeQuery('Warszawa')
  await submit()
  const results = [address(58109, 'Jana Zamoyskiego'), address(109106, 'Stanisława Kazury')]
  await respond(0, { query: 'Warszawa', status: 'ready', results, best_match_id: null, source: null })
  const popup = document.querySelector<HTMLElement>('.address-popup')!
  const options = document.querySelectorAll<HTMLElement>('[role="option"]')
  Object.defineProperty(popup, 'clientHeight', { configurable: true, value: 100 })
  for (let index = 0; index < options.length; index += 1) {
    Object.defineProperty(options[index], 'offsetTop', { configurable: true, value: index * 80 })
    Object.defineProperty(options[index], 'offsetHeight', { configurable: true, value: 80 })
  }
  await key('ArrowDown')
  await key('ArrowDown')
  assert.equal(popup.scrollTop, 60)
  assert.equal(document.documentElement.scrollTop, 0)
  assert.equal(document.querySelector('main')!.scrollTop, 0)
  assert.equal(input().getAttribute('aria-activedescendant'), options[1].id)
  await submit()
  assert.deepEqual(selected, [results[1]])
  assert.equal(requests.length, 1)
  await act(async () => input().focus())
  assert.equal(input().getAttribute('aria-expanded'), 'false')
})

test('radius updates preserve existing information and its original distance until the response arrives', async () => {
  await act(async () => root.render(createElement(BuildingProfile, { buildingId: 58109 })))
  await respond(profileRequest(58109, 500), profile(58109, 500))
  const population = document.querySelector('.profile-population')!
  const census = document.querySelector('.profile-census-square')!
  await chooseRadius(300)
  assert.equal(document.querySelector('.profile-population'), population)
  assert.match(population.textContent!, /Residents within 500 m/)
  assert.match(document.querySelector('.profile-distance-note')!.textContent!, /Showing 500 m results while updating/)
  assert.equal(document.querySelector('.profile-census-square'), census)
  assert.match(census.textContent!, /1,985/)
  await respond(profileRequest(58109, 300), profile(58109, 300))
  assert.match(population.textContent!, /Residents within 300 m/)
  assert.doesNotMatch(document.querySelector('.profile-distance-note')!.textContent!, /Showing 500/)
  assert.match(census.textContent!, /1,985/)
})

test('a failed radius refresh preserves previous facts without relabeling their distance', async () => {
  await act(async () => root.render(createElement(BuildingProfile, { buildingId: 58109 })))
  await respond(profileRequest(58109, 500), profile(58109, 500))
  await chooseRadius(1000)
  await act(async () => requests[profileRequest(58109, 1000)].reject(new Error('Source unavailable')))
  assert.match(document.querySelector('.profile-population')!.textContent!, /Residents within 500 m/)
  assert.match(document.querySelector('.profile-distance-note')!.textContent!, /Showing 500 m results/)
  assert.ok(document.querySelector('.profile-error'))
  assert.equal(document.querySelector('.profile-updating'), null)
})

test('switching buildings hides old facts and ignores an old building response', async () => {
  await act(async () => root.render(createElement(BuildingProfile, { buildingId: 58109 })))
  await respond(profileRequest(58109, 500), profile(58109, 500))
  await chooseRadius(300)
  const oldRequest = profileRequest(58109, 300)
  await act(async () => root.render(createElement(BuildingProfile, { buildingId: 109106 })))
  assert.equal(requests[oldRequest].signal?.aborted, true)
  assert.equal(document.querySelector('.profile-population'), null)
  await respond(oldRequest, profile(58109, 300))
  assert.equal(document.querySelector('.profile-population'), null)
  await respond(profileRequest(109106, 300), profile(109106, 300))
  assert.match(document.querySelector('.profile-population')!.textContent!, /Residents within 300 m/)
  assert.match(document.querySelector('.profile-category-list summary')!.textContent!, /Unknown/)
  assert.doesNotMatch(document.querySelector('.profile-category-list summary')!.textContent!, /\b0\b/)
})

test('mobile address selection keeps the map selected until the user opens details', async () => {
  await loadApp()
  await selectDemoAddress()
  const viewButtons = document.querySelectorAll('.mobile-view-switch button')
  assert.equal(viewButtons[0].getAttribute('aria-pressed'), 'true')
  assert.equal(viewButtons[1].getAttribute('aria-pressed'), 'false')
  assert.match(document.querySelector('.mobile-selection-preview')!.textContent!, /Jana Zamoyskiego/)
  assert.equal(document.querySelector('[aria-label="Map selected building"]')!.textContent, '58109')
  const map = document.querySelector('[aria-label="Map interaction test double"]')
  await clickButton('View details →')
  assert.equal(viewButtons[0].getAttribute('aria-pressed'), 'false')
  assert.equal(viewButtons[1].getAttribute('aria-pressed'), 'true')
  assert.equal(document.querySelector('.mobile-selection-preview'), null)
  assert.equal(document.querySelector('[aria-label="Map interaction test double"]'), map)
  assert.match(document.querySelector('.building-details')!.textContent!, /Jana Zamoyskiego/)
  assert.match(document.querySelector('.grocery-model-note')!.textContent!, /recommendation is not available/)
  assert.match(document.querySelector('.profile-premises')!.textContent!, /Unknown/)
  assert.doesNotMatch(document.querySelector('.grocery-assessment')!.textContent!, /test-internal-model|suitability score/i)
  assert.ok(requests.every((request) => !/train|predict|ranking/.test(request.url)))
})

test('selecting another building resets panel scrolling and starts its own neighborhood request', async () => {
  await loadApp()
  await selectDemoAddress()
  await clickButton('View details →')
  const panel = document.querySelector<HTMLElement>('.building-panel')!
  panel.scrollTop = 650
  await chooseRadius(300)
  await clickButton('Map')
  assert.equal(panel.scrollTop, 650)
  await clickButton('Select second building on map')
  assert.equal(panel.scrollTop, 0)
  assert.equal(document.querySelector('[aria-label="Map selected building"]')!.textContent, '109106')
  assert.equal(document.querySelector('.building-details'), null)
  assert.equal(document.querySelector('.profile-population'), null)
  assert.equal(document.querySelectorAll('.mobile-view-switch button')[0].getAttribute('aria-pressed'), 'true')
  assert.ok(requests.some((request) => request.url === '/api/v1/map/buildings/109106/profile?radius_m=500'))
  await respondAt('/api/v1/map/buildings/109106', building(109106, 'Stanisława Kazury'))
  await respond(profileRequest(109106, 500), profile(109106, 500))
  await clickButton('View details →')
  assert.equal(panel.scrollTop, 0)
  assert.match(document.querySelector('.building-details')!.textContent!, /Stanisława Kazury/)
  await act(async () => document.querySelector<HTMLButtonElement>('[aria-label="Clear selection"]')!.click())
  assert.equal(document.querySelector('.mobile-selection-preview'), null)
  assert.equal(document.querySelector('[aria-label="Map selected building"]')!.textContent, 'None')
})

test('map options close on Escape with focus restored and dismiss on outside interaction', async () => {
  await loadApp()
  const menu = document.querySelector<HTMLDetailsElement>('.brand-menu')!
  const summary = menu.querySelector<HTMLElement>('summary')!
  await act(async () => summary.click())
  assert.equal(menu.open, true)
  const action = Array.from(menu.querySelectorAll('button')).find((button) => button.textContent === 'Hide all')!
  await act(async () => action.focus())
  assert.equal(document.activeElement, action)
  await act(async () => {
    action.dispatchEvent(new dom.window.KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
  })
  assert.equal(menu.open, false)
  assert.equal(document.activeElement, summary)
  await act(async () => summary.click())
  assert.equal(menu.open, true)
  await act(async () => {
    document.querySelector('.map-column')!.dispatchEvent(new dom.window.Event('pointerdown', { bubbles: true }))
  })
  assert.equal(menu.open, false)
  assert.equal(document.querySelectorAll('.grocery-brand-filters input:checked').length, 2)
})
