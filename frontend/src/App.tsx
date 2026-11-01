import { useCallback, useEffect, useState } from 'react'
import { getBuilding, getMapMetadata, sourceLink, type AddressFocusRequest, type AddressSearchResult, type BuildingFeature, type MapMetadata, type OutletFocusRequest, type SourceInfo } from './lib/api'
import { AddressSearch } from './components/AddressSearch'
import { FranchiseFilters } from './components/FranchiseFilters'
import { BuildingOutlets, OutletDetails } from './components/OutletDetails'
import { BuildingProfile } from './components/BuildingProfile'
import { OutletDirectory } from './components/OutletDirectory'
import { useFranchises } from './lib/useFranchises'
import { WarsawMap } from './map/WarsawMap'
import './App.css'

type MetadataState = { status: 'loading' | 'ready' | 'error'; data: MapMetadata | null; error: string | null }
type SelectionState = { status: 'idle' | 'loading' | 'ready' | 'error'; data: BuildingFeature | null; error: string | null }
const numberFormat = new Intl.NumberFormat('en-GB', { maximumFractionDigits: 1 })

function formatDate(value: string | null) {
  if (!value) return 'Not provided by source'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? 'Not provided by source' : new Intl.DateTimeFormat('en-GB', { day: 'numeric', month: 'short', year: 'numeric' }).format(date)
}

function SourceCredit({ source }: { source: SourceInfo }) {
  const href = sourceLink(source.url)
  const label = source.attribution || source.name
  return href ? <a href={href} title={source.name} target="_blank" rel="noreferrer">{label}<span aria-hidden="true"> ↗</span></a> : <span>{label}</span>
}

