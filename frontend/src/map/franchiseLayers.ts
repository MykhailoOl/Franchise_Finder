import type { ExpressionSpecification, LayerSpecification } from 'maplibre-gl'
import type { FranchiseBrand } from '../lib/api'
import { BRAND_IMAGE_PREFIX } from './brandImages'

export const OUTLET_SOURCE = 'franchise-outlets'
export const OUTLET_POINTS = 'franchise-points'
export const OUTLET_CLUSTERS = 'franchise-clusters'
export const OUTLET_LOGOS = 'franchise-logos'
export const BUILDING_BRAND_LABELS = 'building-brand-labels'

export function selectedBrandCount(brands: FranchiseBrand[]): ExpressionSpecification {
  if (!brands.length) return ['literal', 0]
  return ['+', 0, ...brands.map((brand): ExpressionSpecification => ['case', ['boolean', ['get', brand.tile_property], false], 1, 0])]
}

export function franchiseBuildingPaint(brands: FranchiseBrand[]) {
  const count = selectedBrandCount(brands)
  const fill: ExpressionSpecification = ['case', ['boolean', ['feature-state', 'selected'], false], '#44755d', ['boolean', ['feature-state', 'hover'], false], '#93b193', ['>', count, 1], '#ac85a2', ['>', count, 0], '#d7b46c', '#bfccb7']
  const line: ExpressionSpecification = ['case', ['boolean', ['feature-state', 'selected'], false], '#244c3e', ['boolean', ['feature-state', 'hover'], false], '#527a57', ['>', count, 1], '#805b77', ['>', count, 0], '#a78037', '#8fa183']
  return { fill, line, count }
}

const categoryColor: ExpressionSpecification = ['match', ['get', 'category_id'], 'grocery', '#4f7e52', ['quick-service', 'fast-food'], '#b57d2d', 'pizza', '#b97153', 'beauty', '#8c6a97', '#456f67']

export function outletMarkerPaint(loadedBrandIds: string[]) {
  const hasLogo: ExpressionSpecification = ['in', ['get', 'brand_id'], ['literal', loadedBrandIds]]
  const radius: ExpressionSpecification = ['interpolate', ['linear'], ['zoom'], 10, ['case', hasLogo, 14, 5], 16, ['case', hasLogo, 17, 7], 19, ['case', hasLogo, 19, 9]]
  const selectedRadius: ExpressionSpecification = ['interpolate', ['linear'], ['zoom'], 10, ['case', hasLogo, 18, 10], 16, ['case', hasLogo, 21, 12], 19, ['case', hasLogo, 23, 14]]
  const fill: ExpressionSpecification = ['case', hasLogo, '#fffefa', ['!=', ['get', 'match_status'], 'matched'], '#fff9ed', categoryColor]
  const stroke: ExpressionSpecification = ['case', ['!=', ['get', 'match_status'], 'matched'], '#b47d39', hasLogo, '#7e9782', '#fffdf5']
  return { hasLogo, radius, selectedRadius, fill, stroke }
}

export function outletLayers(): LayerSpecification[] {
  return [
    {
      id: OUTLET_CLUSTERS, type: 'circle', source: OUTLET_SOURCE,
      filter: ['has', 'point_count'],
      paint: {
        'circle-color': '#3d6250',
        'circle-radius': ['step', ['get', 'point_count'], 17, 10, 21, 50, 26],
        'circle-stroke-width': 3, 'circle-stroke-color': '#fcfbf1',
      },
    },
    {
      id: 'franchise-cluster-count', type: 'symbol', source: OUTLET_SOURCE,
      filter: ['has', 'point_count'],
      layout: { 'text-field': ['get', 'point_count_abbreviated'], 'text-font': ['Noto Sans Regular'], 'text-size': 12, 'text-allow-overlap': true },
      paint: { 'text-color': '#fffdf2' },
    },
    {
      id: OUTLET_POINTS, type: 'circle', source: OUTLET_SOURCE,
      filter: ['!', ['has', 'point_count']],
      paint: {
        'circle-radius': ['interpolate', ['linear'], ['zoom'], 10, 5, 16, 7, 19, 9],
        'circle-color': ['case', ['!=', ['get', 'match_status'], 'matched'], '#fff9ed', categoryColor],
        'circle-stroke-color': ['case', ['!=', ['get', 'match_status'], 'matched'], '#b47d39', '#fffdf5'],
        'circle-stroke-width': 2.5,
      },
    },
    {
      id: OUTLET_LOGOS, type: 'symbol', source: OUTLET_SOURCE,
      filter: ['all', ['!', ['has', 'point_count']], ['in', ['get', 'brand_id'], ['literal', []]]],
      layout: {
        'icon-image': ['concat', BRAND_IMAGE_PREFIX, ['get', 'brand_id']],
        'icon-size': ['interpolate', ['linear'], ['zoom'], 10, 0.75, 16, 1, 19, 1.125],
        'icon-allow-overlap': true, 'icon-ignore-placement': true,
        'icon-anchor': 'center', 'icon-pitch-alignment': 'viewport', 'icon-rotation-alignment': 'viewport',
      },
    },
    {
      id: 'franchise-point-labels', type: 'symbol', source: OUTLET_SOURCE, minzoom: 16,
      filter: ['!', ['has', 'point_count']],
      layout: {
        'text-field': ['get', 'brand_name'], 'text-font': ['Noto Sans Regular'],
        'text-size': 11, 'text-offset': [0, 2.2], 'text-anchor': 'top', 'text-padding': 4,
      },
      paint: { 'text-color': '#3e5948', 'text-halo-color': '#fffdf5', 'text-halo-width': 2 },
    },
    {
      id: 'franchise-selected-point', type: 'circle', source: OUTLET_SOURCE,
      filter: ['==', ['id'], -1],
      paint: { 'circle-radius': 12, 'circle-opacity': 0, 'circle-stroke-color': '#254a39', 'circle-stroke-width': 2 },
    },
  ]
}
