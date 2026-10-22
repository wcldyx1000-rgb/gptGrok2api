// Ported from yukkcat/chatgpt2api (AGPL-3.0-only): resolveLoginRedirect.

/**
 * 校验登录后的回跳地址：只接受站内路径。
 * - 必须是字符串且以单个 "/" 开头（拒绝 "//host"、"/\host"、"http://..."）
 * - 不允许回跳到 /login 本身
 * - 不允许控制字符
 * 不合法时返回 ''，调用方回落到默认首页。
 */
export function resolveLoginRedirect(value: unknown): string {
  const raw = Array.isArray(value) ? value[0] : value
  if (typeof raw !== 'string') return ''
  const target = raw.trim()
  if (!target.startsWith('/') || target.startsWith('//') || target.startsWith('/\\')) return ''
  // eslint-disable-next-line no-control-regex
  if (/[\u0000-\u001f\u007f]/.test(target)) return ''
  if (/^\/login(?:[/?#]|$)/.test(target)) return ''
  return target
}
