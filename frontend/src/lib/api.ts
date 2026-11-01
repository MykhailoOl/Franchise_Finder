import type { Feature, FeatureCollection, MultiPolygon, Point } from 'geojson'

export type SourceInfo = {
  name: string
  url: string
  attribution: string
  updated_at: string | null
}

export type MapMetadata = {
  status: 'empty' | 'ready'
  bounds: [number, number, number, number]
  minzoom: number
  maxzoom: number
  feature_count: number
  last_imported_at: string | null
  source: SourceInfo | null
}

export type BuildingProperties = {
  source_id: string
  source: SourceInfo
  building_type: string | null
  building_type_label?: string | null
  address: string | null
  addresses?: AssociatedAddress[]
  franchises?: BuildingOutlet[]
  area_m2: number
  imported_at: string
  attributes: Record<string, unknown>
}

export type AddressSearchResult = {
  id: number
  address: string
  street: string
  house_number: string
  postal_code: string | null
  coordinates: [number, number]
  building_id: number | null
  match_status: 'matched' | 'unmatched' | 'ambiguous'
  match_method: 'point_inside_building' | 'unique_nearby_building' | null
  distance_m: number | null
}

export type AssociatedAddress = {
  id: number
  address: string
  street: string
  house_number: string
  postal_code: string | null
  source_id: string
  source: SourceInfo
  match_method: 'point_inside_building' | 'unique_nearby_building'
  distance_m: number | null
  is_inferred: boolean
}

export type AddressSearchResponse = {
  query: string
  results: AddressSearchResult[]
  source: SourceInfo | null
  status: 'ready' | 'empty'
  best_match_id?: number | null
  exact_match_count?: number
}

export type AddressFocusRequest = { result: AddressSearchResult }

export type BuildingFeature = Feature<MultiPolygon, BuildingProperties> & { id: number }

export type FranchiseBrand = {
  id: string
  name: string
  category_id: string
  website: string
  source_url: string
  retrieved_at: string
  coverage_status: 'complete' | 'partial' | 'unavailable'
  coverage_note: string
  tile_property: string
  outlet_count: number
  mapped_count: number
  matched_count: number
}

export type FranchiseCatalog = {
  status: 'ready' | 'empty'
  categories: { id: string; name: string }[]
  brands: FranchiseBrand[]
  summary: { total_outlets: number; mapped_outlets: number; matched_outlets: number }
}

export type OutletProperties = {
  brand_id: string
  brand_name: string
  category_id: string
  name: string | null
  address: string
  building_id: number | null
  match_status: 'matched' | 'unmatched' | 'ambiguous' | 'conflict'
  match_method: string | null
  match_note?: string
  match_distance_m?: number | null
  candidate_building_ids: (number | string)[]
  point_source: string | null
  source_url: string
  retrieved_at: string
  status: 'listed' | 'open' | 'temporarily_closed' | 'unknown'
  operating_model: string
}

export type OutletFeature = Feature<Point | null, OutletProperties> & { id: number }
export type MappedOutletFeature = Feature<Point, OutletProperties> & { id: number }
export type OutletCollection = Omit<FeatureCollection<Point | null, OutletProperties>, 'features'> & { features: OutletFeature[] }
export type BuildingOutlet = OutletProperties & { id: number }
export type OutletFocusRequest = { outlet: OutletFeature }

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null
}

function isSource(value: unknown): value is SourceInfo {
  return isRecord(value) && typeof value.name === 'string' && typeof value.url === 'string'
    && typeof value.attribution === 'string'
    && (value.updated_at === null || typeof value.updated_at === 'string')
}

async function getJson(path: string, signal: AbortSignal, kind: 'building' | 'address' | 'franchise' = 'building'): Promise<unknown> {
  const response = await fetch(path, {
    signal,
    headers: { Accept: 'application/json' },
    cache: 'no-store',
  })

  if (!response.ok) {
    if (kind === 'franchise') throw new Error('Franchise locations are unavailable. Please try again.')
    if (kind === 'address') throw new Error('Address search is unavailable. Please try again.')
    if (response.status === 404) throw new Error('This building is no longer in the current dataset. Select another footprint.')
    if (response.status === 503) throw new Error('Building data is temporarily unavailable. Please try again.')
    throw new Error('We could not load the building data. Please try again.')
  }

  return response.json()
}

