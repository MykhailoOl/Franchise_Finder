export type ProfileRadius = 300 | 500 | 1000
export type ContextStatus = 'ready' | 'partial' | 'unavailable'

export type ProfileSource = {
  id: string
  name: string
  url: string
  attribution: string
  license: string | null
  retrieved_at: string | null
  observed_at: string | null
  snapshot_at?: string | null
  notes: string | string[] | null
}

export type NearbyVenue = {
  name: string | null
  distance_m: number
  source_url: string
  location_method: string
  address?: string | null
  updated_at?: string | null
}

export type BuildingProfileData = {
  building_id: number
  radius_m: ProfileRadius
  available_radii_m: ProfileRadius[]
  origin: { coordinates: [number, number]; method: string }
  computed_at: string
  context_status: 'ready' | 'partial'
  population: {
    status: ContextStatus
    estimated_residents: number | null
    coverage_fraction: number | null
    observation_year: number | null
    resolution_m: number | null
    method: 'area_weighted_grid'
    source: ProfileSource | null
  }
  competition: {
    status: ContextStatus
    categories: {
      id: string
      name: string
      count: number | null
      official_count: number | null
      osm_count: number | null
      nearest?: NearbyVenue[]
    }[]
    onsite_outlet_count: number
    deduplicated_count: number
    sources: ProfileSource[]
  }
  access: {
    status: 'ready' | 'unavailable'
    method: 'straight_line'
    nearest_search_scope?: 'selected_radius'
    items: {
      id: string
      name: string
      count_within_radius: number | null
      nearest: NearbyVenue | null
    }[]
    source: ProfileSource | null
  }
  premises: { status: 'unknown'; unknown_fields: { id: string; label: string }[] }
  quality_notes: string[]
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object'
}

function isCount(value: unknown): boolean {
  return value === null || (typeof value === 'number' && Number.isFinite(value) && value >= 0)
}

function isSource(value: unknown): boolean {
  return value === null || (isRecord(value) && typeof value.id === 'string' && typeof value.name === 'string' && typeof value.url === 'string')
}

function isVenue(value: unknown): boolean {
  return isRecord(value) && (value.name === null || typeof value.name === 'string')
    && typeof value.distance_m === 'number' && Number.isFinite(value.distance_m) && value.distance_m >= 0
    && typeof value.source_url === 'string' && typeof value.location_method === 'string'
}

export async function getBuildingProfile(buildingId: number, radius: ProfileRadius, signal: AbortSignal): Promise<BuildingProfileData> {
  const response = await fetch(`/api/v1/map/buildings/${buildingId}/profile?radius_m=${radius}`, {
    signal, headers: { Accept: 'application/json' }, cache: 'no-store',
  })
  if (!response.ok) {
    if (response.status === 404) throw new Error('Details are not available for this building. Select another footprint or retry.')
    throw new Error('The building details could not be loaded. Please try again.')
  }
  const value: unknown = await response.json()
  if (!isRecord(value) || value.building_id !== buildingId || value.radius_m !== radius
    || !Array.isArray(value.available_radii_m) || !value.available_radii_m.every((item) => [300, 500, 1000].includes(item))
    || !isRecord(value.origin) || !['ready', 'partial'].includes(String(value.context_status))
    || !isRecord(value.population) || !['ready', 'partial', 'unavailable'].includes(String(value.population.status))
    || !isCount(value.population.estimated_residents) || !isCount(value.population.coverage_fraction) || !isSource(value.population.source)
    || !isRecord(value.competition) || !['ready', 'partial', 'unavailable'].includes(String(value.competition.status))
    || !Array.isArray(value.competition.categories)
    || !value.competition.categories.every((category) => isRecord(category) && typeof category.id === 'string' && typeof category.name === 'string'
      && isCount(category.count) && isCount(category.official_count) && isCount(category.osm_count)
      && (category.nearest === undefined || (Array.isArray(category.nearest) && category.nearest.every(isVenue))))
    || !Array.isArray(value.competition.sources) || !value.competition.sources.every((source) => source !== null && isSource(source))
    || !isRecord(value.access) || !['ready', 'unavailable'].includes(String(value.access.status)) || !isSource(value.access.source)
    || !Array.isArray(value.access.items) || !value.access.items.every((item) => isRecord(item) && typeof item.id === 'string' && typeof item.name === 'string'
      && isCount(item.count_within_radius) && (item.nearest === null || isVenue(item.nearest)))
    || !isRecord(value.premises) || value.premises.status !== 'unknown' || !Array.isArray(value.premises.unknown_fields)
    || !value.premises.unknown_fields.every((item) => isRecord(item) && typeof item.id === 'string' && typeof item.label === 'string')
    || !Array.isArray(value.quality_notes) || !value.quality_notes.every((note) => typeof note === 'string')) {
    throw new Error('The building details response could not be read. Please try again.')
  }
  return value as unknown as BuildingProfileData
}
