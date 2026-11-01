import type { Map } from 'maplibre-gl'

export const BRAND_IMAGE_PREFIX = 'brand-logo-'

type BrandAsset = { path: string; sourceUrl: string; retrievedAt: string; crop?: [number, number, number, number] }

const assets: Record<string, BrandAsset> = {
  zabka: {
    path: '/brands/zabka.png',
    sourceUrl: 'https://cdn.zabka.pl/wp-content/uploads/2023/06/03065825/cropped-favicon-192x192.png',
    retrievedAt: '2026-10-23',
  },
  lewiatan: {
    path: '/brands/lewiatan.png',
    sourceUrl: 'https://lewiatan.pl/themes/custom/lew/images/logo-lokalnosci-2.png',
    retrievedAt: '2026-10-23',
    crop: [65, 23, 72, 52],
  },
  mcdonalds: {
    path: '/brands/mcdonalds.png',
    sourceUrl: 'https://cdn.mcdonalds.pl/public/build/images/favicon/android-icon-192x192.png?v=3',
    retrievedAt: '2026-10-23',
  },
  subway: {
    path: '/brands/subway.png',
    sourceUrl: 'https://mysubway.pl/wp-content/uploads/2019/07/android-chrome-192x192.png',
    retrievedAt: '2026-10-23',
  },
  'da-grasso': {
    path: '/brands/da-grasso.png',
    sourceUrl: 'https://www.dagrasso.pl/css/ecom1/Themes/DAG/Images/Favicons/apple-touch-icon.png',
    retrievedAt: '2026-10-23',
  },
  dominos: {
    path: '/brands/dominos.png',
    sourceUrl: 'https://www.dominospizza.pl/assets/static/android-chrome-192x192.png',
    retrievedAt: '2026-10-23',
  },
  yasumi: {
    path: '/brands/yasumi.png',
    sourceUrl: 'https://gabinety.yasumi.pl/images/favicons/apple-touch-icon.png',
    retrievedAt: '2026-10-23',
  },
  depilconcept: {
    path: '/brands/depilconcept.png',
    sourceUrl: 'https://depilconcept.pl/wp-content/uploads/2019/02/icon_depil.png',
    retrievedAt: '2026-10-23',
  },
}

async function loadBrandImage(map: Map, brandId: string, asset: BrandAsset, signal: AbortSignal): Promise<string | null> {
  const imageId = BRAND_IMAGE_PREFIX + brandId
  if (map.hasImage(imageId)) return brandId
  let objectUrl: string | null = null
  try {
    const response = await fetch(asset.path, { signal, cache: 'force-cache' })
    if (!response.ok) return null
    objectUrl = URL.createObjectURL(await response.blob())
    const image = new Image()
    image.src = objectUrl
    await image.decode()
    if (signal.aborted || !image.naturalWidth || !image.naturalHeight) return null
    const canvas = document.createElement('canvas')
    canvas.width = 96
    canvas.height = 96
    const context = canvas.getContext('2d')
    if (!context) return null
    const [sourceX, sourceY, sourceWidth, sourceHeight] = asset.crop ?? [0, 0, image.naturalWidth, image.naturalHeight]
    const scale = 96 / Math.max(sourceWidth, sourceHeight)
    const width = sourceWidth * scale
    const height = sourceHeight * scale
    context.imageSmoothingQuality = 'high'
    context.drawImage(image, sourceX, sourceY, sourceWidth, sourceHeight, (96 - width) / 2, (96 - height) / 2, width, height)
    if (signal.aborted) return null
    map.addImage(imageId, context.getImageData(0, 0, 96, 96), { pixelRatio: 4 })
    return brandId
  } catch {
    return null
  } finally {
    if (objectUrl) URL.revokeObjectURL(objectUrl)
  }
}

export async function loadBrandImages(map: Map, signal: AbortSignal): Promise<string[]> {
  const results = await Promise.all(Object.entries(assets).map(([id, asset]) => loadBrandImage(map, id, asset, signal)))
  return results.filter((id): id is string => id !== null)
}
