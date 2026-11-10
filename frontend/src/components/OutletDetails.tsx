import { sourceLink, type BuildingOutlet, type OutletFeature } from '../lib/api'

export function OutletDetails({ outlet }: { outlet: OutletFeature }) {
  const properties = outlet.properties
  const matched = properties.match_status === 'matched' && properties.building_id !== null
  return <section className="outlet-details" aria-label="Selected franchise location"><h2>{properties.brand_name}</h2>{properties.name && properties.name !== properties.brand_name && <p className="outlet-name">{properties.name}</p>}<p className="outlet-address">{properties.address}</p>{!matched && <p className="outlet-match-badge">{outlet.geometry ? 'Approximate location · building unconfirmed' : 'Map location unavailable'}</p>}{properties.status === 'temporarily_closed' && <p className="outlet-closure">Temporarily closed according to the brand.</p>}<div className="outlet-provenance"><a href={sourceLink(properties.source_url)} target="_blank" rel="noreferrer">View official listing ↗</a><span>Collected {new Date(properties.retrieved_at).toLocaleDateString('en-GB')}</span></div></section>
}

export function BuildingOutlets({ outlets, onSelect }: { outlets: BuildingOutlet[]; onSelect: (id: number) => void }) {
  if (!outlets.length) return null
  const brands = [...new Set(outlets.map((outlet) => outlet.brand_name))]
  return <section className="building-outlets" aria-label="Franchise locations in this building"><div className="building-outlets-heading"><h3>{brands.length > 1 ? `${brands.length} brands share this building` : 'Franchise in this building'}</h3></div><ul>{outlets.map((outlet) => <li key={outlet.id}><button type="button" onClick={() => onSelect(outlet.id)}><strong>{outlet.brand_name}</strong><span>{outlet.address}</span>{outlet.status === 'temporarily_closed' && <small>Temporarily closed</small>}</button></li>)}</ul></section>
}
