const shareCodePattern = /^[0-9a-f]{32}$/;

export function shareCode(search) {
  const codes = new URLSearchParams(search).getAll('via');
  return codes.length === 1 && shareCodePattern.test(codes[0]) ? codes[0] : null;
}

export function shareUrl(url, code) {
  if (typeof code !== 'string' || !shareCodePattern.test(code)) return url;
  const attributed = new URL(url);
  attributed.searchParams.set('via', code);
  return attributed.href;
}
