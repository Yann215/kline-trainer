export const fmtPct = (v: number | null | undefined, digits = 2) => v == null ? '—' : `${(v * 100).toFixed(digits)}%`
