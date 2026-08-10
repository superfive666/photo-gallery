/**
 * 客户端预压缩。
 *
 * 手机原图动辄 4~8MB，直接上传在移动网络下要等很久，而人脸识别根本不需要那么高的分辨率 ——
 * 检测器的输入是 640×640，长边压到 1600px 已经远超所需。
 *
 * 顺带的隐私收益：canvas 重绘会丢掉全部 EXIF（含 GPS）。服务端也会剥离一次，
 * 但在离开设备之前就丢掉更好。
 */

const MAX_EDGE = 1600
const QUALITY = 0.85

export interface CompressResult {
  file: File
  originalBytes: number
  compressedBytes: number
}

export async function compressImage(file: File): Promise<CompressResult> {
  // HEIC 等浏览器无法解码的格式：原样上传，由服务端处理
  const bitmap = await tryDecode(file)
  if (!bitmap) {
    return { file, originalBytes: file.size, compressedBytes: file.size }
  }

  const scale = Math.min(1, MAX_EDGE / Math.max(bitmap.width, bitmap.height))
  // 已经足够小的图不重新编码 —— 二次压缩只会白掉画质
  if (scale === 1 && file.size < 1_500_000) {
    bitmap.close()
    return { file, originalBytes: file.size, compressedBytes: file.size }
  }

  const width = Math.round(bitmap.width * scale)
  const height = Math.round(bitmap.height * scale)

  const canvas = document.createElement('canvas')
  canvas.width = width
  canvas.height = height
  const ctx = canvas.getContext('2d')
  if (!ctx) {
    bitmap.close()
    return { file, originalBytes: file.size, compressedBytes: file.size }
  }
  ctx.drawImage(bitmap, 0, 0, width, height)
  bitmap.close()

  const blob = await new Promise<Blob | null>((resolve) =>
    canvas.toBlob(resolve, 'image/jpeg', QUALITY),
  )
  if (!blob || blob.size >= file.size) {
    return { file, originalBytes: file.size, compressedBytes: file.size }
  }

  const compressed = new File([blob], renameToJpeg(file.name), {
    type: 'image/jpeg',
    lastModified: Date.now(),
  })
  return { file: compressed, originalBytes: file.size, compressedBytes: compressed.size }
}

async function tryDecode(file: File): Promise<ImageBitmap | null> {
  try {
    // createImageBitmap 会自动应用 EXIF 方向
    return await createImageBitmap(file, { imageOrientation: 'from-image' })
  } catch {
    return null
  }
}

function renameToJpeg(name: string): string {
  return name.replace(/\.[^.]+$/, '') + '.jpg'
}
