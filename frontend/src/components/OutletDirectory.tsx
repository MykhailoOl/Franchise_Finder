import { useId, useMemo, useState } from 'react'
import type { OutletFeature } from '../lib/api'

function normalize(value: string) {
  return value.toLocaleLowerCase('pl').normalize('NFD').replace(/[\u0300-\u036f]/g, '').replace(/ł/g, 'l')
}

export function OutletDirectory({ outlets, onSelect }: { outlets: OutletFeature[]; onSelect: (id: number) => void }) {
  const inputId = useId()
  const [open, setOpen] = useState(false)
  const [query, setQuery] = useState('')
  const [limit, setLimit] = useState(15)
  const results = useMemo(() => {
    const terms = normalize(query).trim().split(/\s+/)
    return outlets.filter((outlet) => {
      const text = normalize(`${outlet.properties.brand_name} ${outlet.properties.name ?? ''} ${outlet.properties.address}`)
      return terms.every((term) => text.includes(term))
    }).sort((a, b) => a.properties.brand_name.localeCompare(b.properties.brand_name) || a.properties.address.localeCompare(b.properties.address))
  }, [outlets, query])

  return <details className="outlet-directory" onToggle={(event) => setOpen(event.currentTarget.open)}><summary>Browse locations <span>{outlets.length}</span></summary>{open && <div><label htmlFor={inputId}>Filter by brand or address</label><input id={inputId} type="search" value={query} onChange={(event) => { setQuery(event.target.value); setLimit(15) }} placeholder="e.g. Subway, Złota" /><p className="directory-result-count" aria-live="polite">{results.length} {results.length === 1 ? 'location' : 'locations'} in selected brands</p><ul>{results.slice(0, limit).map((outlet) => <li key={outlet.id}><button type="button" onClick={() => onSelect(outlet.id)}><strong>{outlet.properties.brand_name}</strong><span>{outlet.properties.address}</span>{!outlet.geometry && <small>Map location unavailable</small>}{outlet.geometry && outlet.properties.match_status !== 'matched' && <small>Building unconfirmed</small>}</button></li>)}</ul>{results.length > limit && <button className="text-button" type="button" onClick={() => setLimit((value) => value + 30)}>Show more locations</button>}</div>}</details>
}
