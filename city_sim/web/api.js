export async function request(path, data) {
  const response = await fetch('/api/v1' + path, data === undefined ? { cache: 'no-store' } : {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data)
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    const detail = Array.isArray(error.detail)
      ? error.detail.map(item => item.msg).join(' · ')
      : error.detail;
    throw new Error(detail || `HTTP ${response.status}`);
  }
  return response.json();
}

export const $ = id => document.getElementById(id);
export const empty = () => ({ type: 'FeatureCollection', features: [] });
export const format = (value, digits = 1) => Number(value).toLocaleString('ru', {
  maximumFractionDigits: digits
});
