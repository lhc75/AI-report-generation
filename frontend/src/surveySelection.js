export const levels = [
  ['province', '省级'], ['city', '市州'], ['county', '区县'], ['school', '学校'],
]

export function inJurisdiction(organization, jurisdiction, byId) {
  for (let row = organization; row; row = byId.get(row.parent)) {
    if (row.id === jurisdiction) return true
  }
  return false
}

export function toggleOrganizations(selected, rows, value) {
  const next = new Set(selected)
  for (const row of rows) {
    if (value) next.add(row.id)
    else next.delete(row.id)
  }
  return [...next]
}
