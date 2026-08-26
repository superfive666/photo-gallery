import { describe, expect, it } from 'vitest'

import { EMPTY_SELECTION, togglePick } from './shotSelection'

describe('togglePick', () => {
  it('选主选、再选备选', () => {
    let sel = togglePick(EMPTY_SELECTION, 'a', 'primary')
    expect(sel).toEqual({ primaryId: 'a', backupId: null })
    sel = togglePick(sel, 'b', 'backup')
    expect(sel).toEqual({ primaryId: 'a', backupId: 'b' })
  })

  it('重复点同身份是取消', () => {
    let sel = togglePick(EMPTY_SELECTION, 'a', 'primary')
    sel = togglePick(sel, 'a', 'primary')
    expect(sel).toEqual(EMPTY_SELECTION)

    sel = togglePick(EMPTY_SELECTION, 'b', 'backup')
    sel = togglePick(sel, 'b', 'backup')
    expect(sel).toEqual(EMPTY_SELECTION)
  })

  it('同一候选不能身兼两职：备选升主选时卸下备选', () => {
    let sel = togglePick(EMPTY_SELECTION, 'a', 'backup')
    sel = togglePick(sel, 'a', 'primary')
    expect(sel).toEqual({ primaryId: 'a', backupId: null })
  })

  it('主选降为备选时卸下主选', () => {
    let sel = togglePick(EMPTY_SELECTION, 'a', 'primary')
    sel = togglePick(sel, 'a', 'backup')
    expect(sel).toEqual({ primaryId: null, backupId: 'a' })
  })

  it('换主选不影响已有备选', () => {
    let sel = togglePick(EMPTY_SELECTION, 'a', 'primary')
    sel = togglePick(sel, 'b', 'backup')
    sel = togglePick(sel, 'c', 'primary')
    expect(sel).toEqual({ primaryId: 'c', backupId: 'b' })
  })
})
