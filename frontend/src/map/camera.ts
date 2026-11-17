export function focusPadding(width: number, height: number) {
  const horizontal = Math.min(64, Math.max(0, width) * 0.16)
  const vertical = Math.min(96, Math.max(0, height) * 0.18)
  return { top: vertical, right: horizontal, bottom: vertical, left: horizontal }
}

export function isFootprintClipped(points: { x: number; y: number }[], width: number, height: number) {
  if (width <= 0 || height <= 0) return false
  return points.some(({ x, y }) => Number.isFinite(x) && Number.isFinite(y) && (x < 0 || y < 0 || x > width || y > height))
}
