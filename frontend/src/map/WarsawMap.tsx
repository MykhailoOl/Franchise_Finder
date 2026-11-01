import { useEffect, useRef, useState } from 'react'
import { AttributionControl, LngLatBounds, Map, Marker, NavigationControl, ScaleControl, setWorkerUrl, type GeoJSONSource } from 'maplibre-gl'
import workerUrl from 'maplibre-gl/dist/maplibre-gl-worker.mjs?worker&url'
import 'maplibre-gl/dist/maplibre-gl.css'
import type { AddressFocusRequest, BuildingFeature, FranchiseBrand, MapMetadata, MappedOutletFeature, OutletCollection, OutletFocusRequest } from '../lib/api'
import { BUILDING_LAYER, BUILDING_SOURCE, INITIAL_ZOOM, SELECTED_SOURCE, WARSAW_BOUNDS, WARSAW_CENTER, buildingLayers, createMapStyle } from './style'
import { BUILDING_BRAND_LABELS, OUTLET_CLUSTERS, OUTLET_LOGOS, OUTLET_POINTS, OUTLET_SOURCE, franchiseBuildingPaint, outletLayers, outletMarkerPaint } from './franchiseLayers'
import { loadBrandImages } from './brandImages'

setWorkerUrl(workerUrl)

type LoadStatus = 'loading' | 'ready' | 'error'

type Props = {
  metadata: MapMetadata | null
  selectedId: number | null
  selectedFeature: BuildingFeature | null
  addressFocus: AddressFocusRequest | null
  onSelect: (id: number) => void
  onRetryMetadata: () => void
  metadataError: string | null
  metadataLoading: boolean
  outlets: OutletCollection
  selectedBrands: FranchiseBrand[]
  outletFocus: OutletFocusRequest | null
  onSelectOutlet: (id: number) => void
  onSelectOutletGroup: (ids: number[]) => void
  onRetryOutlets: () => void
}

const emptySelection = { type: 'FeatureCollection' as const, features: [] }

