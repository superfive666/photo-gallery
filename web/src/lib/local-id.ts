/**
 * 本地列表项 id。
 *
 * 不用 crypto.randomUUID：它只在安全上下文（HTTPS / localhost）里存在，
 * 内网用 http://192.168.x.x:8080 访问时直接是 undefined —— 上传第一张图就抛
 * TypeError。这个 id 只用来做 React key 和撤销单张的定位，不需要密码学强度，
 * 也不发给服务端。
 */

let counter = 0

export function localId(): string {
  counter += 1
  return `${Date.now().toString(36)}-${counter}-${Math.random().toString(36).slice(2, 8)}`
}
