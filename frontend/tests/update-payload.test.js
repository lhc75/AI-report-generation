import test from 'node:test'
import assert from 'node:assert'
import { reactive } from 'vue'
import { serializeInstallPayload } from '../src/updatePayload.js'

test('reactive update payload is serialized to a plain JSON string', () => {
  const state = reactive({
    asset: { name: 'AIReport-1.1.3-arm64.zip', url: 'https://github.com/x', size: 10 },
    assets: [{ kind: 'zip', name: 'AIReport-1.1.3-arm64.zip' }, { kind: 'dmg' }],
  })
  const text = serializeInstallPayload(state.asset, state.assets)
  assert.strictEqual(typeof text, 'string')
  assert.deepStrictEqual(JSON.parse(text), {
    asset: { name: 'AIReport-1.1.3-arm64.zip', url: 'https://github.com/x', size: 10 },
    assets: [{ kind: 'zip', name: 'AIReport-1.1.3-arm64.zip' }, { kind: 'dmg' }],
  })
})

test('missing asset and non-array assets are normalized', () => {
  const text = serializeInstallPayload(null, undefined)
  assert.deepStrictEqual(JSON.parse(text), { asset: null, assets: [] })
})
