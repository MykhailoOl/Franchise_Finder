import type { ExpressionSpecification, FilterSpecification, LayerSpecification, StyleSpecification } from 'maplibre-gl'

export const WARSAW_CENTER: [number, number] = [21.0122, 52.2297]
export const WARSAW_BOUNDS: [[number, number], [number, number]] = [[20.75, 52.05], [21.3, 52.4]]
export const INITIAL_ZOOM = 15.5
export const BUILDING_SOURCE = 'warsaw-buildings'
export const BUILDING_LAYER = 'building-footprints'
export const SELECTED_SOURCE = 'selected-building'

const roadFilter: FilterSpecification = ['match', ['get', 'class'], ['motorway', 'trunk', 'primary', 'secondary', 'tertiary', 'minor', 'service'], true, false]
const roadScale: ExpressionSpecification = ['match', ['get', 'class'], ['motorway', 'trunk'], 1.6, 'primary', 1.5, 'secondary', 1.2, 'tertiary', 1, 'minor', 0.7, 'service', 0.35, 0.6]

function roadWidth(casing: boolean): ExpressionSpecification {
  return ['interpolate', ['exponential', 1.4], ['zoom'],
    10, ['+', ['*', 0.6, roadScale], casing ? 0.4 : 0],
    14, ['+', ['*', 3, roadScale], casing ? 0.8 : 0],
    18, ['+', ['*', 18, roadScale], casing ? 1.2 : 0],
    21, ['+', ['*', 70, roadScale], casing ? 2 : 0],
  ]
}

export function createMapStyle(): StyleSpecification {
  return {
    version: 8,
    name: 'MasterFranchises Warsaw',
    glyphs: 'https://tiles.openfreemap.org/fonts/{fontstack}/{range}.pbf',
    sources: {
      context: {
        type: 'vector',
        url: 'https://tiles.openfreemap.org/planet',
        attribution: '<a href="https://openfreemap.org/">OpenFreeMap</a> · <a href="https://openmaptiles.org/">OpenMapTiles</a> · © <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
      },
    },
    layers: [
      { id: 'land', type: 'background', paint: { 'background-color': '#f0efe7' } },
      {
        id: 'green-land', type: 'fill', source: 'context', 'source-layer': 'landcover',
        filter: ['match', ['get', 'class'], ['wood', 'grass', 'farmland'], true, false],
        paint: { 'fill-color': '#dce5d4', 'fill-opacity': 0.7 },
      },
      {
        id: 'parks', type: 'fill', source: 'context', 'source-layer': 'park',
        paint: { 'fill-color': '#d5e1cc', 'fill-opacity': 0.65 },
      },
      {
        id: 'water', type: 'fill', source: 'context', 'source-layer': 'water',
        paint: { 'fill-color': '#c5dcd9' },
      },
      {
        id: 'waterways', type: 'line', source: 'context', 'source-layer': 'waterway',
        paint: { 'line-color': '#c5dcd9', 'line-width': ['interpolate', ['linear'], ['zoom'], 10, 1, 18, 5] },
      },
      {
        id: 'paths', type: 'line', source: 'context', 'source-layer': 'transportation', minzoom: 14,
        filter: ['match', ['get', 'class'], ['track', 'path', 'pedestrian'], true, false],
        layout: { 'line-cap': 'round', 'line-join': 'round' },
        paint: {
          'line-color': '#dedfd3',
          'line-width': ['interpolate', ['linear'], ['zoom'], 14, 0.5, 16, 0.8, 18, 1.3, 21, 3],
        },
      },
      {
        id: 'road-edges', type: 'line', source: 'context', 'source-layer': 'transportation',
        filter: roadFilter,
        layout: { 'line-cap': 'round', 'line-join': 'round' },
        paint: { 'line-color': '#dddcd2', 'line-width': roadWidth(true) },
      },
      {
        id: 'roads', type: 'line', source: 'context', 'source-layer': 'transportation',
        filter: roadFilter,
        layout: { 'line-cap': 'round', 'line-join': 'round' },
        paint: {
          'line-color': ['match', ['get', 'class'], 'service', '#f6f5ee', '#fffefa'],
          'line-width': roadWidth(false),
        },
      },
      {
        id: 'street-labels', type: 'symbol', source: 'context', 'source-layer': 'transportation_name', minzoom: 13,
        filter: roadFilter,
        layout: {
          'symbol-placement': 'line', 'text-field': ['coalesce', ['get', 'name:pl'], ['get', 'name']],
          'text-font': ['Noto Sans Regular'], 'text-size': ['interpolate', ['linear'], ['zoom'], 13, 10, 18, 13],
          'symbol-spacing': 300, 'text-max-angle': 35,
        },
        paint: { 'text-color': '#83877c', 'text-halo-color': '#fffefa', 'text-halo-width': 1.5 },
      },
      {
        id: 'district-labels', type: 'symbol', source: 'context', 'source-layer': 'place', maxzoom: 15,
        filter: ['match', ['get', 'class'], ['city', 'suburb', 'quarter', 'neighbourhood'], true, false],
        layout: {
          'text-field': ['coalesce', ['get', 'name:pl'], ['get', 'name']],
          'text-font': ['Noto Sans Regular'], 'text-size': ['interpolate', ['linear'], ['zoom'], 10, 12, 14, 15],
          'text-letter-spacing': 0.08, 'text-transform': 'uppercase', 'text-padding': 25,
        },
        paint: { 'text-color': '#7c8777', 'text-halo-color': '#f0efe7', 'text-halo-width': 2 },
      },
    ],
  }
}

export function buildingLayers(): LayerSpecification[] {
  return [
    {
      id: BUILDING_LAYER, type: 'fill', source: BUILDING_SOURCE, 'source-layer': 'buildings', minzoom: 13,
      paint: {
        'fill-color': ['case', ['boolean', ['feature-state', 'selected'], false], '#44755d', ['boolean', ['feature-state', 'hover'], false], '#93b193', '#bfccb7'],
        'fill-opacity': 0.9,
      },
    },
    {
      id: 'building-outlines', type: 'line', source: BUILDING_SOURCE, 'source-layer': 'buildings', minzoom: 13,
      paint: {
        'line-color': ['case', ['boolean', ['feature-state', 'selected'], false], '#244c3e', ['boolean', ['feature-state', 'hover'], false], '#527a57', '#8fa183'],
        'line-width': ['interpolate', ['linear'], ['zoom'], 13, 0.4, 17, 0.9, 21, 1.2],
      },
    },
  ]
}