export function WarsawMap({ metadata, selectedId, selectedFeature, addressFocus, onSelect, onRetryMetadata, metadataError, metadataLoading, outlets, selectedBrands, outletFocus, onSelectOutlet, onSelectOutletGroup, onRetryOutlets }: Props) {
  const containerRef = useRef<HTMLDivElement>(null)
  const mapRef = useRef<Map | null>(null)
  const selectedRef = useRef<number | null>(null)
  const failedSources = useRef(new Set<string>())
  const lastFocusedAddress = useRef<{ map: Map; request: AddressFocusRequest } | null>(null)
  const lastFocusedOutlet = useRef<{ map: Map; request: OutletFocusRequest } | null>(null)
  const [readyMap, setReadyMap] = useState<Map | null>(null)
  const [basemapStatus, setBasemapStatus] = useState<LoadStatus>('loading')
  const [buildingStatus, setBuildingStatus] = useState<LoadStatus>('loading')
  const [mapError, setMapError] = useState<string | null>(null)
  const [revision, setRevision] = useState(0)
  const [zoom, setZoom] = useState(INITIAL_ZOOM)
  const [center, setCenter] = useState(WARSAW_CENTER)
  const [outletError, setOutletError] = useState<string | null>(null)

  useEffect(() => {
    if (!containerRef.current) return

    let map: Map
    try {
      map = new Map({
        container: containerRef.current,
        style: createMapStyle(),
        center: WARSAW_CENTER,
        zoom: INITIAL_ZOOM,
        minZoom: 10,
        maxZoom: 21,
        maxBounds: WARSAW_BOUNDS,
        maxPitch: 0,
        attributionControl: false,
        dragRotate: false,
        touchPitch: false,
        cancelPendingTileRequestsWhileZooming: false,
        renderWorldCopies: false,
        canvasContextAttributes: { antialias: true },
      })
    } catch {
      queueMicrotask(() => setMapError('The interactive map could not start. Enable hardware acceleration in your browser and try again.'))
      return
    }

    mapRef.current = map
    failedSources.current.clear()
    let hoveredId: number | null = null
    const slowTimer = window.setTimeout(() => {
      if (!map.isSourceLoaded('context')) {
        failedSources.current.add('context')
        setBasemapStatus('error')
      }
    }, 20000)

    map.addControl(new NavigationControl({ showCompass: false }), 'top-right')
    map.addControl(new ScaleControl({ unit: 'metric', maxWidth: 100 }), 'bottom-left')
    map.addControl(new AttributionControl({ compact: false }), 'bottom-right')
    map.touchZoomRotate.disableRotation()
    map.getCanvas().setAttribute('aria-label', 'Warsaw building map. Drag to pan, scroll to zoom, or use the zoom buttons. Select a building footprint for details.')

    map.on('style.load', () => {
      if (mapRef.current !== map) return
      setReadyMap(map)
      setMapError(null)
      setZoom(map.getZoom())
      const currentCenter = map.getCenter()
      setCenter([currentCenter.lng, currentCenter.lat])
    })
    map.on('moveend', () => {
      setZoom(map.getZoom())
      const next = map.getCenter()
      setCenter([next.lng, next.lat])
    })
    map.on('sourcedataloading', (event) => {
      if (event.sourceId === BUILDING_SOURCE && !failedSources.current.has(BUILDING_SOURCE)) setBuildingStatus('loading')
    })
    map.on('sourcedata', (event) => {
      if (event.sourceId === 'context' && event.isSourceLoaded && !failedSources.current.has('context')) {
        window.clearTimeout(slowTimer)
        setBasemapStatus('ready')
      }
      if (event.sourceId === BUILDING_SOURCE && event.isSourceLoaded && !failedSources.current.has(BUILDING_SOURCE)) setBuildingStatus('ready')
    })
    map.on('error', (event) => {
      const sourceId = 'sourceId' in event ? String(event.sourceId) : ''
      if (sourceId === OUTLET_SOURCE) {
        setOutletError('Franchise locations could not be drawn. Please reload locations.')
        return
      }
      const buildingError = sourceId === BUILDING_SOURCE || event.error.message.includes('/api/v1/map/tiles/')
      if (buildingError) {
        failedSources.current.add(BUILDING_SOURCE)
        setBuildingStatus('error')
      } else {
        failedSources.current.add('context')
        setBasemapStatus('error')
      }
    })
    map.on('mousemove', BUILDING_LAYER, (event) => {
      const id = event.features?.[0]?.id
      if (typeof id !== 'number' || hoveredId === id) return
      if (hoveredId !== null) map.setFeatureState({ source: BUILDING_SOURCE, sourceLayer: 'buildings', id: hoveredId }, { hover: false })
      hoveredId = id
      map.setFeatureState({ source: BUILDING_SOURCE, sourceLayer: 'buildings', id }, { hover: true })
      map.getCanvas().style.cursor = 'pointer'
    })
    map.on('mouseleave', BUILDING_LAYER, () => {
      if (hoveredId !== null && map.getSource(BUILDING_SOURCE)) map.setFeatureState({ source: BUILDING_SOURCE, sourceLayer: 'buildings', id: hoveredId }, { hover: false })
      hoveredId = null
      map.getCanvas().style.cursor = ''
    })
    map.on('click', BUILDING_LAYER, (event) => {
      const outletLayers = [OUTLET_POINTS, OUTLET_CLUSTERS].filter((id) => map.getLayer(id))
      if (outletLayers.length && map.queryRenderedFeatures(event.point, { layers: outletLayers }).length) return
      const id = event.features?.[0]?.id
      if (typeof id === 'number' && Number.isSafeInteger(id)) onSelect(id)
    })

    const observer = new ResizeObserver(() => map.resize())
    observer.observe(containerRef.current)

    return () => {
      observer.disconnect()
      window.clearTimeout(slowTimer)
      map.remove()
      mapRef.current = null
      selectedRef.current = null
    }
  }, [onSelect, revision])

  useEffect(() => {
    const map = mapRef.current
    if (!map || readyMap !== map || metadata?.status !== 'ready') return

    if (!map.getSource(BUILDING_SOURCE)) {
      map.addSource(BUILDING_SOURCE, {
        type: 'vector',
        tiles: [`${window.location.origin}/api/v1/map/tiles/{z}/{x}/{y}.pbf`],
        bounds: metadata.bounds,
        minzoom: metadata.minzoom,
        maxzoom: metadata.maxzoom,
      })
      for (const layer of buildingLayers()) map.addLayer(layer, 'street-labels')
      map.addSource(SELECTED_SOURCE, { type: 'geojson', data: emptySelection })
      map.addLayer({ id: 'selected-fill', type: 'fill', source: SELECTED_SOURCE, minzoom: 13, paint: { 'fill-color': '#3d725b', 'fill-opacity': 0.55 } }, 'street-labels')
      map.addLayer({ id: 'selected-outline', type: 'line', source: SELECTED_SOURCE, minzoom: 13, paint: { 'line-color': '#234d3b', 'line-width': 2.5 } }, 'street-labels')
    }

    if (selectedRef.current !== null && selectedRef.current !== selectedId) {
      map.setFeatureState({ source: BUILDING_SOURCE, sourceLayer: 'buildings', id: selectedRef.current }, { selected: false })
    }
    if (selectedId !== null) map.setFeatureState({ source: BUILDING_SOURCE, sourceLayer: 'buildings', id: selectedId }, { selected: true })
    selectedRef.current = selectedId
    const selectionSource = map.getSource(SELECTED_SOURCE) as GeoJSONSource
    selectionSource.setData(selectedFeature ?? emptySelection)
  }, [metadata, readyMap, selectedId, selectedFeature])

  useEffect(() => {
    const map = mapRef.current
    if (!map || readyMap !== map || !map.getLayer(BUILDING_LAYER)) return
    const paint = franchiseBuildingPaint(selectedBrands)
    map.setPaintProperty(BUILDING_LAYER, 'fill-color', paint.fill)
    map.setPaintProperty('building-outlines', 'line-color', paint.line)
    if (!map.getLayer(BUILDING_BRAND_LABELS)) {
      map.addLayer({
        id: BUILDING_BRAND_LABELS, type: 'symbol', source: BUILDING_SOURCE, 'source-layer': 'buildings', minzoom: 16,
        filter: ['>', paint.count, 1],
        layout: { 'text-field': ['concat', ['to-string', paint.count], ' brands'], 'text-font': ['Noto Sans Regular'], 'text-size': 10, 'text-padding': 10 },
        paint: { 'text-color': '#6c4864', 'text-halo-color': '#fffaf2', 'text-halo-width': 2 },
      })
    } else {
      map.setFilter(BUILDING_BRAND_LABELS, ['>', paint.count, 1])
      map.setLayoutProperty(BUILDING_BRAND_LABELS, 'text-field', ['concat', ['to-string', paint.count], ' brands'])
    }
  }, [metadata, readyMap, selectedBrands])

  useEffect(() => {
    const map = mapRef.current
    if (!map || readyMap !== map) return
    const data = { type: 'FeatureCollection' as const, features: outlets.features.filter((outlet): outlet is MappedOutletFeature => outlet.geometry !== null) }
    if (!map.getSource(OUTLET_SOURCE)) {
      map.addSource(OUTLET_SOURCE, { type: 'geojson', data, cluster: true, clusterRadius: 38, clusterMaxZoom: 14 })
      for (const layer of outletLayers()) map.addLayer(layer)
    } else {
      (map.getSource(OUTLET_SOURCE) as GeoJSONSource).setData(data)
    }
    map.setFilter('franchise-selected-point', ['==', ['id'], outletFocus?.outlet.id ?? -1])
  }, [readyMap, outlets, outletFocus])

  useEffect(() => {
    const map = mapRef.current
    if (!map || readyMap !== map) return
    const controller = new AbortController()
    loadBrandImages(map, controller.signal).then((brandIds) => {
      if (controller.signal.aborted || mapRef.current !== map || !map.getLayer(OUTLET_LOGOS)) return
      const paint = outletMarkerPaint(brandIds)
      map.setFilter(OUTLET_LOGOS, ['all', ['!', ['has', 'point_count']], paint.hasLogo])
      map.setPaintProperty(OUTLET_POINTS, 'circle-radius', paint.radius)
      map.setPaintProperty(OUTLET_POINTS, 'circle-color', paint.fill)
      map.setPaintProperty(OUTLET_POINTS, 'circle-stroke-color', paint.stroke)
      map.setPaintProperty('franchise-selected-point', 'circle-radius', paint.selectedRadius)
    })
    return () => controller.abort()
  }, [readyMap])

  useEffect(() => {
    const map = mapRef.current
    if (!map || readyMap !== map) return
    const onPoint = (event: { features?: { id?: string | number }[] }) => {
      const ids = [...new Set(event.features?.map((feature) => feature.id).filter((id): id is number => typeof id === 'number') ?? [])]
      if (ids.length > 1) onSelectOutletGroup(ids)
      else if (ids.length === 1) onSelectOutlet(ids[0])
    }
    const onCluster = async (event: { features?: { geometry: { type: string; coordinates?: unknown }; properties: Record<string, unknown> }[] }) => {
      const cluster = event.features?.[0]
      if (!cluster || cluster.geometry.type !== 'Point') return
      const source = map.getSource(OUTLET_SOURCE) as GeoJSONSource | undefined
      if (!source) return
      try {
        const expansionZoom = await source.getClusterExpansionZoom(Number(cluster.properties.cluster_id))
        if (mapRef.current !== map) return
        map.easeTo({ center: cluster.geometry.coordinates as [number, number], zoom: Math.min(expansionZoom, 19), duration: 600 })
      } catch {
        if (mapRef.current === map) setOutletError('This group of locations changed. Reload locations to try again.')
      }
    }
    const pointer = () => { map.getCanvas().style.cursor = 'pointer' }
    const resetPointer = () => { map.getCanvas().style.cursor = '' }
    map.on('click', OUTLET_POINTS, onPoint)
    map.on('click', OUTLET_CLUSTERS, onCluster)
    for (const layer of [OUTLET_POINTS, OUTLET_CLUSTERS]) {
      map.on('mouseenter', layer, pointer)
      map.on('mouseleave', layer, resetPointer)
    }
    return () => {
      map.off('click', OUTLET_POINTS, onPoint)
      map.off('click', OUTLET_CLUSTERS, onCluster)
      for (const layer of [OUTLET_POINTS, OUTLET_CLUSTERS]) {
        map.off('mouseenter', layer, pointer)
        map.off('mouseleave', layer, resetPointer)
      }
    }
  }, [readyMap, onSelectOutlet, onSelectOutletGroup])

  useEffect(() => {
    const map = mapRef.current
    if (!map || readyMap !== map || !outletFocus) return
    if (lastFocusedOutlet.current?.map === map && lastFocusedOutlet.current.request === outletFocus) return
    const outlet = outletFocus.outlet
    if (outlet.properties.match_status === 'matched' && outlet.properties.building_id !== null) {
      if (!selectedFeature || selectedFeature.id !== outlet.properties.building_id) return
      const bounds = new LngLatBounds()
      for (const polygon of selectedFeature.geometry.coordinates) for (const ring of polygon) for (const position of ring) bounds.extend([position[0], position[1]])
      if (!bounds.isEmpty()) map.fitBounds(bounds, { padding: { top: 100, right: 65, bottom: 100, left: 55 }, maxZoom: 18.5, duration: 700 })
    } else if (outlet.geometry) {
      map.flyTo({ center: outlet.geometry.coordinates as [number, number], zoom: 17.5, duration: 700 })
    }
    lastFocusedOutlet.current = { map, request: outletFocus }
  }, [readyMap, outletFocus, selectedFeature])

  useEffect(() => {
    const map = mapRef.current
    if (!map || readyMap !== map || !addressFocus) return
    const result = addressFocus.result
    const alreadyFocused = lastFocusedAddress.current?.map === map && lastFocusedAddress.current.request === addressFocus

    if (result.match_status === 'matched' && result.building_id !== null) {
      if (!selectedFeature || selectedFeature.id !== result.building_id || alreadyFocused) return
      const bounds = new LngLatBounds()
      for (const polygon of selectedFeature.geometry.coordinates) {
        for (const ring of polygon) {
          for (const position of ring) {
            if (Number.isFinite(position[0]) && Number.isFinite(position[1])) bounds.extend([position[0], position[1]])
          }
        }
      }
      if (bounds.isEmpty()) return
      map.fitBounds(bounds, { padding: { top: 110, right: 65, bottom: 110, left: 55 }, maxZoom: 18.5, duration: 900 })
      lastFocusedAddress.current = { map, request: addressFocus }
      return
    }

    const markerElement = document.createElement('div')
    markerElement.className = 'unmatched-address-marker'
    markerElement.setAttribute('role', 'img')
    markerElement.setAttribute('aria-label', `Address point: ${result.address}. No unique building match.`)
    markerElement.title = `${result.address} — address point, no unique building match`
    const marker = new Marker({ element: markerElement }).setLngLat(result.coordinates).addTo(map)
    if (!alreadyFocused) map.flyTo({ center: result.coordinates, zoom: 17.5, duration: 900 })
    lastFocusedAddress.current = { map, request: addressFocus }
    return () => { marker.remove() }
  }, [addressFocus, selectedFeature, readyMap])

  useEffect(() => {
    if (buildingStatus !== 'loading' || metadata?.status !== 'ready' || readyMap !== mapRef.current || !readyMap || zoom < metadata.minzoom) return
    const timeout = window.setTimeout(() => {
      failedSources.current.add(BUILDING_SOURCE)
      setBuildingStatus('error')
    }, 20000)
    return () => window.clearTimeout(timeout)
  }, [buildingStatus, metadata, readyMap, zoom])

  function retryMap() {
    setReadyMap(null)
    setBasemapStatus('loading')
    setBuildingStatus('loading')
    setMapError(null)
    failedSources.current.clear()
    setRevision((value) => value + 1)
  }

  function retryBuildings() {
    failedSources.current.delete(BUILDING_SOURCE)
    setBuildingStatus('loading')
    const map = mapRef.current
    if (map?.getSource(BUILDING_SOURCE)) map.refreshTiles(BUILDING_SOURCE)
    onRetryMetadata()
  }

  const buildingMessage = metadataLoading ? 'Loading building data…'
    : metadataError ? 'Building data unavailable'
      : metadata?.status === 'empty' ? 'Building data has not been imported yet'
        : buildingStatus === 'error' ? 'Some building footprints could not load'
          : zoom < 13 ? 'Zoom in to see individual buildings'
            : buildingStatus === 'loading' ? 'Loading buildings in this view…'
              : 'Select a building to explore it'

  return (
    <section className="map-area" aria-label="Interactive Warsaw map">
      <div className="map-canvas" ref={containerRef} />
      <div className="map-title"><span className="map-title-dot" /><div><strong>Warsaw</strong><span>Building footprints</span></div></div>
      <button className="show-all-outlets" type="button" disabled={!outlets.features.some((outlet) => outlet.geometry)} onClick={() => {
        const bounds = new LngLatBounds()
        for (const outlet of outlets.features) if (outlet.geometry) bounds.extend(outlet.geometry.coordinates as [number, number])
        if (!bounds.isEmpty()) mapRef.current?.fitBounds(bounds, { padding: 80, maxZoom: 15, duration: 800 })
      }}>View all locations</button>
      <button className="reset-view" type="button" onClick={() => mapRef.current?.flyTo({ center: WARSAW_CENTER, zoom: INITIAL_ZOOM, bearing: 0, duration: 800 })} disabled={Boolean(mapError)} aria-label="Reset map to central Warsaw"><svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M12 3v3m0 12v3M3 12h3m12 0h3" /><circle cx="12" cy="12" r="6" /><circle cx="12" cy="12" r="2" /></svg></button>
      <div className="map-message-stack" aria-live="polite">
        {(mapError || basemapStatus === 'error') && <div className="map-message map-message-error"><span>{mapError ?? 'Street map unavailable. Building data is loaded separately.'}</span><button type="button" onClick={retryMap}>Retry map</button></div>}
        {!mapError && basemapStatus === 'loading' && <div className="map-message">Loading street map…</div>}
        {outletError && <div className="map-message map-message-error"><span>{outletError}</span><button type="button" onClick={() => { setOutletError(null); onRetryOutlets() }}>Reload locations</button></div>}
        <div className={`map-message ${(metadataError || buildingStatus === 'error') ? 'map-message-error' : ''}`}><span>{buildingMessage}</span>{(metadataError || metadata?.status === 'empty' || buildingStatus === 'error') && <button type="button" onClick={retryBuildings} disabled={metadataLoading}>Retry buildings</button>}</div>
      </div>
      <div className="map-legend"><span><i />Footprint</span><span><i className="outlet-building-swatch" />Franchise</span><span><i className="multiple-brands-swatch" />Multiple brands</span><span><i className="uncertain-outlet-swatch" />Unconfirmed building</span></div>
      <div className="map-coordinates" aria-label="Map center and zoom">{center[1].toFixed(4)}° N · {center[0].toFixed(4)}° E<span>Zoom {zoom.toFixed(1)}</span></div>
    </section>
  )
}