export async function getFranchiseCatalog(signal: AbortSignal): Promise<FranchiseCatalog> {
  const value = await getJson('/api/v1/map/franchises', signal, 'franchise')
  if (!isRecord(value) || !['ready', 'empty'].includes(String(value.status))
    || !Array.isArray(value.categories) || !Array.isArray(value.brands) || !isRecord(value.summary)
    || !value.categories.every((category) => isRecord(category) && typeof category.id === 'string' && typeof category.name === 'string')
    || !value.brands.every((brand) => isRecord(brand) && typeof brand.id === 'string'
      && typeof brand.name === 'string' && typeof brand.category_id === 'string'
      && typeof brand.tile_property === 'string' && typeof brand.source_url === 'string'
      && typeof brand.coverage_note === 'string' && typeof brand.retrieved_at === 'string'
      && ['complete', 'partial', 'unavailable'].includes(String(brand.coverage_status))
      && Number.isSafeInteger(brand.outlet_count) && Number.isSafeInteger(brand.mapped_count)
      && Number.isSafeInteger(brand.matched_count))) {
    throw new Error('Franchise source information could not be read. Please try again.')
  }
  return value as FranchiseCatalog
}

export async function getOutlets(signal: AbortSignal): Promise<OutletCollection> {
  const value = await getJson('/api/v1/map/outlets', signal, 'franchise')
  if (!isRecord(value) || value.type !== 'FeatureCollection' || !Array.isArray(value.features)
    || !value.features.every((feature) => isRecord(feature) && feature.type === 'Feature'
      && Number.isSafeInteger(feature.id) && isRecord(feature.properties)
      && typeof feature.properties.brand_id === 'string' && typeof feature.properties.brand_name === 'string'
      && typeof feature.properties.address === 'string' && typeof feature.properties.source_url === 'string'
      && (feature.properties.building_id === null || Number.isSafeInteger(feature.properties.building_id))
      && ['matched', 'unmatched', 'ambiguous', 'conflict'].includes(String(feature.properties.match_status))
      && (feature.geometry === null || (isRecord(feature.geometry) && feature.geometry.type === 'Point'
        && Array.isArray(feature.geometry.coordinates) && feature.geometry.coordinates.length === 2
        && feature.geometry.coordinates.every(Number.isFinite))))) {
    throw new Error('Franchise locations could not be read. Please try again.')
  }
  return value as unknown as OutletCollection
}

export async function searchAddresses(query: string, signal: AbortSignal): Promise<AddressSearchResponse> {
  const parameters = new URLSearchParams({ q: query, limit: '8' })
  const value = await getJson(`/api/v1/map/addresses/search?${parameters}`, signal, 'address')

  if (!isRecord(value) || typeof value.query !== 'string'
    || !['ready', 'empty'].includes(String(value.status))
    || !(value.source === null || isSource(value.source))
    || !Array.isArray(value.results)
    || !value.results.every((result) => isRecord(result)
      && Number.isSafeInteger(result.id) && typeof result.address === 'string'
      && typeof result.street === 'string' && typeof result.house_number === 'string'
      && Array.isArray(result.coordinates) && result.coordinates.length === 2 && result.coordinates.every(Number.isFinite)
      && ['matched', 'unmatched', 'ambiguous'].includes(String(result.match_status))
      && (result.building_id === null || Number.isSafeInteger(result.building_id))
      && (result.match_status !== 'matched' || Number.isSafeInteger(result.building_id)))) {
    throw new Error('The address search response could not be read. Please try again.')
  }

  return value as AddressSearchResponse
}

export async function getMapMetadata(signal: AbortSignal): Promise<MapMetadata> {
  const value = await getJson('/api/v1/map/metadata', signal)

  if (!isRecord(value) || !['empty', 'ready'].includes(String(value.status))
    || !Array.isArray(value.bounds) || value.bounds.length !== 4 || !value.bounds.every(Number.isFinite)
    || typeof value.minzoom !== 'number' || typeof value.maxzoom !== 'number'
    || typeof value.feature_count !== 'number'
    || !(value.source === null || isSource(value.source))) {
    throw new Error('Building data information could not be read. Please try again.')
  }

  return value as MapMetadata
}

export async function getBuilding(id: number, signal: AbortSignal): Promise<BuildingFeature> {
  const value = await getJson(`/api/v1/map/buildings/${id}`, signal)

  if (!isRecord(value) || value.type !== 'Feature' || value.id !== id
    || !isRecord(value.geometry) || value.geometry.type !== 'MultiPolygon'
    || !Array.isArray(value.geometry.coordinates) || !isRecord(value.properties)
    || !isSource(value.properties.source) || typeof value.properties.source_id !== 'string'
    || typeof value.properties.area_m2 !== 'number' || !Number.isFinite(value.properties.area_m2)
    || !isRecord(value.properties.attributes)) {
    throw new Error('The selected building details could not be read. Please try again.')
  }

  return value as unknown as BuildingFeature
}

export function sourceLink(value: string): string | undefined {
  try {
    const url = new URL(value)
    return ['https:', 'http:'].includes(url.protocol) ? url.href : undefined
  } catch {
    return undefined
  }
}
