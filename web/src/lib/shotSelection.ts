/** 评审页「主选 + 备选」的选择状态机（纯函数，方便单测）。
 *
 * 规则：
 *   · 同一候选不能同时是主选和备选 —— 赋新身份时自动卸下旧身份；
 *   · 再点一次同身份 = 取消该身份（toggle）。
 */

export interface ShotSelection {
  primaryId: string | null
  backupId: string | null
}

export const EMPTY_SELECTION: ShotSelection = { primaryId: null, backupId: null }

export function togglePick(
  sel: ShotSelection,
  candidateId: string,
  role: 'primary' | 'backup',
): ShotSelection {
  if (role === 'primary') {
    if (sel.primaryId === candidateId) return { ...sel, primaryId: null }
    return {
      primaryId: candidateId,
      backupId: sel.backupId === candidateId ? null : sel.backupId,
    }
  }
  if (sel.backupId === candidateId) return { ...sel, backupId: null }
  return {
    primaryId: sel.primaryId === candidateId ? null : sel.primaryId,
    backupId: candidateId,
  }
}
