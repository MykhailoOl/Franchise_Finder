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

function locationLabel(value: string) {
  const labels: Record<string, string> = {
    official_locator: 'Official locator point', municipal_address: 'Municipal address point',
    building_representative_point: 'Building reference point', osm_point: 'Mapped point',
    osm_node: 'Mapped point', point_on_surface: 'Feature reference point',
    osm_way_center: 'Mapped feature centre', osm_relation_center: 'Mapped feature centre',
    node: 'Mapped point', way_center: 'Mapped feature centre', relation_center: 'Mapped feature centre',
  }
  return labels[value] ?? value.replaceAll('_', ' ')
}

function Venue({ venue }: { venue: NearbyVenue }) {
  const url = sourceLink(venue.source_url)
  return <div className="profile-venue"><div><span>{venue.name || 'Unnamed mapped place'}</span><strong>{distanceLabel(venue.distance_m)}</strong></div>{venue.address && <p>{venue.address}</p>}<small>{locationLabel(venue.location_method)}{url && <> · <a href={url} target="_blank" rel="noreferrer">Source ↗</a></>}</small>{venue.updated_at && <small>Map record edited {sourceDate(venue.updated_at)}; not a site observation.</small>}</div>
}

function ProfileSources({ profile }: { profile: BuildingProfileData }) {
  const sources = [profile.population.source, ...profile.competition.sources, profile.access.source].filter((source): source is ProfileSource => source !== null)
  const uniqueSources = [...new Map(sources.map((source) => [source.id, source])).values()]
  return <details className="profile-sources"><summary>Sources, dates & limitations</summary><div>{uniqueSources.map((source) => <article key={source.id}><h5>{sourceLink(source.url) ? <a href={sourceLink(source.url)} target="_blank" rel="noreferrer">{source.name} ↗</a> : source.name}</h5><p>{source.attribution}{source.license && <> · {source.license}</>}</p><dl><div><dt>Observation date</dt><dd>{sourceDate(source.observed_at)}</dd></div>{source.snapshot_at && <div><dt>Dataset snapshot</dt><dd>{sourceDate(source.snapshot_at)}</dd></div>}<div><dt>Retrieved</dt><dd>{sourceDate(source.retrieved_at)}</dd></div></dl>{Array.isArray(source.notes) ? source.notes.map((note, index) => <p key={index}>{note}</p>) : source.notes && <p>{source.notes}</p>}</article>)}{!uniqueSources.length && <p>No source metadata is available for these building details.</p>}{profile.quality_notes.length > 0 && <ul>{profile.quality_notes.map((note, index) => <li key={index}>{note}</li>)}</ul>}<p>Calculated {sourceDate(profile.computed_at)}. Retrieval and calculation dates do not change the age of the source observations.</p></div></details>
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
    }).catch((error: unknown) => {
      if (!disposed) setState({ requestKey, status: 'error', data: null, error: controller.signal.aborted ? 'The building details took too long to load. Please try again.' : error instanceof Error ? error.message : 'The building details are unavailable.' })
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
      <div className="profile-heading"><div><p>LOCAL CONTEXT</p><h3 id={titleId}>Building details</h3></div>{profile?.context_status === 'partial' && <span className="profile-partial">Partial data</span>}</div>
      <fieldset className="profile-radius"><legend>Distance from building reference point</legend><div>{radii.map((value) => <label key={value} className={radius === value ? 'profile-radius-selected' : ''}><input type="radio" name={groupId} value={value} checked={radius === value} onChange={() => setRadius(value)} /><span>{countFormat.format(value)} m</span></label>)}</div></fieldset>
      <p className="profile-distance-note">Circular area · straight-line distances</p>

      <div aria-live="polite" aria-busy={loading}>
        {loading && <div className="profile-loading"><span className="loading-ring" /><p>Loading context within {countFormat.format(radius)} m…</p></div>}
        {current?.status === 'error' && <div className="profile-error"><p>{current.error}</p><button type="button" className="text-button" onClick={() => setRevision((value) => value + 1)}>Retry details ↻</button></div>}
        {profile && population && venues && access && <>
          <div className={`profile-population ${population.status === 'unavailable' ? 'profile-unavailable' : ''}`}>
            <div className="profile-metric-heading"><h4>Residents in published cells</h4><span>{population.status === 'unavailable' ? 'Unavailable' : 'Known-cell estimate'}</span></div>
            <strong>{population.estimated_residents === null ? 'Unknown' : <><span aria-label="approximately">≈ </span>{countFormat.format(population.estimated_residents)}</>}</strong>
            <p>{population.observation_year ? `${population.observation_year} census` : 'Census year unavailable'}{population.resolution_m && <> · {population.resolution_m} m grid</>}</p>
            {population.estimated_residents !== null ? <small>Area-weighted estimate from published census cells. Residents are not counted at this building.</small> : <small>Population data is not available for this area. Missing data is not shown as zero.</small>}
            {population.coverage_fraction !== null && <small>Published-cell coverage: {(population.coverage_fraction * 100).toFixed(1)}% of this area.</small>}
            {population.status === 'partial' && <span className="profile-data-note">Missing areas are not assumed empty or extrapolated. This subtotal does not estimate the whole area.</span>}
          </div>

          <section className="profile-businesses" aria-label="Nearby businesses by category"><div className="profile-section-heading"><h4>Nearby businesses</h4><span>{venues.status === 'ready' ? 'Mapped records' : venues.status === 'partial' ? 'Partial coverage' : 'Unavailable'}</span></div><p className="profile-caption">Broad category context from the imported listings.{venues.onsite_outlet_count > 0 && <> Excludes {countFormat.format(venues.onsite_outlet_count)} recorded {venues.onsite_outlet_count === 1 ? 'outlet' : 'outlets'} linked to this building.</>}</p>{venues.status === 'unavailable' && <p className="profile-empty">Business data is unavailable for this area.</p>}<div className="profile-category-list">{venues.categories.map((category) => <details key={category.id}><summary><span>{category.name}</span><strong>{category.count === null ? 'Unknown' : countFormat.format(category.count)}</strong></summary><div><p className="profile-category-breakdown">Official listings: {category.official_count === null ? 'unavailable' : countFormat.format(category.official_count)} · OpenStreetMap: {category.osm_count === null ? 'unavailable' : countFormat.format(category.osm_count)}</p>{category.nearest?.length ? <><h5>Closest recorded places</h5>{category.nearest.map((venue, index) => <Venue key={`${venue.source_url}:${index}`} venue={venue} />)}</> : <p className="profile-empty">{category.count === 0 ? 'No nearby places are recorded in this category.' : 'Closest-place details are unavailable.'}</p>}</div></details>)}</div><p className="profile-caption">Counts describe source records, not a complete competitor census. Map brand filters do not change these counts.</p></section>

          <section className="profile-access" aria-label="Nearby access points"><div className="profile-section-heading"><h4>Access nearby</h4><span>Straight-line distance</span></div><p className="profile-caption">Closest mapped points within the selected radius. No walking route, service frequency, or step-free access is implied.</p>{access.status === 'unavailable' ? <p className="profile-empty">Transport and parking data is unavailable.</p> : <div className="profile-access-list">{access.items.map((item) => <details key={item.id}><summary><span>{item.name}</span><strong>{item.nearest ? distanceLabel(item.nearest.distance_m) : item.count_within_radius === null ? 'Unknown' : 'None recorded'}</strong></summary><div><p className="profile-category-breakdown">{item.count_within_radius === null ? 'Count unavailable' : `${countFormat.format(item.count_within_radius)} mapped ${item.count_within_radius === 1 ? 'feature' : 'features'} within ${countFormat.format(radius)} m`}</p>{item.nearest ? <Venue venue={item.nearest} /> : <p className="profile-empty">No feature of this type is recorded within this radius. Map coverage may be incomplete.</p>}</div></details>)}</div>}</section>

          <details className="profile-premises"><summary><span>Premises details to verify</span><strong>{profile.premises.unknown_fields.length} unknown</strong></summary><p>Public context does not confirm the condition or availability of an individual unit.</p><ul>{profile.premises.unknown_fields.map((field) => <li key={field.id}><span>{field.label}</span><strong>Unknown</strong></li>)}</ul></details>
          <ProfileSources profile={profile} />
        </>}
      </div>
    </section>
  )
}