function App() {
  const [metadata, setMetadata] = useState<MetadataState>({ status: 'loading', data: null, error: null })
  const [metadataRevision, setMetadataRevision] = useState(0)
  const [selectedId, setSelectedId] = useState<number | null>(null)
  const [selection, setSelection] = useState<SelectionState>({ status: 'idle', data: null, error: null })
  const [selectionRevision, setSelectionRevision] = useState(0)
  const [addressFocus, setAddressFocus] = useState<AddressFocusRequest | null>(null)
  const [selectedAddress, setSelectedAddress] = useState<AddressSearchResult | null>(null)
  const [manualSelectionRevision, setManualSelectionRevision] = useState(0)
  const [outletFocus, setOutletFocus] = useState<OutletFocusRequest | null>(null)
  const [outletGroup, setOutletGroup] = useState<number[]>([])
  const franchises = useFranchises()

  useEffect(() => {
    const controller = new AbortController()
    let disposed = false
    const timeout = window.setTimeout(() => controller.abort(), 15000)

    getMapMetadata(controller.signal).then((data) => {
      if (!disposed) setMetadata({ status: 'ready', data, error: null })
    }).catch((error: unknown) => {
      if (!disposed) setMetadata({ status: 'error', data: null, error: controller.signal.aborted ? 'The building data request timed out. Please try again.' : error instanceof Error ? error.message : 'Building data is unavailable.' })
    }).finally(() => window.clearTimeout(timeout))

    return () => { disposed = true; controller.abort(); window.clearTimeout(timeout) }
  }, [metadataRevision])

  useEffect(() => {
    if (selectedId === null) return
    const controller = new AbortController()
    let disposed = false
    const timeout = window.setTimeout(() => controller.abort(), 15000)

    getBuilding(selectedId, controller.signal).then((data) => {
      if (!disposed) setSelection({ status: 'ready', data, error: null })
    }).catch((error: unknown) => {
      if (!disposed) setSelection({ status: 'error', data: null, error: controller.signal.aborted ? 'Building details took too long to load. Please try again.' : error instanceof Error ? error.message : 'Building details are unavailable.' })
    }).finally(() => window.clearTimeout(timeout))

    return () => { disposed = true; controller.abort(); window.clearTimeout(timeout) }
  }, [selectedId, selectionRevision])

  const selectBuilding = useCallback((id: number) => {
    setOutletGroup([])
    setManualSelectionRevision((value) => value + 1)
    setAddressFocus(null)
    setSelectedAddress(null)
    setOutletFocus(null)
    setSelectedId(id)
    setSelection({ status: 'loading', data: null, error: null })
    setSelectionRevision((value) => value + 1)
  }, [])

  const selectAddress = useCallback((result: AddressSearchResult) => {
    setOutletGroup([])
    setOutletFocus(null)
    setAddressFocus({ result })
    if (result.match_status === 'matched' && result.building_id !== null) {
      setSelectedAddress(result)
      setSelectedId(result.building_id)
      setSelection({ status: 'loading', data: null, error: null })
      setSelectionRevision((value) => value + 1)
    } else {
      setSelectedAddress(null)
      setSelectedId(null)
      setSelection({ status: 'idle', data: null, error: null })
    }
  }, [])

  const selectOutlet = useCallback((id: number) => {
    const outlet = franchises.outlets?.features.find((item) => item.id === id)
    if (!outlet) return
    setOutletGroup([])
    setManualSelectionRevision((value) => value + 1)
    setAddressFocus(null)
    setSelectedAddress(null)
    setOutletFocus({ outlet })
    if (outlet.properties.match_status === 'matched' && outlet.properties.building_id !== null) {
      setSelectedId(outlet.properties.building_id)
      setSelection({ status: 'loading', data: null, error: null })
      setSelectionRevision((value) => value + 1)
    } else {
      setSelectedId(null)
      setSelection({ status: 'idle', data: null, error: null })
    }
  }, [franchises.outlets])

  const selectOutletGroup = useCallback((ids: number[]) => {
    setOutletGroup(ids)
    setOutletFocus(null)
    setAddressFocus(null)
    setSelectedAddress(null)
    setSelectedId(null)
    setSelection({ status: 'idle', data: null, error: null })
  }, [])

  const retryMetadata = useCallback(() => {
    setMetadata((previous) => ({ status: 'loading', data: previous.data, error: null }))
    setMetadataRevision((value) => value + 1)
  }, [])

  function clearSelection() {
    setOutletGroup([])
    setManualSelectionRevision((value) => value + 1)
    setAddressFocus(null)
    setSelectedAddress(null)
    setOutletFocus(null)
    setSelectedId(null)
    setSelection({ status: 'idle', data: null, error: null })
  }

  function retrySelectedBuilding() {
    if (selectedId === null) return
    setSelection({ status: 'loading', data: null, error: null })
    setSelectionRevision((value) => value + 1)
  }

  const feature = selection.data
  const source = feature?.properties.source ?? metadata.data?.source
  const associatedAddresses = feature?.properties.addresses ?? []
  const addressSource = associatedAddresses[0]?.source
  const selectedHeading = feature && selectedAddress?.building_id === feature.id ? selectedAddress.address : feature?.properties.address
  const groupedOutlets = franchises.visibleOutlets.features.filter((outlet) => outletGroup.includes(outlet.id))

  return (
    <div className="map-workspace">
      <header className="site-header">
        <a className="brand" href="/" aria-label="MasterFranchises home"><span className="brand-mark" aria-hidden="true"><svg viewBox="0 0 28 28" fill="none"><path d="M5 22V7l9 8 9-8v15M5 7l9-4 9 4M14 15v10" /></svg></span><span>MasterFranchises</span></a>
        <div className="header-divider" /><span className="header-page">Location explorer</span>
        <span className="city-label"><span />WARSAW, POLAND</span>
      </header>

      <div className="search-toolbar"><AddressSearch key={manualSelectionRevision} onSelect={selectAddress} /></div>
      <FranchiseFilters catalog={franchises.catalog} selectedIds={franchises.selectedIds} status={franchises.status} error={franchises.error} onToggle={franchises.toggleBrands} onAll={franchises.selectAll} onNone={franchises.selectNone} onRetry={franchises.retry} />

      <main className="workspace-content">
        <aside className="building-panel" aria-labelledby="panel-title">
          <div className="panel-intro"><p className="eyebrow">LOCATION EXPLORER</p><h1 id="panel-title">A closer look<br />at <em>Warsaw.</em></h1><p>See franchise locations and the buildings around them.</p></div>
          <div className="panel-section-heading"><h2>{outletFocus ? 'Selected location' : selectedId === null ? 'Explore locations' : 'Selected building'}</h2>{(selectedId !== null || outletFocus) && <button className="clear-selection" type="button" onClick={clearSelection} aria-label="Clear selection">×</button>}</div>
          {outletFocus && <OutletDetails outlet={outletFocus.outlet} />}
          {groupedOutlets.length > 0 && <section className="building-outlets overlapping-outlets"><h3>{groupedOutlets.length} locations here</h3><p>These markers overlap. Choose an outlet to inspect its location evidence.</p><ul>{groupedOutlets.map((outlet) => <li key={outlet.id}><button type="button" onClick={() => selectOutlet(outlet.id)}><strong>{outlet.properties.brand_name}</strong><span>{outlet.properties.address}</span></button></li>)}</ul></section>}

          <div className="selection-content" aria-live="polite" aria-busy={selection.status === 'loading'}>
            {selection.status === 'idle' && !outletFocus && !groupedOutlets.length && <div className="selection-empty"><div className="building-illustration" aria-hidden="true"><svg viewBox="0 0 120 100" fill="none"><path d="m20 43 40-22 40 22-40 23-40-23Z" /><path d="M20 43v29l40 23 40-23V43M60 66v29" /><path d="m36 52 24-14 24 14M36 65l12 7m24 0 12-7M36 77l12 7m24 0 12-7" /></svg><span className="illustration-dot" /></div><h3>Select a location or a building</h3><p>Click an outlet marker for its source details, or a footprint to see the brands linked to that building.</p><div className="navigation-hint"><span aria-hidden="true">↔</span>Drag to move<span className="hint-separator" />Scroll to zoom</div></div>}
            {selection.status === 'loading' && <div className="selection-loading"><span className="loading-ring" /><p>Loading building details…</p></div>}
            {selection.status === 'error' && <div className="selection-error"><h3>Details unavailable</h3><p>{selection.error}</p><button className="text-button" type="button" onClick={retrySelectedBuilding}>Try again <span aria-hidden="true">↻</span></button></div>}
            {feature && (
              <div className="building-details">
                <div className="selected-badge"><span />BUILDING FOOTPRINT</div>
                <h3>{selectedHeading ?? 'Building in Warsaw'}</h3>
                <p className="building-id">Source ID <span>{feature.properties.source_id}</span></p>
                <div className="area-card">
                  <span>Footprint area</span>
                  <strong>{numberFormat.format(feature.properties.area_m2)} <small>m²</small></strong>
                  <p>Area covered by the building on the ground.</p>
                </div>
              </div>
            )}
          </div>

          {selectedId !== null && <BuildingProfile buildingId={selectedId} />}

          {feature && <>
              <BuildingOutlets outlets={feature.properties.franchises ?? []} onSelect={selectOutlet} />
              <details className="building-source-details">
                <summary>Building addresses & source</summary>
                <dl className="building-facts">
                  <div className="associated-addresses">
                    <dt>{associatedAddresses.length > 1 ? 'Associated addresses' : 'Associated address'}</dt>
                    <dd>{associatedAddresses.length ? <ul>{associatedAddresses.map((address) => <li key={address.id} className={selectedAddress?.id === address.id ? 'chosen-address' : undefined}>{address.address}{address.postal_code && <small>{address.postal_code}</small>}</li>)}</ul> : feature.properties.address ?? 'No address is linked to this footprint'}</dd>
                  </div>
                  <div>
                    <dt>Source building category</dt>
                    <dd>{feature.properties.building_type_label ?? feature.properties.building_type ?? 'Not provided'}{feature.properties.building_type && <small>Source code: {feature.properties.building_type}</small>}</dd>
                  </div>
                  <div><dt>Data updated</dt><dd>{formatDate(feature.properties.source.updated_at)}</dd></div>
                </dl>
                {associatedAddresses.length > 0 && <p className="address-association-note">Addresses are linked spatially from official city address points.{addressSource && <> Source: <SourceCredit source={addressSource} />.</>}</p>}
                <p className="building-note">This footprint does not indicate available commercial space, floor area, or franchise suitability.</p>
              </details>
          </>}

          <OutletDirectory outlets={franchises.visibleOutlets.features} onSelect={selectOutlet} />

          <div className="dataset-card"><div className="dataset-heading"><span className={`dataset-dot ${metadata.data?.status === 'ready' ? 'dataset-dot-ready' : ''}`} /><h2>Warsaw building data</h2></div>{metadata.status === 'loading' ? <p>Checking the building dataset…</p> : metadata.status === 'error' ? <><p>{metadata.error}</p><button type="button" className="text-button" onClick={retryMetadata}>Retry dataset ↻</button></> : metadata.data?.status === 'empty' ? <><p>No building dataset has been imported yet.</p><button type="button" className="text-button" onClick={retryMetadata}>Check again ↻</button></> : <><p>{numberFormat.format(metadata.data?.feature_count ?? 0)} building footprints</p><span className="dataset-imported">Imported {formatDate(metadata.data?.last_imported_at ?? null)}</span></>}</div>
          <footer className="panel-footer">Official listings, with source coverage shown above.<span>Locations do not establish available premises or franchise suitability.</span></footer>
        </aside>

        <div className="map-column"><WarsawMap metadata={metadata.data} selectedId={selectedId} selectedFeature={feature} addressFocus={addressFocus} onSelect={selectBuilding} onRetryMetadata={retryMetadata} metadataError={metadata.error} metadataLoading={metadata.status === 'loading'} outlets={franchises.visibleOutlets} selectedBrands={franchises.selectedBrands} outletFocus={outletFocus} onSelectOutlet={selectOutlet} onSelectOutletGroup={selectOutletGroup} onRetryOutlets={franchises.retry} /><div className="data-attribution"><span>Buildings: {source ? <SourceCredit source={source} /> : 'Warsaw city dataset'}</span><span>Data updated: {formatDate(source?.updated_at ?? null)}</span></div></div>
      </main>
    </div>
  )
}

export default App
