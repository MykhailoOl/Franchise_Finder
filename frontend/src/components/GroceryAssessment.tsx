import { useEffect, useId, useState } from 'react'
import { sourceLink } from '../lib/api'
import { getGroceryAssessment, type GroceryAssessmentData, type GroceryBrand, type GroceryEvidence } from '../lib/groceryAssessment'
import './GroceryAssessment.css'

type AssessmentState = { requestKey: string; status: 'ready' | 'error'; data: GroceryAssessmentData | null; error: string | null }

const activityFacts = [
  { id: 'school_count_500m', label: 'Schools' },
  { id: 'university_count_500m', label: 'Universities' },
  { id: 'office_count_500m', label: 'Offices' },
  { id: 'retail_destination_count_500m', label: 'Retail destinations' },
]
const countFormat = new Intl.NumberFormat('en-GB', { maximumFractionDigits: 0 })
const dateFormat = new Intl.DateTimeFormat('en-GB', { day: 'numeric', month: 'short', year: 'numeric' })

function dateLabel(value: string | null) {
  if (!value) return 'Date not supplied'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? 'Date not supplied' : dateFormat.format(date)
}

function ActivityFact({ label, evidence }: { label: string; evidence?: GroceryEvidence }) {
  const measured = evidence && (evidence.status === 'available' || evidence.status === 'partial') && typeof evidence.value === 'number'
  return <div className="grocery-fact"><dt>{label}</dt><dd><strong>{measured ? countFormat.format(evidence.value as number) : 'Unknown'}</strong><span>{measured ? evidence.status === 'partial' ? 'Partial coverage' : 'Recorded nearby' : 'Data unavailable'}</span></dd></div>
}

function BrandRequirements({ brand }: { brand: GroceryBrand }) {
  return <details className="grocery-brand"><summary><span>{brand.brand_name}</span><span className="grocery-unknown">To verify</span></summary><div><p>These are the brand’s published criteria. Whether this unit meets them is unconfirmed.</p>{brand.formats.map((format) => <div className="grocery-format" key={format.id}>{brand.formats.length > 1 && <h5>{format.name}</h5>}<ul>{format.requirements.map((requirement) => <li key={requirement.id}><span>{requirement.label}</span><small>{requirement.operator === 'not_published' ? 'Ask the brand' : requirement.strength === 'preferred' ? 'Preference · to verify' : requirement.strength === 'approval' ? 'Brand review needed' : 'Not confirmed'}</small></li>)}</ul></div>)}<p className="grocery-reviewed">Requirements reviewed {dateLabel(brand.reviewed_at)}. Confirm current terms with the brand.</p></div></details>
}

function AssessmentSources({ assessment }: { assessment: GroceryAssessmentData }) {
  const activitySourceIds = new Set(assessment.evidence.filter((item) => activityFacts.some((fact) => fact.id === item.id)).flatMap((item) => item.source_refs))
  const sources = assessment.sources.filter((source) => activitySourceIds.has(source.id))
  return <details className="grocery-sources"><summary>Sources & dates</summary><div>{sources.map((source) => {
    const url = source.url ? sourceLink(source.url) : undefined
    const name = source.name || 'Public map data'
    return <article key={source.id}><h5>{url ? <a href={url} target="_blank" rel="noreferrer">{name} ↗</a> : name}</h5>{source.attribution && <p>{source.attribution}</p>}<dl>{source.observed_at && <div><dt>Observed</dt><dd>{dateLabel(source.observed_at)}</dd></div>}{source.snapshot_at && <div><dt>Map snapshot</dt><dd>{dateLabel(source.snapshot_at)}</dd></div>}<div><dt>Collected</dt><dd>{dateLabel(source.retrieved_at)}</dd></div></dl>{!source.available && <p>Information from this source is currently unavailable.</p>}</article>
  })}{assessment.requirements.map((brand) => <article key={brand.brand_id}><h5>{brand.brand_name} requirements</h5><dl><div><dt>Reviewed</dt><dd>{dateLabel(brand.reviewed_at)}</dd></div></dl>{brand.sources.map((source, index) => {
    const url = sourceLink(source.url)
    return <div className="grocery-source-link" key={source.id}>{url && <a href={url} target="_blank" rel="noreferrer">Official information{brand.sources.length > 1 ? ` ${index + 1}` : ''} ↗</a>}<span>Collected {dateLabel(source.retrieved_at)}</span>{source.published_at && <span>Published {dateLabel(source.published_at)}</span>}</div>
  })}</article>)}</div></details>
}

export function GroceryAssessment({ buildingId }: { buildingId: number }) {
  const titleId = useId()
  const [revision, setRevision] = useState(0)
  const [state, setState] = useState<AssessmentState | null>(null)
  const requestKey = `${buildingId}:${revision}`

  useEffect(() => {
    const controller = new AbortController()
    let disposed = false
    const timeout = window.setTimeout(() => controller.abort(), 20000)
    getGroceryAssessment(buildingId, controller.signal).then((data) => {
      if (!disposed) setState({ requestKey, status: 'ready', data, error: null })
    }).catch(() => {
      if (!disposed) setState({ requestKey, status: 'error', data: null, error: controller.signal.aborted ? 'Grocery information took too long to load.' : 'Grocery information is temporarily unavailable.' })
    }).finally(() => window.clearTimeout(timeout))
    return () => { disposed = true; controller.abort(); window.clearTimeout(timeout) }
  }, [buildingId, requestKey])

  const current = state?.requestKey === requestKey ? state : null
  const assessment = current?.data ?? null
  const loading = current === null
  const announcement = loading ? 'Loading grocery information…' : current.status === 'error' ? current.error : 'Grocery information loaded. A franchise recommendation is not available.'

  return <section className="grocery-assessment" aria-labelledby={titleId}>
    <div className="grocery-heading"><h3 id={titleId}>Grocery assessment</h3></div>
    <p className={loading ? 'grocery-loading' : 'grocery-announcement'} role="status" aria-live="polite" aria-atomic="true">{announcement}</p>
    <div aria-busy={loading}>
      {current?.status === 'error' && <div className="grocery-error"><p>{current.error} You can still explore the neighborhood below.</p><button className="text-button" type="button" onClick={() => setRevision((value) => value + 1)}>Try grocery information again</button></div>}
      {assessment && <>
        <div className="grocery-availability"><h4>Recommendation not available</h4><p>{assessment.ranking.reason}</p></div>
        <div className="grocery-places-heading"><h4>Nearby places</h4><span>Within 500 m</span></div>
        <dl className="grocery-facts">{activityFacts.map((fact) => <ActivityFact key={fact.id} label={fact.label} evidence={assessment.evidence.find((item) => item.id === fact.id)} />)}</dl>
        <p className="grocery-caption">Mapped places outside this building, within a straight-line distance. These counts do not measure customer traffic.</p>
        <div className="grocery-requirements-heading"><h4>Brand requirements</h4><p>Unit availability, size and permitted use need to be checked with the owner and the brand.</p></div>
        <div className="grocery-brands">{assessment.requirements.map((brand) => <BrandRequirements key={brand.brand_id} brand={brand} />)}</div>
        <AssessmentSources assessment={assessment} />
      </>}
    </div>
  </section>
}
