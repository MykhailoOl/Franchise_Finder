import { useEffect, useId, useState } from 'react'
import { sourceLink } from '../lib/api'
import { getBuildingProfile, type BuildingProfileData, type NearbyVenue, type ProfileRadius, type ProfileSource } from '../lib/buildingProfile'
import { GroceryAssessment } from './GroceryAssessment'
import './BuildingProfile.css'

type ProfileState = { requestKey: string; status: 'loading' | 'ready' | 'error'; data: BuildingProfileData | null; error: string | null }
const radii: ProfileRadius[] = [300, 500, 1000]
const countFormat = new Intl.NumberFormat('en-GB', { maximumFractionDigits: 0 })
const dateFormat = new Intl.DateTimeFormat('en-GB', { day: 'numeric', month: 'short', year: 'numeric' })

function sourceDate(value: string | null | undefined) {
  if (!value) return 'Not supplied'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : dateFormat.format(date)
}

function distanceLabel(value: number) {
  return value < 1000 ? `${Math.round(value)} m` : `${(value / 1000).toFixed(1)} km`
}

const premisesLabels: Record<string, string> = {
  usable_unit_area: 'Unit size', rent: 'Rent and lease terms', availability: 'Availability',
  entrance: 'Entrance, frontage and step-free access', utilities: 'Utilities and ventilation',
  permitted_use: 'Permitted use',
}

function Venue({ venue }: { venue: NearbyVenue }) {
  const url = sourceLink(venue.source_url)
  return <div className="profile-venue"><div><span>{venue.name || 'Unnamed place'}</span><strong>{distanceLabel(venue.distance_m)}</strong></div>{venue.address && <p>{venue.address}</p>}{url && <a href={url} target="_blank" rel="noreferrer">View source ↗</a>}</div>
}

function ProfileSources({ profile }: { profile: BuildingProfileData }) {
  const sources = [profile.population.source, profile.population_context?.source, ...profile.competition.sources, profile.access.source].filter((source): source is ProfileSource => source != null)
  const uniqueSources = [...new Map(sources.map((source) => [source.id, source])).values()]
  return <details className="profile-sources"><summary>Sources & dates</summary><div><p>Population comes from census records. Business and transport information may be incomplete or have changed since collection.</p>{uniqueSources.map((source) => <article key={source.id}><h5>{sourceLink(source.url) ? <a href={sourceLink(source.url)} target="_blank" rel="noreferrer">{source.name} ↗</a> : source.name}</h5><p>{source.attribution}{source.license && <> · {source.license}</>}</p><dl>{source.observed_at && <div><dt>Observed</dt><dd>{sourceDate(source.observed_at)}</dd></div>}{source.snapshot_at && <div><dt>Map snapshot</dt><dd>{sourceDate(source.snapshot_at)}</dd></div>}<div><dt>Collected</dt><dd>{sourceDate(source.retrieved_at)}</dd></div></dl></article>)}{!uniqueSources.length && <p>Source information is unavailable.</p>}</div></details>
}

