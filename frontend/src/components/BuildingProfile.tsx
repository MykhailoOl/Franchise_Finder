import { useEffect, useId, useState } from 'react'
import { sourceLink } from '../lib/api'
import { getBuildingProfile, type BuildingProfileData, type NearbyVenue, type ProfileRadius, type ProfileSource } from '../lib/buildingProfile'
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
  const sources = [profile.population.source, ...profile.competition.sources, profile.access.source].filter((source): source is ProfileSource => source !== null)
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
      if (!disposed) setState({ requestKey, status: 'error', data: null, error: controller.signal.aborted ? 'The building details took too long to load. Please try again.' : 'Nearby information is unavailable. Please try again.' })
    }).finally(() => window.clearTimeout(timeout))
    return () => { disposed = true; controller.abort(); window.clearTimeout(timeout) }
  }, [buildingId, radius, requestKey])

  const current = state.requestKey === requestKey ? state : null
  const profile = current?.data ?? null
  const loading = !current || current.status === 'loading'
  const population = profile?.population
  const venues = profile?.competition
  const access = profile?.access

  return (
    <section className="building-profile" aria-labelledby={titleId}>
      <div className="profile-heading"><h3 id={titleId}>Around this building</h3></div>
      <fieldset className="profile-radius"><legend>Explore within</legend><div>{radii.map((value) => <label key={value} className={radius === value ? 'profile-radius-selected' : ''}><input type="radio" name={groupId} value={value} checked={radius === value} onChange={() => setRadius(value)} /><span>{countFormat.format(value)} m</span></label>)}</div></fieldset>
      <p className="profile-distance-note">Straight-line distances from the building.</p>
      <div aria-live="polite" aria-busy={loading}>
        {loading && <div className="profile-loading"><span className="loading-ring" /><p>Loading nearby information…</p></div>}
        {current?.status === 'error' && <div className="profile-error"><p>{current.error}</p><button type="button" className="text-button" onClick={() => setRevision((value) => value + 1)}>Try again</button></div>}
        {profile && population && venues && access && <>
          <div className={`profile-population ${population.status === 'unavailable' ? 'profile-unavailable' : ''}`}>
            <div className="profile-metric-heading"><h4>Residents nearby</h4><span>{population.status === 'unavailable' ? 'Unavailable' : population.status === 'partial' ? 'Partial estimate' : 'Estimate'}</span></div>
            <strong>{population.estimated_residents === null ? 'Unknown' : <><span aria-label="approximately">≈ </span>{countFormat.format(population.estimated_residents)}</>}</strong>
            <p>{population.observation_year ? `${population.observation_year} census` : 'Census date unavailable'}</p>
            {population.status === 'partial' ? <span className="profile-data-note">{population.coverage_fraction !== null ? `Published census data covers ${(population.coverage_fraction * 100).toFixed(1)}% of this area. ` : ''}The population in the remaining area is unknown; this is not a total.</span> : population.estimated_residents === null ? <small>No population estimate is available here.</small> : <small>Estimated population in the surrounding area.</small>}
          </div>
          <section className="profile-businesses" aria-label="Nearby businesses by category"><div className="profile-section-heading"><h4>Nearby businesses</h4>{venues.status !== 'ready' && <span>{venues.status === 'partial' ? 'Partial coverage' : 'Unavailable'}</span>}</div><p className="profile-caption">Recorded places across all brands. Excludes this building; coverage may be incomplete.</p><div className="profile-category-list">{venues.categories.map((category) => <details key={category.id}><summary><span>{category.name}</span><strong>{category.count === null ? 'Unknown' : countFormat.format(category.count)}</strong></summary><div>{category.nearest?.length ? <>{category.nearest.map((venue, index) => <Venue key={`${venue.source_url}:${index}`} venue={venue} />)}</> : <p className="profile-empty">{category.count === 0 ? 'No places recorded within this distance.' : 'Nearby place details are unavailable.'}</p>}</div></details>)}</div></section>
          <section className="profile-access" aria-label="Nearby transport and parking"><div className="profile-section-heading"><h4>Transport & parking</h4></div><p className="profile-caption">Closest recorded locations. Distances are not walking routes.</p>{access.status === 'unavailable' ? <p className="profile-empty">Transport and parking information is unavailable.</p> : <div className="profile-access-list">{access.items.map((item) => <details key={item.id}><summary><span>{item.name}</span><strong>{item.nearest ? distanceLabel(item.nearest.distance_m) : item.count_within_radius === null ? 'Unknown' : 'None recorded'}</strong></summary><div>{item.nearest ? <Venue venue={item.nearest} /> : <p className="profile-empty">None recorded within {countFormat.format(radius)} m. Coverage may be incomplete.</p>}</div></details>)}</div>}</section>
          <details className="profile-premises"><summary><span>Before choosing a unit</span><strong>To verify</strong></summary><p>Unit availability and these premises details are not confirmed. Check them with the owner.</p><ul>{profile.premises.unknown_fields.map((field) => <li key={field.id}><span>{premisesLabels[field.id] ?? field.label}</span><strong>Unknown</strong></li>)}</ul></details>
          <ProfileSources profile={profile} />
        </>}
      </div>
    </section>
  )
}
