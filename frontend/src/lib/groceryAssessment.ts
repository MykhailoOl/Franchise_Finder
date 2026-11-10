export type EvidenceStatus = 'available' | 'partial' | 'missing' | 'unavailable' | 'outside_coverage' | 'censored'

export type GroceryEvidence = {
  id: string
  label: string
  value: number | string | null
  unit: string
  status: EvidenceStatus
  reason: string | null
  radius_m: number | null
  coverage_fraction: number | null
  source_refs: string[]
  method: string
  limitation: string
}

export type PremisesRequirement = {
  id: string
  label: string
  strength: string
  operator: string
  assessment_status: 'unknown'
  assessment_reason: string
  source_url: string
}

export type GroceryBrand = {
  brand_id: 'zabka' | 'lewiatan'
  brand_name: string
  status: 'unknown'
  reviewed_at: string
  formats: { id: string; name: string; requirements: PremisesRequirement[] }[]
  sources: { id: string; url: string; retrieved_at: string | null; published_at: string | null }[]
  notes: string[]
}

export type AssessmentSource = {
  id: string
  name: string | null
  attribution: string | null
  url: string | null
  observed_at: string | null
  retrieved_at: string | null
  snapshot_at: string | null
  limitations: string[]
  available: boolean
}

export type GroceryAssessmentData = {
  building_id: number
  category: 'grocery'
  status: 'evidence_only'
  reason: string
  computed_at: string
  evidence: GroceryEvidence[]
  evidence_highlights: string[]
  requirements: GroceryBrand[]
  missing_details: { id: string; label: string; status: 'unknown' }[]
  sources: AssessmentSource[]
  ranking: { status: 'withheld'; score: null; rank: null; reason: string }
  model: {
    status: 'validation_withheld' | 'unavailable' | 'stale' | 'integrity_error' | 'review_required'
    reason: string
    version: string | null
    evaluated_at: string | null
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
}

function isNullableText(value: unknown): boolean {
  return value === null || typeof value === 'string'
}

function isNumber(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0
}

function isTextList(value: unknown): value is string[] {
  return Array.isArray(value) && value.every((item) => typeof item === 'string')
}

function isEvidence(value: unknown): boolean {
  if (!isRecord(value) || typeof value.id !== 'string' || typeof value.label !== 'string'
    || typeof value.unit !== 'string' || !['available', 'partial', 'missing', 'unavailable', 'outside_coverage', 'censored'].includes(String(value.status))
    || !isNullableText(value.reason) || !isTextList(value.source_refs)
    || !(value.radius_m === null || isNumber(value.radius_m))
    || !(value.coverage_fraction === null || (isNumber(value.coverage_fraction) && value.coverage_fraction <= 1))
    || typeof value.method !== 'string' || typeof value.limitation !== 'string') return false
  const measured = value.status === 'available' || value.status === 'partial'
  if (measured !== (value.value !== null)) return false
  if (value.value === null) return true
  if (value.unit === 'category') return typeof value.value === 'string' && value.value.length > 0
  return isNumber(value.value) && (value.unit !== 'fraction' || value.value <= 1)
    && (!['count', 'storeys'].includes(value.unit) || Number.isInteger(value.value))
}

function isRequirement(value: unknown): boolean {
  return isRecord(value) && typeof value.id === 'string' && typeof value.label === 'string'
    && typeof value.strength === 'string' && typeof value.operator === 'string'
    && value.assessment_status === 'unknown' && typeof value.assessment_reason === 'string'
    && typeof value.source_url === 'string'
}

function isBrand(value: unknown): boolean {
  return isRecord(value) && ['zabka', 'lewiatan'].includes(String(value.brand_id))
    && typeof value.brand_name === 'string' && value.status === 'unknown' && typeof value.reviewed_at === 'string'
    && Array.isArray(value.formats) && value.formats.every((format) => isRecord(format)
      && typeof format.id === 'string' && typeof format.name === 'string'
      && Array.isArray(format.requirements) && format.requirements.every(isRequirement))
    && Array.isArray(value.sources) && value.sources.every((source) => isRecord(source)
      && typeof source.id === 'string' && typeof source.url === 'string'
      && isNullableText(source.retrieved_at) && isNullableText(source.published_at))
    && isTextList(value.notes)
}

function isSource(value: unknown): boolean {
  return isRecord(value) && typeof value.id === 'string' && isNullableText(value.name)
    && isNullableText(value.attribution) && isNullableText(value.url)
    && isNullableText(value.observed_at) && isNullableText(value.retrieved_at) && isNullableText(value.snapshot_at)
    && isTextList(value.limitations) && typeof value.available === 'boolean'
}

export async function getGroceryAssessment(buildingId: number, signal: AbortSignal): Promise<GroceryAssessmentData> {
  const response = await fetch(`/api/v1/map/buildings/${buildingId}/grocery-assessment`, {
    signal, headers: { Accept: 'application/json' }, cache: 'no-store',
  })
  if (!response.ok) throw new Error('Grocery information is temporarily unavailable.')
  const value: unknown = await response.json()
  if (!isRecord(value) || value.building_id !== buildingId || value.category !== 'grocery'
    || value.status !== 'evidence_only' || typeof value.reason !== 'string' || typeof value.computed_at !== 'string'
    || !Array.isArray(value.evidence) || !value.evidence.every(isEvidence)
    || !isTextList(value.evidence_highlights)
    || !Array.isArray(value.requirements) || !value.requirements.every(isBrand)
    || !Array.isArray(value.missing_details) || !value.missing_details.every((item) => isRecord(item)
      && typeof item.id === 'string' && typeof item.label === 'string' && item.status === 'unknown')
    || !Array.isArray(value.sources) || !value.sources.every(isSource)
    || !isRecord(value.ranking) || value.ranking.status !== 'withheld' || value.ranking.score !== null
    || value.ranking.rank !== null || typeof value.ranking.reason !== 'string'
    || !isRecord(value.model) || !['validation_withheld', 'unavailable', 'stale', 'integrity_error', 'review_required'].includes(String(value.model.status))
    || typeof value.model.reason !== 'string' || !isNullableText(value.model.version) || !isNullableText(value.model.evaluated_at)
    || (value.score !== undefined && value.score !== null) || (value.rank !== undefined && value.rank !== null)
    || (value.model.score !== undefined && value.model.score !== null) || (value.model.rank !== undefined && value.model.rank !== null)) {
    throw new Error('The grocery information could not be read.')
  }
  const evidenceIds = new Set(value.evidence.map((item) => (item as GroceryEvidence).id))
  if (evidenceIds.size !== value.evidence.length || !value.evidence_highlights.every((id) => evidenceIds.has(id))) {
    throw new Error('The grocery information could not be read.')
  }
  return value as unknown as GroceryAssessmentData
}