export function BuildingProfile({ buildingId }: { buildingId: number }) {
  const groupId = useId()
  const titleId = useId()
  const [radius, setRadius] = useState<ProfileRadius>(500)
  const [revision, setRevision] = useState(0)
  const [state, setState] = useState<ProfileState>({ requestKey: '', status: 'loading', data: null, error: null })
  const requestKey = `${buildingId}:${radius}:${revision}`

  useEffect(() => {
    const controller = new AbortController()
    let disposed = false
    const timeout = window.setTimeout(() => controller.abort(), 20000)
    getBuildingProfile(buildingId, radius, controller.signal).then((data) => {
      if (!disposed) setState({ requestKey, status: 'ready', data, error: null })
    }).catch(() => {
      if (!disposed) setState((previous) => ({ requestKey, status: 'error', data: previous.data?.building_id === buildingId ? previous.data : null, error: controller.signal.aborted ? 'The building details took too long to load. Please try again.' : 'Nearby information is unavailable. Please try again.' }))
    }).finally(() => window.clearTimeout(timeout))
    return () => { disposed = true; controller.abort(); window.clearTimeout(timeout) }
  }, [buildingId, radius, requestKey])

  const current = state.requestKey === requestKey ? state : null
  const profile = state.data?.building_id === buildingId ? state.data : null
  const loading = !current || current.status === 'loading'
  const displayedRadius = profile?.radius_m ?? radius
  const population = profile?.population
  const venues = profile?.competition
  const access = profile?.access
  const censusSquare = profile?.population_context
  const groceryCategories = venues?.categories.filter((category) => category.id === 'grocery') ?? []
  const otherCategories = venues?.categories.filter((category) => category.id !== 'grocery') ?? []

  return (
    <>
    <section className="building-profile" aria-labelledby={titleId}>
      <div className="profile-heading"><h3 id={titleId}>Grocery location overview</h3><p>Explore the neighborhood. Recommendations are not available yet.</p></div>
      <fieldset className="profile-radius"><legend>Search nearby</legend><div>{radii.map((value) => <label key={value} className={radius === value ? 'profile-radius-selected' : ''}><input type="radio" name={groupId} value={value} checked={radius === value} onChange={() => setRadius(value)} /><span>{countFormat.format(value)} m</span></label>)}</div></fieldset>
      <p className="profile-distance-note">Straight-line distances from the building.{profile && displayedRadius !== radius && <> Showing {countFormat.format(displayedRadius)} m results{loading ? ' while updating' : ''}.</>}</p>
      <div aria-live="polite" aria-busy={loading}>
        {loading && <div className={profile ? 'profile-updating' : 'profile-loading'}>{!profile && <span className="loading-ring" />}<p>{profile ? 'Updating nearby information…' : 'Loading nearby information…'}</p></div>}
        {current?.status === 'error' && <div className="profile-error"><p>{current.error}</p><button type="button" className="text-button" onClick={() => setRevision((value) => value + 1)}>Try again</button></div>}
        {profile && population && venues && access && <>
          <div className={`profile-population ${population.status === 'unavailable' ? 'profile-unavailable' : ''}`}>
            <div className="profile-metric-heading"><h4>Residents within {countFormat.format(displayedRadius)} m</h4><span>{population.status === 'unavailable' ? 'Unavailable' : population.status === 'partial' ? 'Partial data' : 'Estimate'}</span></div>
            <strong>{population.estimated_residents === null ? 'Unknown' : <><span aria-label="approximately">≈ </span>{countFormat.format(population.estimated_residents)}</>}</strong>
            <p>{population.observation_year ? `${population.observation_year} census` : 'Census date unavailable'}</p>
            {population.status === 'partial' ? <span className="profile-data-note">{population.coverage_fraction !== null ? `Census data covers ${(population.coverage_fraction * 100).toFixed(1)}% of this area. ` : ''}Missing areas are not counted; this is not a total.</span> : population.estimated_residents === null ? <small>No population estimate is available here.</small> : <small>Estimated population in the surrounding area.</small>}
          </div>
          {censusSquare && <details className="profile-census-square"><summary><span>Residents in this census square<small>2021 · 500 × 500 m square</small></span><strong>{censusSquare.population === null ? 'Unknown' : countFormat.format(censusSquare.population)}</strong></summary><div><p>The square containing this building, separate from the {countFormat.format(displayedRadius)} m neighborhood above. These figures are not added together.</p><span>{censusSquare.source?.attribution || 'Source unavailable'}</span></div></details>}
          <section className="profile-businesses" aria-label="Nearby grocery stores"><div className="profile-section-heading"><h4>Nearby grocery stores</h4>{venues.status !== 'ready' && <span>{venues.status === 'partial' ? 'Partial coverage' : 'Unavailable'}</span>}</div><p className="profile-caption">All brands within {countFormat.format(displayedRadius)} m, excluding this building. Coverage may be incomplete.</p>{groceryCategories.length ? <div className="profile-category-list">{groceryCategories.map((category) => <details key={category.id}><summary><span>Grocery stores</span><strong>{category.count === null ? 'Unknown' : countFormat.format(category.count)}</strong></summary><div>{category.nearest?.length ? <>{category.nearest.map((venue, index) => <Venue key={`${venue.source_url}:${index}`} venue={venue} />)}</> : <p className="profile-empty">{category.count === 0 ? 'No places recorded within this distance.' : 'Nearby place details are unavailable.'}</p>}</div></details>)}</div> : <p className="profile-empty">Grocery store information is unavailable.</p>}</section>
          <section className="profile-access" aria-label="Nearby transport and parking"><div className="profile-section-heading"><h4>Transport & parking</h4></div><p className="profile-caption">Nearest recorded locations within {countFormat.format(displayedRadius)} m. Distances are not walking routes.</p>{access.status === 'unavailable' ? <p className="profile-empty">Transport and parking information is unavailable.</p> : <div className="profile-access-list">{access.items.map((item) => <details key={item.id}><summary><span>{item.name}</span><strong>{item.nearest ? distanceLabel(item.nearest.distance_m) : item.count_within_radius === null ? 'Unknown' : 'None recorded'}</strong></summary><div>{item.nearest ? <Venue venue={item.nearest} /> : <p className="profile-empty">{item.count_within_radius === null ? 'Information is unavailable for this area.' : `None recorded within ${countFormat.format(displayedRadius)} m. Coverage may be incomplete.`}</p>}</div></details>)}</div>}</section>
          {otherCategories.length > 0 && <details className="profile-other-businesses"><summary>Other nearby businesses</summary><div className="profile-category-list">{otherCategories.map((category) => <details key={category.id}><summary><span>{category.name}</span><strong>{category.count === null ? 'Unknown' : countFormat.format(category.count)}</strong></summary><div>{category.nearest?.length ? category.nearest.map((venue, index) => <Venue key={`${venue.source_url}:${index}`} venue={venue} />) : <p className="profile-empty">{category.count === 0 ? 'No places recorded within this distance.' : 'Nearby place details are unavailable.'}</p>}</div></details>)}</div></details>}
          <details className="profile-premises"><summary><span>Premises details</span><strong>Unconfirmed</strong></summary><p>Availability, usable floor area and the details below need to be checked with the owner.</p><ul>{profile.premises.unknown_fields.map((field) => <li key={field.id}><span>{premisesLabels[field.id] ?? field.label}</span><strong>Unknown</strong></li>)}</ul></details>
          <ProfileSources profile={profile} />
        </>}
      </div>
    </section>
    <GroceryAssessment key={buildingId} buildingId={buildingId} />
    </>
  )
}
