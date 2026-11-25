import assert from 'node:assert/strict'
import test from 'node:test'
import { inJurisdiction, toggleOrganizations } from '../src/surveySelection.js'

const province = { id: 1, parent: null }
const city = { id: 2, parent: 1 }
const county = { id: 3, parent: 2 }
const school = { id: 4, parent: 3 }
const otherCity = { id: 5, parent: 1 }
const otherSchool = { id: 6, parent: 5 }
const byId = new Map([province, city, county, school, otherCity, otherSchool].map(row => [row.id, row]))

test('a city selection is independent of its descendants and mixed selections survive filters', () => {
  const cityOnly = toggleOrganizations([], [city], true)
  assert.deepEqual(cityOnly, [2])
  const mixed = toggleOrganizations(cityOnly, [otherSchool, school, school], true)
  assert.deepEqual(mixed, [2, 6, 4])
  assert.deepEqual(toggleOrganizations(mixed, [school], false), [2, 6])
})

test('jurisdiction matching follows organization IDs instead of labels', () => {
  assert.equal(inJurisdiction(school, city.id, byId), true)
  assert.equal(inJurisdiction(county, province.id, byId), true)
  assert.equal(inJurisdiction(otherSchool, city.id, byId), false)
  assert.equal(inJurisdiction(city, school.id, byId), false)
})
