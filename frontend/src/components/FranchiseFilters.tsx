import { sourceLink, type FranchiseCatalog } from '../lib/api'

type Props = {
  catalog: FranchiseCatalog | null
  selectedIds: string[]
  status: 'loading' | 'ready' | 'error'
  error: string | null
  onToggle: (ids: string[]) => void
  onAll: () => void
  onNone: () => void
  onRetry: () => void
}

const countFormat = new Intl.NumberFormat('en-GB')

export function FranchiseFilters({ catalog, selectedIds, status, error, onToggle, onAll, onNone, onRetry }: Props) {
  if (status === 'loading') return <section className="franchise-toolbar franchise-toolbar-message" aria-live="polite">Loading franchise locations…</section>
  if (error) return <section className="franchise-toolbar franchise-toolbar-message" role="status"><span>Franchise locations are unavailable.</span><button className="text-button" type="button" onClick={onRetry}>Try again</button></section>
  if (!catalog || catalog.status === 'empty') return <section className="franchise-toolbar franchise-toolbar-message"><span>Franchise locations are not available yet.</span><button className="text-button" type="button" onClick={onRetry}>Try again</button></section>
  const selectedBrands = catalog.brands.filter((brand) => selectedIds.includes(brand.id))
  const mapped = selectedBrands.reduce((count, brand) => count + brand.mapped_count, 0)

  return <section className="franchise-toolbar" aria-label="Franchise filters">
    <details className="brand-menu">
      <summary><strong>Brands</strong><span>{selectedIds.length === catalog.brands.length ? `All ${catalog.brands.length}` : `${selectedIds.length} selected`}</span></summary>
      <div className="brand-menu-content">
        <div className="filter-actions"><button type="button" onClick={onAll} disabled={selectedIds.length === catalog.brands.length}>Show all</button><button type="button" onClick={onNone} disabled={selectedIds.length === 0}>Hide all</button></div>
        <div className="category-filters">{catalog.categories.map((category) => {
          const brands = catalog.brands.filter((brand) => brand.category_id === category.id)
          const allSelected = brands.every((brand) => selectedIds.includes(brand.id))
          return <fieldset key={category.id} className="category-filter"><legend><button type="button" aria-pressed={allSelected} onClick={() => onToggle(brands.map((brand) => brand.id))}>{category.name}</button></legend><div>{brands.map((brand) => <label key={brand.id} className={`brand-filter ${selectedIds.includes(brand.id) ? 'brand-filter-selected' : ''}`}><input type="checkbox" checked={selectedIds.includes(brand.id)} onChange={() => onToggle([brand.id])} /><span>{brand.name}</span></label>)}</div></fieldset>
        })}</div>
      </div>
    </details>
    <p className="franchise-visible-count" aria-live="polite">{selectedIds.length ? `${countFormat.format(mapped)} locations in Warsaw` : 'No brands selected'}</p>
    <details className="coverage-details"><summary>Sources</summary><div className="coverage-popover"><p>Locations come from each brand’s public directory. Opening status and exact building locations may change.</p>{catalog.brands.map((brand) => <article key={brand.id}><div><strong>{brand.name}</strong>{brand.coverage_status !== 'complete' && <span className="partial-coverage">{brand.coverage_status === 'unavailable' ? 'Unavailable' : 'Partial coverage'}</span>}</div><a href={sourceLink(brand.source_url)} target="_blank" rel="noreferrer">Official directory ↗</a><time dateTime={brand.retrieved_at}>Collected {new Date(brand.retrieved_at).toLocaleDateString('en-GB')}</time></article>)}</div></details>
  </section>
}
