type SelectableBrand = { id: string; category_id: string }

export type BrandSelection = string[] | 'all' | null

export function resolveBrandSelection(brands: readonly SelectableBrand[], selection: BrandSelection) {
  return brands.filter((brand) => selection === 'all' || (selection === null ? brand.category_id === 'grocery' : selection.includes(brand.id))).map((brand) => brand.id)
}
