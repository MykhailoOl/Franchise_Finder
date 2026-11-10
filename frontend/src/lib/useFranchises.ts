import { useCallback, useEffect, useMemo, useState } from 'react'
import { getFranchiseCatalog, getOutlets, type FranchiseCatalog, type OutletCollection } from './api'

type State = {
  status: 'loading' | 'ready' | 'error'
  catalog: FranchiseCatalog | null
  outlets: OutletCollection | null
  error: string | null
}

export function useFranchises() {
  const [state, setState] = useState<State>({ status: 'loading', catalog: null, outlets: null, error: null })
  const [selection, setSelection] = useState<string[] | null>(null)
  const [revision, setRevision] = useState(0)

  useEffect(() => {
    const controller = new AbortController()
    let disposed = false
    const timeout = window.setTimeout(() => controller.abort(), 20000)
    Promise.all([getFranchiseCatalog(controller.signal), getOutlets(controller.signal)]).then(([catalog, outlets]) => {
      if (!disposed) setState({ status: 'ready', catalog, outlets, error: null })
    }).catch(() => {
      if (!disposed) setState({ status: 'error', catalog: null, outlets: null, error: controller.signal.aborted ? 'Franchise locations took too long to load. Please try again.' : 'Franchise locations are unavailable. Please try again.' })
    }).finally(() => window.clearTimeout(timeout))
    return () => { disposed = true; controller.abort(); window.clearTimeout(timeout) }
  }, [revision])

  const selectedIds = useMemo(() => {
    const available = state.catalog?.brands.map((brand) => brand.id) ?? []
    return selection === null ? available : selection.filter((id) => available.includes(id))
  }, [selection, state.catalog])

  const visibleOutlets = useMemo<OutletCollection>(() => ({
    type: 'FeatureCollection',
    features: state.outlets?.features.filter((outlet) => selectedIds.includes(outlet.properties.brand_id)) ?? [],
  }), [state.outlets, selectedIds])

  const selectedBrands = useMemo(() => state.catalog?.brands.filter((brand) => selectedIds.includes(brand.id)) ?? [], [state.catalog, selectedIds])
  const retry = useCallback(() => {
    setState({ status: 'loading', catalog: null, outlets: null, error: null })
    setRevision((value) => value + 1)
  }, [])

  function toggleBrands(ids: string[]) {
    setSelection(ids.every((id) => selectedIds.includes(id)) ? selectedIds.filter((id) => !ids.includes(id)) : [...new Set([...selectedIds, ...ids])])
  }

  return { ...state, selectedIds, selectedBrands, visibleOutlets, toggleBrands, selectAll: () => setSelection(null), selectNone: () => setSelection([]), retry }
}
