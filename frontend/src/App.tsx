import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
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
  const workspaceRef = useRef<HTMLDivElement>(null)
  const panelRef = useRef<HTMLElement>(null)
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
  const [mobileView, setMobileView] = useState<'map' | 'details'>('map')
  const franchises = useFranchises()

  useLayoutEffect(() => {
    if (panelRef.current) panelRef.current.scrollTop = 0
  }, [selectedId, selectionRevision, outletFocus, outletGroup])

  useEffect(() => {
    const mobile = window.matchMedia('(max-width: 760px)')
    function keepFocusVisible() {
      if (!mobile.matches) return
      const hiddenPanel = workspaceRef.current?.querySelector(mobileView === 'map' ? '.building-panel' : '.map-column')
      if (hiddenPanel?.contains(document.activeElement)) {
        workspaceRef.current?.querySelector<HTMLButtonElement>('.mobile-view-switch button[aria-pressed="true"]')?.focus()
      }
    }
    keepFocusVisible()
    mobile.addEventListener('change', keepFocusVisible)
    return () => mobile.removeEventListener('change', keepFocusVisible)
  }, [mobileView])

  useEffect(() => {
    const controller = new AbortController()
    let disposed = false
    const timeout = window.setTimeout(() => controller.abort(), 15000)

    getMapMetadata(controller.signal).then((data) => {
      if (!disposed) setMetadata({ status: 'ready', data, error: null })
    }).catch(() => {
      if (!disposed) setMetadata({ status: 'error', data: null, error: controller.signal.aborted ? 'The building data request timed out. Please try again.' : 'Buildings are temporarily unavailable. Please try again.' })
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
    }).catch(() => {
      if (!disposed) setSelection({ status: 'error', data: null, error: controller.signal.aborted ? 'Building details took too long to load. Please try again.' : 'Building details are unavailable. Please try again.' })
    }).finally(() => window.clearTimeout(timeout))

    return () => { disposed = true; controller.abort(); window.clearTimeout(timeout) }
  }, [selectedId, selectionRevision])

  const selectBuilding = useCallback((id: number) => {
    setMobileView('map')
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
    setMobileView('map')
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
    setMobileView('map')
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
    setMobileView('map')
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
    setMobileView('map')
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
  const primaryAddress = feature && selectedAddress?.building_id === feature.id ? selectedAddress : associatedAddresses[0]
  const addressTitle = primaryAddress ? `${primaryAddress.street.replace(/^ulica\s+/i, '')} ${primaryAddress.house_number}` : selectedHeading
  const locality = primaryAddress ? [primaryAddress.postal_code, 'Warszawa'].filter(Boolean).join(' ') : null
  const groupedOutlets = franchises.visibleOutlets.features.filter((outlet) => outletGroup.includes(outlet.id))
  const hasSelection = selectedId !== null || outletFocus !== null || groupedOutlets.length > 0
  const previewTitle = groupedOutlets.length > 0 ? `${groupedOutlets.length} locations here` : outletFocus?.outlet.properties.brand_name ?? addressTitle ?? selectedAddress?.address ?? 'Selected building'
  const previewDescription = groupedOutlets.length > 0 ? 'Choose a franchise at this location' : outletFocus?.outlet.properties.address ?? 'Residents, grocery stores & transport'

  return (
    <div ref={workspaceRef} className="map-workspace">
      <header className="site-header">
        <a className="brand" href="/" aria-label="MasterFranchises home"><span className="brand-mark" aria-hidden="true"><svg viewBox="0 0 28 28" fill="none"><path d="M5 22V7l9 8 9-8v15M5 7l9-4 9 4M14 15v10" /></svg></span><span>MasterFranchises</span></a>
        <span className="header-page">Explore grocery locations</span>
        <span className="city-label"><span />Warsaw</span>
      </header>
      <div className="search-toolbar"><AddressSearch key={manualSelectionRevision} onSelect={selectAddress} /></div>
      <FranchiseFilters catalog={franchises.catalog} selectedIds={franchises.selectedIds} status={franchises.status} error={franchises.error} onToggle={franchises.toggleBrands} onAll={franchises.selectAll} onGrocery={franchises.selectGrocery} onNone={franchises.selectNone} onRetry={franchises.retry} />
      <nav className="mobile-view-switch" aria-label="Explorer view">
        <button type="button" aria-pressed={mobileView === 'map'} onClick={() => setMobileView('map')}>Map</button>
        <button type="button" aria-pressed={mobileView === 'details'} onClick={() => setMobileView('details')}>Details{(selectedId !== null || outletFocus || groupedOutlets.length > 0) && <span className="selection-indicator" aria-label="Location selected" />}</button>
      </nav>
      <main className={`workspace-content mobile-view-${mobileView}`}>
        <aside ref={panelRef} className="building-panel" aria-labelledby="panel-title">
          <div className="panel-section-heading"><h1 id="panel-title">{selectedId !== null ? 'Building details' : outletFocus ? 'Franchise location' : 'Explore Warsaw'}</h1>{hasSelection && <button className="clear-selection" type="button" onClick={clearSelection} aria-label="Clear selection">×</button>}</div>
          {outletFocus && selectedId === null && <OutletDetails outlet={outletFocus.outlet} />}
          {groupedOutlets.length > 0 && <section className="building-outlets overlapping-outlets"><h3>{groupedOutlets.length} locations here</h3><p>Choose a franchise to see its address and nearby area.</p><ul>{groupedOutlets.map((outlet) => <li key={outlet.id}><button type="button" onClick={() => selectOutlet(outlet.id)}><strong>{outlet.properties.brand_name}</strong><span>{outlet.properties.address}</span></button></li>)}</ul></section>}
          <div className="selection-content" aria-live="polite" aria-busy={selection.status === 'loading'}>
            {selection.status === 'idle' && !outletFocus && !groupedOutlets.length && <div className="selection-empty"><div className="building-illustration" aria-hidden="true"><svg viewBox="0 0 120 100" fill="none"><path d="m20 43 40-22 40 22-40 23-40-23Z" /><path d="M20 43v29l40 23 40-23V43M60 66v29" /><path d="m36 52 24-14 24 14M36 65l12 7m24 0 12-7M36 77l12 7m24 0 12-7" /></svg></div><h2>Start with a place</h2><p>Search an address or select a building to review nearby residents, grocery stores and transport.</p><div className="navigation-hint">Drag to move · Scroll to zoom</div><button className="mobile-map-link text-button" type="button" onClick={() => setMobileView('map')}>Explore the map</button></div>}
            {selection.status === 'loading' && <div className="selection-loading"><span className="loading-ring" /><p>Loading building details…</p></div>}
            {selection.status === 'error' && <div className="selection-error"><h2>Details unavailable</h2><p>{selection.error}</p><button className="text-button" type="button" onClick={retrySelectedBuilding}>Try again</button></div>}
            {feature && <div className="building-details"><h2>{addressTitle ?? 'Building in Warsaw'}</h2>{(locality || feature.properties.building_type_label) && <p className="building-use">{[locality, feature.properties.building_type_label].filter(Boolean).join(' · ')}</p>}</div>}
          </div>
          {feature && <BuildingOutlets outlets={feature.properties.franchises ?? []} onSelect={selectOutlet} />}
          {selectedId !== null && <BuildingProfile key={selectedId} buildingId={selectedId} />}
          {feature && <details className="building-source-details"><summary>About this building</summary>{outletFocus && <OutletDetails outlet={outletFocus.outlet} />}<dl className="building-facts"><div className="associated-addresses"><dt>Addresses</dt><dd>{associatedAddresses.length ? <ul>{associatedAddresses.map((address) => <li key={address.id} className={selectedAddress?.id === address.id ? 'chosen-address' : undefined}>{address.address}</li>)}</ul> : feature.properties.address ?? 'Address unavailable'}</dd></div><div><dt>Building footprint</dt><dd>{numberFormat.format(feature.properties.area_m2)} m²</dd></div>{feature.properties.source.updated_at && <div><dt>Building data updated</dt><dd>{formatDate(feature.properties.source.updated_at)}</dd></div>}</dl><p className="building-note">Footprint size is the area covered on the ground. Available unit size and permitted use need to be checked.</p>{addressSource && <p className="address-association-note">Address source: <SourceCredit source={addressSource} /></p>}</details>}
          <OutletDirectory outlets={franchises.visibleOutlets.features} onSelect={selectOutlet} />
        </aside>
        <div className="map-column"><WarsawMap metadata={metadata.data} selectedId={selectedId} selectedFeature={feature} addressFocus={addressFocus} onSelect={selectBuilding} onRetryMetadata={retryMetadata} metadataError={metadata.error} metadataLoading={metadata.status === 'loading'} outlets={franchises.visibleOutlets} selectedBrands={franchises.selectedBrands} outletFocus={outletFocus} onSelectOutlet={selectOutlet} onSelectOutletGroup={selectOutletGroup} onRetryOutlets={franchises.retry} /><div className="data-attribution"><span>Buildings: {source ? <SourceCredit source={source} /> : 'City of Warsaw'}</span></div></div>
      </main>
      {hasSelection && mobileView === 'map' && <div className="mobile-selection-preview"><div aria-live="polite"><strong>{previewTitle}</strong><span>{previewDescription}</span></div><button type="button" onClick={() => setMobileView('details')}>{groupedOutlets.length > 0 ? 'Choose location' : 'View details'}<span aria-hidden="true"> →</span></button></div>}
    </div>
  )
}

export default App
