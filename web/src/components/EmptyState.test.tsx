import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { EmptyState } from './EmptyState'

describe('EmptyState', () => {
  it('区分「没检测到人脸」与「没匹配上」', () => {
    // 这两种情况用户该做的事完全不同，文案不能混成一句「没有结果」
    const { unmount } = render(<EmptyState status="no_face" message={null} />)
    expect(screen.getByText('没有识别到人脸')).toBeInTheDocument()
    unmount()

    render(<EmptyState status="no_match" message={null} />)
    expect(screen.getByText('没有找到匹配的照片')).toBeInTheDocument()
  })

  it('优先使用服务端返回的说明文案', () => {
    render(<EmptyState status="no_match" message="照片库还在导入中" />)
    expect(screen.getByText('照片库还在导入中')).toBeInTheDocument()
  })
})
