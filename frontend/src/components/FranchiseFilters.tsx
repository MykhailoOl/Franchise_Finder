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
  if (error) return <section className="franchise-toolbar franchise-toolbar-message" role="status"><span>{error}</span><button className="text-button" type="button" onClick={onRetry}>Retry locations</button></section>
  if (!catalog || catalog.status === 'empty') return <section className="franchise-toolbar franchise-toolbar-message"><span>No franchise locations have been imported yet.</span><button className="text-button" type="button" onClick={onRetry}>Check again</button></section>

  const selectedBrands = catalog.brands.filter((brand) => selectedIds.includes(brand.id))
  const mapped = selectedBrands.reduce((count, brand) => count + brand.mapped_count, 0)
  const listed = selectedBrands.reduce((count, brand) => count + brand.outlet_count, 0)
  const matched = selectedBrands.reduce((count, brand) => count + brand.matched_count, 0)

  return (
    <section className="franchise-toolbar" aria-labelledby="franchise-filters-title">
      <div className="franchise-toolbar-heading"><h2 id="franchise-filters-title">Franchise locations <span>{catalog.brands.length} brands · {catalog.categories.length} categories</span></h2><div className="filter-actions"><button type="button" onClick={onAll} disabled={selectedIds.length === catalog.brands.length}>All</button><button type="button" onClick={onNone} disabled={selectedIds.length === 0}>None</button></div></div>
      <div className="category-filters">
        {catalog.categories.map((category) => {
          const brands = catalog.brands.filter((brand) => brand.category_id === category.id)
          const allSelected = brands.every((brand) => selectedIds.includes(brand.id))
          return <fieldset key={category.id} className="category-filter"><legend><button type="button" aria-pressed={allSelected} onClick={() => onToggle(brands.map((brand) => brand.id))}>{category.name}</button></legend><div>{brands.map((brand) => <label key={brand.id} className={`brand-filter ${selectedIds.includes(brand.id) ? 'brand-filter-selected' : ''}`}><input type="checkbox" checked={selectedIds.includes(brand.id)} onChange={() => onToggle([brand.id])} /><span>{brand.name}</span><small title="Listed Warsaw outlets">{countFormat.format(brand.outlet_count)}</small></label>)}</div></fieldset>
        })}
      </div>
      <div className="franchise-coverage-row"><p aria-live="polite">{selectedIds.length ? <><strong>{countFormat.format(mapped)}</strong> map locations · {countFormat.format(matched)} linked to buildings{listed > mapped && <> · {countFormat.format(listed - mapped)} without a map position</>}</> : 'Choose a brand to show its locations.'}</p><details className="coverage-details"><summary>Sources & coverage</summary><div className="coverage-popover"><p>Official locator listings, retrieved snapshots. “Complete” refers to the source directory, not independently verified trading status. An outlet’s individual ownership model is not confirmed.</p>{catalog.brands.map((brand) => <article key={brand.id}><div><strong>{brand.name}</strong><span className={brand.coverage_status === 'complete' ? '' : 'partial-coverage'}>{brand.coverage_status} snapshot</span></div><p>{brand.outlet_count} listed · {brand.mapped_count} mapped · {brand.matched_count} linked to buildings</p><p>{brand.coverage_note}</p><a href={sourceLink(brand.source_url)} target="_blank" rel="noreferrer">Official source ↗</a><time dateTime={brand.retrieved_at}>Retrieved {new Date(brand.retrieved_at).toLocaleDateString('en-GB')}</time></article>)}</div></details></div>
    </section>
  )
}
