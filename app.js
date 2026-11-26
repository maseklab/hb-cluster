// ==============================================================================
// HB CLUSTER MANAGER - SPA FRONTEND LOGIC (app.js)
// ==============================================================================

const { createApp } = Vue;
// Sekce Nastavení – JEDINÝ seznam (používá setSettingsSection i obnova sekce po načtení stavu v fetchState)
const SETTINGS_SECTIONS = ['general', 'appearance', 'video', 'smb', 'binaries', 'security', 'about'];
const RUNNING_STATES = ['IN_PROGRESS', 'STAGING', 'PREMUX', 'COPYING'];

// Vektorové glyfy pro faviconu (jednotky: box 100×140, inkoust vždy přesně y = 10…130 -> střed y = 70).
// Díky tomu je číslo geometricky přesně vystředěné (stejná mezera nahoře i dole) bez ohledu na fonty prohlížeče.
const FAV_GLYPHS = {
  '0': { x0: 12, x1: 88, d: 'M50 10 C80 10 88 38 88 70 C88 102 80 130 50 130 C20 130 12 102 12 70 C12 38 20 10 50 10 Z' },
  '1': { x0: 28, x1: 58, d: 'M28 34 L58 10 L58 130' },
  '2': { x0: 14, x1: 88, d: 'M16 38 C18 20 32 10 50 10 C70 10 86 22 86 42 C86 64 66 80 14 130 L88 130' },
  '3': { x0: 12, x1: 88, d: 'M16 28 C22 16 34 10 50 10 C70 10 84 22 84 38 C84 56 68 68 46 68 C70 68 88 80 88 100 C88 120 70 130 50 130 C32 130 18 122 12 110' },
  '4': { x0: 10, x1: 90, d: 'M66 130 L66 10 L10 96 L90 96' },
  '5': { x0: 12, x1: 88, d: 'M84 10 L24 10 L18 64 C28 56 38 54 50 54 C72 54 88 70 88 92 C88 116 70 130 48 130 C32 130 18 122 12 112' },
  '6': { x0: 12, x1: 88, d: 'M78 18 C70 12 60 10 52 10 C26 10 12 36 12 76 C12 110 28 130 52 130 C74 130 88 114 88 92 C88 70 72 56 52 56 C34 56 18 66 12 82' },
  '7': { x0: 12, x1: 88, d: 'M12 10 L88 10 L40 130' },
  '8': { x0: 12, x1: 88, d: 'M50 68 C30 68 18 56 18 40 C18 22 32 10 50 10 C68 10 82 22 82 40 C82 56 70 68 50 68 C28 68 12 82 12 100 C12 118 28 130 50 130 C72 130 88 118 88 100 C88 82 72 68 50 68 Z' },
  '9': { x0: 12, x1: 88, d: 'M22 122 C30 128 40 130 48 130 C74 130 88 104 88 64 C88 30 72 10 48 10 C26 10 12 26 12 48 C12 70 28 84 48 84 C66 84 82 74 88 58' },
  '!': { x0: 50, x1: 50, d: 'M50 10 L50 90 M50 130 L50 130.01' }
};

// Symetrická ležatá osmička (Bernoulliho lemniskáta) vystředěná přesně v (cx, cy).
function favLemniscatePath(cx, cy, halfW, halfH, n = 72) {
  let d = '';
  for (let i = 0; i <= n; i++) {
    const t = (2 * Math.PI * i) / n;
    const s = Math.sin(t), c = Math.cos(t), den = 1 + s * s;
    const x = cx + (c / den) * halfW;
    const y = cy + ((s * c / den) / 0.353553) * halfH;
    d += (i === 0 ? 'M' : 'L') + x.toFixed(2) + ' ' + y.toFixed(2) + ' ';
  }
  return d + 'Z';
}

// Vykreslí řetězec vektorových glyfů přesně vystředěný kolem (128, 128) s výškou inkoustu inkH px.
function favGlyphRow(txt, inkH, sw, gapPx, fill, scaleX = 1.0) {
  const k = inkH / (120 + sw);
  const kx = k * scaleX;
  const glyphs = [...String(txt)].map(ch => FAV_GLYPHS[ch]).filter(Boolean);
  if (!glyphs.length) return '';
  const widths = glyphs.map(g => (g.x1 - g.x0 + sw) * kx);
  const total = widths.reduce((a, b) => a + b, 0) + gapPx * (glyphs.length - 1);
  let left = 128 - total / 2;
  let out = '';
  glyphs.forEach((g, i) => {
    const tx = left + (sw / 2 - g.x0) * kx;
    const ty = 128 - 70 * k;
    const tr = `translate(${tx.toFixed(3)} ${ty.toFixed(3)}) scale(${kx.toFixed(5)} ${k.toFixed(5)})`;
    out += `<path transform="${tr}" d="${g.d}" fill="none" stroke="rgba(0,0,0,0.42)" stroke-width="${sw + 10}" stroke-linecap="round" stroke-linejoin="round"/>`;
    out += `<path transform="${tr}" d="${g.d}" fill="none" stroke="${fill}" stroke-width="${sw}" stroke-linecap="round" stroke-linejoin="round"/>`;
    left += widths[i] + gapPx;
  });
  return out;
}

const SVG_ICONS = {
  'list': '<path stroke-linecap="round" stroke-linejoin="round" d="M3.75 6.75h16.5M3.75 12h16.5m-16.5 5.25h16.5"/>',
  'chart': '<path stroke-linecap="round" stroke-linejoin="round" d="M3 13.125C3 12.504 3.504 12 4.125 12h2.25c.621 0 1.125.504 1.125 1.125v6.75C7.5 20.496 6.996 21 6.375 21h-2.25A1.125 1.125 0 013 19.875v-6.75zM9.75 8.625c0-.621.504-1.125 1.125-1.125h2.25c.621 0 1.125.504 1.125 1.125v11.25c0 .621-.504 1.125-1.125 1.125h-2.25a1.125 1.125 0 01-1.125-1.125V8.625zM16.5 4.125c0-.621.504-1.125 1.125-1.125h2.25C20.496 3 21 3.504 21 4.125v15.75c0 .621-.504 1.125-1.125 1.125h-2.25a1.125 1.125 0 01-1.125-1.125V4.125z"/>',
  'clock': '<path stroke-linecap="round" stroke-linejoin="round" d="M12 6v6h4.5m4.5 0a9 9 0 11-18 0 9 9 0 0118 0z"/>',
  'moon': '<path stroke-linecap="round" stroke-linejoin="round" d="M21.752 15.002A9.718 9.718 0 0118 15.75c-5.385 0-9.75-4.365-9.75-9.75 0-1.33.266-2.597.748-3.752A9.753 9.753 0 003 11.25C3 16.635 7.365 21 12.75 21a9.753 9.753 0 009.002-5.998z"/>',
  'upload': '<path stroke-linecap="round" stroke-linejoin="round" d="M3 16.5v2.25A2.25 2.25 0 005.25 21h13.5A2.25 2.25 0 0021 18.75V16.5m-13.5-9L12 3m0 0l4.5 4.5M12 3v13.5"/>',
  'download': '<path stroke-linecap="round" stroke-linejoin="round" d="M3 16.5v2.25A2.25 2.25 0 005.25 21h13.5A2.25 2.25 0 0021 18.75V16.5M16.5 12L12 16.5m0 0L7.5 12m4.5 4.5V3"/>',
  'cpu': '<path stroke-linecap="round" stroke-linejoin="round" d="M8.25 3v1.5M4.5 8.25H3m18 0h-1.5M4.5 12H3m18 0h-1.5m-15 3.75H3m18 0h-1.5M8.25 19.5V21M12 3v1.5m0 15V21m3.75-18v1.5m0 15V21m-9-1.5h10.5a2.25 2.25 0 002.25-2.25V6.75a2.25 2.25 0 00-2.25-2.25H6.75A2.25 2.25 0 004.5 6.75v10.5a2.25 2.25 0 002.25 2.25zm.75-12h9v9h-9v-9z"/>',
  'copy': '<path stroke-linecap="round" stroke-linejoin="round" d="M15.75 17.25v3.375c0 .621-.504 1.125-1.125 1.125h-9.75a1.125 1.125 0 01-1.125-1.125V7.875c0-.621.504-1.125 1.125-1.125H6.75a9.06 9.06 0 011.5.124m7.5 10.376h3.375c.621 0 1.125-.504 1.125-1.125V11.25c0-4.46-3.243-8.161-7.5-8.876a9.06 9.06 0 00-1.5-.124H9.375c-.621 0-1.125.504-1.125 1.125v3.5m7.5 10.375H9.375a1.125 1.125 0 01-1.125-1.125v-9.25m12 6.625v-1.875a3.375 3.375 0 00-3.375-3.375h-1.5a1.125 1.125 0 01-1.125-1.125v-1.5a3.375 3.375 0 00-3.375-3.375H9.75"/>',
  'dots': '<path stroke-linecap="round" stroke-linejoin="round" d="M6.75 12a.75.75 0 11-1.5 0 .75.75 0 011.5 0zM12.75 12a.75.75 0 11-1.5 0 .75.75 0 011.5 0zM18.75 12a.75.75 0 11-1.5 0 .75.75 0 011.5 0z"/>',
  'cog': '<path stroke-linecap="round" stroke-linejoin="round" d="M10.343 3.94c.09-.542.56-.94 1.11-.94h1.093c.55 0 1.02.398 1.11.94l.149.894c.07.424.384.764.78.93.398.164.855.142 1.205-.108l.737-.527a1.125 1.125 0 011.45.12l.773.774c.39.389.44 1.002.12 1.45l-.527.737c-.25.35-.272.806-.107 1.204.165.397.505.71.93.78l.893.15c.543.09.94.56.94 1.109v1.094c0 .55-.397 1.02-.94 1.11l-.893.149c-.425.07-.765.383-.93.78-.165.398-.143.854.107 1.204l.527.738c.32.447.269 1.06-.12 1.45l-.774.773a1.125 1.125 0 01-1.449.12l-.738-.527c-.35-.25-.806-.272-1.203-.107-.397.165-.71.505-.781.929l-.149.894c-.09.542-.56.94-1.11.94h-1.094c-.55 0-1.019-.398-1.11-.94l-.148-.894c-.071-.424-.384-.764-.781-.93-.398-.164-.854-.142-1.204.108l-.738.527c-.447.32-1.06.269-1.45-.12l-.773-.774a1.125 1.125 0 01-.12-1.45l.527-.737c.25-.35.273-.806.108-1.204-.165-.397-.505-.71-.93-.78l-.894-.15c-.542-.09-.94-.56-.94-1.109v-1.094c0 .55.398-1.02.94-1.11l.894-.149c.424-.07.765-.383.93-.78.165-.398.143-.854-.107-1.204l-.527-.738a1.125 1.125 0 01.12-1.45l.773-.773a1.125 1.125 0 011.45-.12l.737.527c.35.25.807.272 1.204.107.397-.165.71-.505.78-.929l.15-.894z"/><path stroke-linecap="round" stroke-linejoin="round" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z"/>',
  'doc': '<path stroke-linecap="round" stroke-linejoin="round" d="M19.5 14.25v-2.625a3.375 3.375 0 00-3.375-3.375h-1.5A1.125 1.125 0 0113.5 7.125v-1.5a3.375 3.375 0 00-3.375-3.375H8.25m0 12.75h7.5m-7.5 3H12M10.5 2.25H5.625c-.621 0-1.125.504-1.125 1.125v17.25c0 .621.504 1.125 1.125 1.125h12.75c0 .621 1.125-.504 1.125-1.125V11.25a9 9 0 00-9-9z"/>',
  'search': '<path stroke-linecap="round" stroke-linejoin="round" d="M21 21l-5.197-5.197m0 0A7.5 7.5 0 105.196 5.196a7.5 7.5 0 0010.607 10.607z"/>',
  'pause': '<path stroke-linecap="round" stroke-linejoin="round" d="M15.75 5.25v13.5m-7.5-13.5v13.5"/>',
  'play': '<path stroke-linecap="round" stroke-linejoin="round" d="M5.25 5.653c0-.856.917-1.398 1.667-.986l11.54 6.348a1.125 1.125 0 010 1.971l-11.54 6.347a1.125 1.125 0 01-1.667-.985V5.653z"/>',
  'pause-circle': '<path stroke-linecap="round" stroke-linejoin="round" d="M14.25 9v6m-4.5 0V9M21 12a9 9 0 11-18 0 9 9 0 0118 0z"/>',
  'play-circle': '<path stroke-linecap="round" stroke-linejoin="round" d="M21 12a9 9 0 11-18 0 9 9 0 0118 0z"/><path stroke-linecap="round" stroke-linejoin="round" d="M15.91 11.672a.375.375 0 010 .656l-5.603 3.113a.375.375 0 01-.557-.328V8.887c0-.286.307-.466.557-.327l5.603 3.112z"/>',
  'gate-open': '<path stroke-linecap="round" stroke-linejoin="round" d="M4.5 4.5v15m15-15v15M8.25 12h7.5m0 0l-3-3m3 3l-3 3"/>',
  'gate-closed': '<path stroke-linecap="round" stroke-linejoin="round" d="M4.5 4.5v15m15-15v15M4.5 9.75h15M4.5 14.25h15M9 9.75l6 4.5"/>',
  'intake': '<path stroke-linecap="round" stroke-linejoin="round" d="M9 3.75H6.912a2.25 2.25 0 00-2.15 1.588L2.35 13.177a2.25 2.25 0 00-.1.661V18a2.25 2.25 0 002.25 2.25h15A2.25 2.25 0 0021.75 18v-4.162c0-.224-.034-.447-.1-.661L19.24 5.338a2.25 2.25 0 00-2.15-1.588H15M2.25 13.5h3.86a2.25 2.25 0 012.012 1.244l.256.512a2.25 2.25 0 002.013 1.244h3.218a2.25 2.25 0 002.013-1.244l.256-.512a2.25 2.25 0 012.013-1.244h3.859M12 3v8.25m0 0l-3-3m3 3l3-3"/>',
  'refresh': '<path stroke-linecap="round" stroke-linejoin="round" d="M16.023 9.348h4.992v-.001M2.985 19.644v-4.992m0 0h4.992m-4.993 0l3.181 3.183a8.25 8.25 0 0013.803-3.7M4.031 9.865a8.25 8.25 0 0113.803-3.7l3.181 3.182m0-4.991v4.99"/>',
  'info': '<path stroke-linecap="round" stroke-linejoin="round" d="M11.25 11.25l.041-.02a.75.75 0 011.063.852l-.708 2.836a.75.75 0 001.063.853l.041-.021M21 12a9 9 0 11-18 0 9 9 0 0118 0zm-9-3.75h.008v.008H12V8.25z"/>',
  'stop-sign': '<polygon stroke-linecap="round" stroke-linejoin="round" points="7.86 2 16.14 2 22 7.86 22 16.14 16.14 22 7.86 22 2 16.14 2 7.86 7.86 2"/><path stroke-linecap="round" stroke-linejoin="round" d="M9.5 9.5l5 5m0-5l-5 5"/>',
  'trash': '<path stroke-linecap="round" stroke-linejoin="round" d="M14.74 9l-.346 9m-4.788 0L9.26 9m9.968-3.21c.342.052.682.107 1.022.166m-1.022-.165L18.16 19.673a2.25 2.25 0 01-2.244 2.077H8.084a2.25 2.25 0 01-2.244-2.077L4.772 5.79m14.456 0a48.108 48.108 0 00-3.478-.397m-12 .562c.34-.059.68-.114 1.022-.165m0 0a48.11 48.11 0 013.478-.397m7.5 0v-.916c0-1.18-.91-2.164-2.09-2.201a51.964 51.964 0 00-3.32 0c-1.18.037-2.09 1.022-2.09 2.201v.916m7.5 0a48.667 48.667 0 00-7.5 0"/>',
  'chevron-up': '<path stroke-linecap="round" stroke-linejoin="round" d="M4.5 15.75l7.5-7.5 7.5 7.5"/>',
  'chevron-down': '<path stroke-linecap="round" stroke-linejoin="round" d="M19.5 8.25l-7.5 7.5-7.5-7.5"/>',
  'chevron-double-up': '<path stroke-linecap="round" stroke-linejoin="round" d="M4.5 12.75l7.5-7.5 7.5 7.5m-15 6l7.5-7.5 7.5 7.5"/>',
  'chevron-double-down': '<path stroke-linecap="round" stroke-linejoin="round" d="M19.5 5.25l-7.5 7.5-7.5-7.5m15 6l-7.5 7.5-7.5-7.5"/>',
  'bolt': '<path stroke-linecap="round" stroke-linejoin="round" d="M3.75 13.5l10.5-11.25L12 10.5h8.25L9.75 21.75 12 13.5H3.75z"/>',
  'film': '<path stroke-linecap="round" stroke-linejoin="round" d="M3.375 19.5h17.25m-17.25 0a1.125 1.125 0 01-1.125-1.125M3.375 19.5h1.5C5.496 19.5 6 18.996 6 18.375m-3.75 0V5.625m0 12.75v-1.5c0-.621.504-1.125 1.125-1.125m18.375 2.625V5.625m0 12.75c0 .621-.504 1.125-1.125 1.125m1.125-1.125v-1.5c0-.621-.504-1.125-1.125-1.125m0 3.75h-1.5A1.125 1.125 0 0118 18.375M20.625 4.5H3.375m17.25 0c.621 0 1.125.504 1.125 1.125M20.625 4.5h-1.5C18.504 4.5 18 5.004 18 5.625m3.75 0v1.5c0 .621-.504 1.125-1.125 1.125M3.375 4.5c-.621 0-1.125.504-1.125 1.125M3.375 4.5h1.5C5.496 4.5 6 5.004 6 5.625m-3.75 0v1.5c0 .621.504 1.125 1.125 1.125m0 0h1.5m-1.5 0c-.621 0-1.125.504-1.125 1.125v1.5c0 .621.504 1.125 1.125 1.125m1.5-3.75C5.496 8.25 6 7.746 6 7.125v-1.5M4.875 8.25C5.496 8.25 6 8.754 6 9.375v1.5m0-5.25v5.25m0-5.25C6 5.004 6.504 4.5 7.125 4.5h9.75c.621 0 1.125.504 1.125 1.125m1.125 2.625h1.5m-1.5 0A1.125 1.125 0 0118 7.125v-1.5m1.125 2.625c-.621 0-1.125.504-1.125 1.125v1.5m2.625-2.625c.621 0 1.125.504 1.125 1.125v1.5c0 .621-.504 1.125-1.125 1.125M18 5.625v5.25M7.125 12h9.75m-9.75 0A1.125 1.125 0 016 10.875M7.125 12C6.504 12 6 12.504 6 13.125m0-2.25C6 11.496 5.496 12 4.875 12M18 10.875c0 .621-.504 1.125-1.125 1.125M18 10.875c0 .621.504 1.125 1.125 1.125m-2.25 0c.621 0 1.125.504 1.125 1.125m-12 5.25v-5.25m0 5.25c0 .621.504 1.125 1.125 1.125h9.75c.621 0 1.125-.504 1.125-1.125m-12 0v-1.5c0-.621-.504-1.125-1.125-1.125M18 18.375v-5.25m0 5.25v-1.5c0-.621.504-1.125 1.125-1.125M18 13.125v1.5c0 .621.504 1.125 1.125 1.125M18 13.125c0-.621.504-1.125 1.125-1.125M6 13.125v1.5c0 .621-.504 1.125-1.125 1.125M6 13.125C6 12.504 5.496 12 4.875 12m-1.5 0h1.5m-1.5 0c-.621 0-1.125.504-1.125 1.125v1.5c0 .621.504 1.125 1.125 1.125M19.125 12h1.5m0 0c.621 0 1.125.504 1.125 1.125v1.5c0 .621-.504 1.125-1.125 1.125m-17.25 0h1.5m14.25 0h1.5"/>',
  'alert': '<path stroke-linecap="round" stroke-linejoin="round" d="M12 9v3.75m-9.303 3.376c-.866 1.5.217 3.374 1.948 3.374h14.71c1.73 0 2.813-1.874 1.948-3.374L13.949 3.378c-.866-1.5-3.032-1.5-3.898 0L2.697 16.126zM12 15.75h.007v.008H12v-.008z"/>',
  'arrow-up-circle': '<path stroke-linecap="round" stroke-linejoin="round" d="M15 11.25l-3-3m0 0l-3 3m3-3v7.5M21 12a9 9 0 11-18 0 9 9 0 0118 0z"/>',
  'grip': '<path stroke-linecap="round" stroke-linejoin="round" d="M8.25 6.75h.008v.008H8.25V6.75zm0 5.25h.008v.008H8.25V12zm0 5.25h.008v.008H8.25v-.008zm7.5-10.5h.008v.008h-.008V6.75zm0 5.25h.008v.008h-.008V12zm0 5.25h.008v.008h-.008v-.008z"/>',
  'coffee': '<path stroke-linecap="round" stroke-linejoin="round" d="M17 9h1.5a3.5 3.5 0 010 7H17M3 9h14v7a5 5 0 01-5 5H8a5 5 0 01-5-5V9z"/><path stroke-linecap="round" stroke-linejoin="round" d="M7.5 6c0-1.2 1.2-1.8 1.2-3M12.5 6c0-1.2 1.2-1.8 1.2-3"/>',
  'beer': '<path stroke-linecap="round" stroke-linejoin="round" d="M5 9h11v10a2 2 0 01-2 2H7a2 2 0 01-2-2V9z"/><path stroke-linecap="round" stroke-linejoin="round" d="M16 11h2.5a2 2 0 012 2v3a2 2 0 01-2 2H16"/><path stroke-linecap="round" stroke-linejoin="round" d="M5 9c-1 0-1.5-.8-1.5-1.8C3.5 5.8 4.8 4.8 6 5c.6-1.4 2-2.2 3.5-2 1.2-.8 3-.5 3.8.7 1.3-.3 2.7.5 3 1.8 1 .3 1.2 1.5.7 2.5-.3.6-.7 1-1 1"/><path stroke-linecap="round" stroke-linejoin="round" d="M8.5 12.5v5.5M12.5 12.5v5.5"/>',
  'shield': '<path stroke-linecap="round" stroke-linejoin="round" d="M9 12.75L11.25 15 15 9.75m-3-7.036A11.959 11.959 0 013.598 6 11.99 11.99 0 003 9.749c0 5.592 3.824 10.29 9 11.623 5.176-1.332 9-6.03 9-11.622 0-1.31-.21-2.571-.598-3.751h-.152c-3.196 0-6.1-1.248-8.25-3.285z"/>',
  'logout': '<path stroke-linecap="round" stroke-linejoin="round" d="M15.75 9V5.25A2.25 2.25 0 0013.5 3h-6a2.25 2.25 0 00-2.25 2.25v13.5A2.25 2.25 0 007.5 21h6a2.25 2.25 0 002.25-2.25V15M12 9l-3 3m0 0l3 3m-3-3h12.75"/>',
  'key': '<path stroke-linecap="round" stroke-linejoin="round" d="M15.75 5.25a3 3 0 013 3m3 0a6 6 0 01-7.029 5.912c-.563-.097-1.159.026-1.563.43L10.5 17.25H8.25v2.25H6v2.25H2.25v-2.818c0-.597.237-1.17.659-1.591l6.499-6.499c.404-.404.527-1 .43-1.563A6 6 0 1121.75 8.25z"/>',
  'eye': '<path stroke-linecap="round" stroke-linejoin="round" d="M2.036 12.322a1.012 1.012 0 010-.639C3.423 7.51 7.36 4.5 12 4.5c4.638 0 8.573 3.007 9.963 7.178.07.207.07.431 0 .639C20.577 16.49 16.64 19.5 12 19.5c-4.638 0-8.573-3.007-9.963-7.178z"/><path stroke-linecap="round" stroke-linejoin="round" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z"/>',
  'eye-off': '<path stroke-linecap="round" stroke-linejoin="round" d="M3.98 8.223A10.477 10.477 0 001.934 12C3.226 16.338 7.244 19.5 12 19.5c.993 0 1.953-.138 2.863-.395M6.228 6.228A10.45 10.45 0 0112 4.5c4.756 0 8.773 3.162 10.065 7.498a10.523 10.523 0 01-4.293 5.774M6.228 6.228L3 3m3.228 3.228l3.65 3.65m7.894 7.894L21 21m-3.228-3.228l-3.65-3.65m0 0a3 3 0 10-4.243-4.243m4.242 4.242L9.88 9.88"/>',
  'save': '<path stroke-linecap="round" stroke-linejoin="round" d="M17.593 3.322c1.1.128 1.907 1.077 1.907 2.185V21L12 17.25 4.5 21V5.507c0-1.108.806-2.057 1.907-2.185a48.507 48.507 0 0111.186 0z"/>',
  'plus': '<path stroke-linecap="round" stroke-linejoin="round" d="M12 4.5v15m7.5-7.5h-15"/>',
  'x': '<path stroke-linecap="round" stroke-linejoin="round" d="M6 18L18 6M6 6l12 12"/>',
  'sliders': '<path stroke-linecap="round" stroke-linejoin="round" d="M10.5 6h9.75M10.5 6a1.5 1.5 0 11-3 0m3 0a1.5 1.5 0 10-3 0M3.75 6H7.5m3 12h9.75m-9.75 0a1.5 1.5 0 01-3 0m3 0a1.5 1.5 0 00-3 0m-3.75 0H7.5m9-6h3.75m-3.75 0a1.5 1.5 0 01-3 0m3 0a1.5 1.5 0 00-3 0m-9.75 0h9.75"/>',
  'folder': '<path stroke-linecap="round" stroke-linejoin="round" d="M2.25 12.75V12A2.25 2.25 0 014.5 9.75h15A2.25 2.25 0 0121.75 12v.75m-8.69-6.44l-2.12-2.12a1.5 1.5 0 00-1.061-.44H4.5A2.25 2.25 0 002.25 6v12a2.25 2.25 0 002.25 2.25h15A2.25 2.25 0 0021.75 18V9a2.25 2.25 0 00-2.25-2.25h-5.379a1.5 1.5 0 01-1.06-.44z"/>',
  'wrench': '<path stroke-linecap="round" stroke-linejoin="round" d="M11.42 15.17L17.25 21A2.652 2.652 0 0021 17.25l-5.877-5.877M11.42 15.17l2.496-3.03c.317-.384.74-.626 1.208-.766M11.42 15.17l-4.655 5.653a2.548 2.548 0 11-3.586-3.586l6.837-5.63m5.108-.233c.55-.164 1.163-.188 1.743-.14a4.5 4.5 0 004.486-6.336l-3.276 3.277a3.004 3.004 0 01-2.25-2.25l3.276-3.276a4.5 4.5 0 00-6.336 4.486c.091 1.076-.071 2.264-.904 2.95l-.102.085m-1.745 1.437L5.909 7.5H4.5L2.25 3.75l1.5-1.5L7.5 4.5v1.409l4.26 4.26m-1.745 1.437l1.745-1.437m6.615 8.206L15.75 15.75M4.867 19.125h.008v.008h-.008v-.008z"/>',
  'server': '<rect x="2" y="3" width="20" height="8" rx="2" stroke-linecap="round" stroke-linejoin="round"/><rect x="2" y="13" width="20" height="8" rx="2" stroke-linecap="round" stroke-linejoin="round"/><line x1="6" y1="7" x2="6.01" y2="7" stroke-width="2.5" stroke-linecap="round"/><line x1="6" y1="17" x2="6.01" y2="17" stroke-width="2.5" stroke-linecap="round"/>',
  'windows': '<path d="M2 4.5l8-1.1v7.6H2V4.5zm9-1.3L22 1.5v9.5H11V3.2zM2 12.5h8v7.6l-8-1.1V12.5zm9 0h11v9.5l-11-1.7V12.5z" fill="currentColor" stroke="none"/>',
  'desktop': '<path stroke-linecap="round" stroke-linejoin="round" d="M9 17.25v1.007a3 3 0 01-.879 2.122L7.5 21h9l-.621-.621A3 3 0 0115 18.257V17.25m6-12V15a2.25 2.25 0 01-2.25 2.25H5.25A2.25 2.25 0 013 15V5.25m18 0A2.25 2.25 0 0018.75 3H5.25A2.25 2.25 0 003 5.25m18 0V12a2.25 2.25 0 01-2.25 2.25H5.25A2.25 2.25 0 013 12V5.25"/>',
  'compass': '<circle cx="12" cy="12" r="9.5"/><polygon points="16.24 7.76 14.12 14.12 7.76 16.24 9.88 9.88" fill="currentColor" fill-opacity="0.35"/><circle cx="12" cy="12" r="1.5" fill="currentColor"/><line x1="12" y1="2.5" x2="12" y2="4.5"/><line x1="12" y1="19.5" x2="12" y2="21.5"/><line x1="2.5" y1="12" x2="4.5" y2="12"/><line x1="19.5" y1="12" x2="21.5" y2="12"/>',
  'terminal': '<path stroke-linecap="round" stroke-linejoin="round" d="M6.75 7.5l3 2.25-3 2.25m4.5 0h3m-9 8.25h13.5A2.25 2.25 0 0021 18V6a2.25 2.25 0 00-2.25-2.25H5.25A2.25 2.25 0 003 6v12a2.25 2.25 0 002.25 2.25z"/>',
  'arrow-up': '<path stroke-linecap="round" stroke-linejoin="round" d="M12 19.5v-15m0 0l-6.75 6.75M12 4.5l6.75 6.75"/>',
  'docker': '<rect x="4" y="9" width="2.6" height="2.2" rx="0.3" fill="currentColor"/><rect x="7.6" y="9" width="2.6" height="2.2" rx="0.3" fill="currentColor"/><rect x="11.2" y="9" width="2.6" height="2.2" rx="0.3" fill="currentColor"/><rect x="14.8" y="9" width="2.6" height="2.2" rx="0.3" fill="currentColor"/><rect x="7.6" y="6" width="2.6" height="2.2" rx="0.3" fill="currentColor"/><rect x="11.2" y="6" width="2.6" height="2.2" rx="0.3" fill="currentColor"/><rect x="11.2" y="3" width="2.6" height="2.2" rx="0.3" fill="currentColor"/><path stroke-linecap="round" stroke-linejoin="round" d="M2.5 12.5h15.5c.8 0 1.5-.4 2-1.1.7.2 1.6.1 2.2-.4-.3-.8-1.1-1.3-2-1.3-.2-.7-.7-1.4-1.4-1.7-.4.6-.5 1.4-.3 2.1M2.5 12.5c-.3 3.5 2.2 6.5 6.5 6.5 5 0 8.5-2.5 9.7-6.5"/>',
  'tux': '<path stroke-linecap="round" stroke-linejoin="round" d="M12 3c-2.2 0-3.5 1.8-3.5 4 0 1.2.4 2.2 1 3-.5 1-2 2.5-3.5 4-1.2 1.2-1 3.5.5 4.5 1.5 1 4 .5 5.5.5s4 .5 5.5-.5c1.5-1 1.7-3.3.5-4.5-1.5-1.5-3-3-3.5-4 .6-.8 1-1.8 1-3 0-2.2-1.3-4-3.5-4z"/><circle cx="10" cy="7" r="0.75" fill="currentColor"/><circle cx="14" cy="7" r="0.75" fill="currentColor"/><path stroke-linecap="round" stroke-linejoin="round" d="M11 9.5c.3.3.7.5 1 .5s.7-.2 1-.5"/>',
  'scale': '<path stroke-linecap="round" stroke-linejoin="round" d="M12 3v17.25m0 0c-1.472 0-2.882.265-4.185.75M12 20.25c1.472 0 2.882.265 4.185.75M18.75 4.97A48.416 48.416 0 0012 4.5c-2.291 0-4.545.16-6.75.47m13.5 0c1.01.143 2.01.317 3 .52m-3-.52l2.62 10.726c.122.499-.106 1.028-.589 1.202a5.988 5.988 0 01-2.031.352 5.988 5.988 0 01-2.031-.352c-.483-.174-.711-.703-.59-1.202L18.75 4.971zm-16.5.52c.99-.203 1.99-.377 3-.52m0 0l2.62 10.726c.122.499-.106 1.028-.589 1.202a5.989 5.989 0 01-2.031.352 5.989 5.989 0 01-2.031-.352c-.483-.174-.711-.703-.59-1.202L5.25 4.971z"/>'
};

createApp({
  data() {
    const pagInit = localStorage.getItem('hb_enable_pagination') === 'true';
    return {
      windowWidth: typeof window !== 'undefined' ? window.innerWidth : 1280,
      secForm: { new1: '', new2: '', cur: '', remember: true, logoutOthers: true, disablePw: '', busy: false },
      secNet: { home_bypass: false, networksText: '', behind_proxy: false, proxiesText: '', trust_cf_header: false, public_stats: true },
      secDevices: [],
      secPk: { usable: false, https: false, lib: true, rp_id: '', items: [] },   // passkeys (krok 3)
      secNetUserToggled: null,       // karta „Síť a přístup“: null = auto (otevřena jen při proxy_hint), true/false = volba uživatele
      secConfirm: '',               // dvojí potvrzení důležitých tlačítek Zabezpečení (vždy, bez ohledu na volbu dvojkliku)
      secHasSession: false,          // přihlášen přes relaci (ne přes domácí síť) → nabídnout Odhlásit se
      mobileSetList: true,           // mobil: Nastavení začíná seznamem sekcí (rozcestník), po výběru jedna sekce s „‹ Nastavení“
      uiLang: (typeof localStorage !== 'undefined' && localStorage.getItem('hb_ui_lang')) || (typeof navigator !== 'undefined' && /^(cs|sk)\b/.test((navigator.language || navigator.userLanguage || '').toLowerCase()) ? 'cs' : 'en'),
      langLockUntil: 0,
      _lastFaviconKey: '',
      isFetchingState: false,
      pendingForcedFetch: false,
      isFetchingLogs: false,
      availableLangs: window.HB_AVAILABLE_LANGS || [{code:'en', label:'English'}, {code:'cs', label:'Čeština'}],
      activeTab: 'queue',
      pulseAddBtn: false,
      pulseAddWorkerBtn: false,      // „+“ Přidat worker na záložce Workery (bez workerů chvíli pulzuje)
      monToolsUserToggled: null,     // null = auto-rozbalit jen při chybě nástrojů/úložiště, true/false = volba uživatele
      changelogSeenVersion: (() => { try { return localStorage.getItem('hb_changelog_seen') || ''; } catch (e) { return ''; } })(),
      searchQuery: '',
      queueFiltersExpanded: false,
      moreFiltersOpen: false,        // Dokončené / Hledání: výběry Preset / Worker / Verze a štítky filtrů
      moreActionsOpen: false,        // nabídka ⋯ v liště (duplicity, mazání historie)
      mobileMoreOpen: false,         // mobil: nabídka „Více“ ve spodní liště
      tipsSeen: [],                  // jednorázové tipy, které uživatel už zavřel (ui_pref tips_seen)
      confirmingMenuAction: '',      // nevratná akce z nabídky čeká na druhé kliknutí
      queuePerPagePref: 50,          // Fronta: úloh na stránku při zapnutém stránkování (ui_pref queue_per_page)
      searchInQueue: true,
      searchInCompleted: false,
      highlightedWorker: '',
      _highlightWorkerTimer: null,
      settingsSnapshot: null,
      _settingsMutationSeq: 0,
      workerBubbleTip: {
        visible: false, x: 0, y: 0, workerName: '', version: '', targetVersion: '',
        needsUpdate: false, syncingTools: false, pendingTools: [], pendingToolsCount: 0,
        activeJobs: 0, usedSlots: 0, maxSlots: 0,
        cpuLine: '', ramLine: '', diagLine: ''
      },
      filterStatus: 'ALL',
      filterPreset: '',
      filterRes: '',
      filterWorker: '',
      filterVersion: '',
      filterPremux: '',
      filterNetFallback: false,
      filterIneffective: false,
      historySortKey: 'completed_at',
      historySortDir: 'desc',
      historyPage: 1,
      historyPerPage: 50,
      workersPlacementMode: localStorage.getItem('hb_workers_placement') || 'auto',
      // Pohyb loga v hlavičce: on = dýchání při převodu + krátké animace při událostech, events = jen události, off = bez pohybu
      logoMotion: (() => { try { const v = JSON.parse(localStorage.getItem('hb_pref_logo_motion') || '"on"'); return ['on', 'events', 'off'].includes(v) ? v : 'on'; } catch (e) { return 'on'; } })(),
      logoFx: '',
      logoHovered: false,
      sidebarSide: localStorage.getItem('hb_sidebar_side') || 'right',
      headerOverflowing: false,
      bubblesMeasure: { sig: '', avail: 0, needW: 0 },
      cardShowDestPath: localStorage.getItem('hb_card_dest') !== 'false',
      cardShowPreset: localStorage.getItem('hb_card_preset') !== 'false',
      cardShowTechBadges: localStorage.getItem('hb_card_tech') !== 'false',
      enablePagination: pagInit,
      showQueueEta: localStorage.getItem('hb_show_eta') !== 'false',
      queuePage: 1,
      perPage: pagInit ? 50 : 10000,
      requireTwoClickConfirm: localStorage.getItem('hb_two_click') !== 'false',
      confirmingResetStats: false,
      confirmingDeleteWorkerStats: '',
      _delStatsTimer: null,
      confirmingStopAll: false,
      confirmingWorkerStop: {},
      confirmingWorkerDelete: {},
      confirmingWorkerUpdate: {},
      confirmingBulkAction: '',
      _bulkConfirmTimer: null,
      confirmingHistoryDelSel: false,
      _histDelSelTimer: null,
      confirmingHistoryDelMap: {},
      _histDelTimers: {},
      confirmingForceReinstall: false,
      _forceReinstallTimer: null,
      confirmingClearSmb: false,
      _clearSmbTimer: null,
      expandedWorkerHistory: {},
      manualUploadOs: 'linux',
      manualUploadTool: 'hbcli',
      manualUploadVersion: '',
      isUploadingCustomBinary: false,
      isCheckingGitHub: false,
      isUpdatingFromGitHub: false,
      updateWaitActive: false,
      otaClientStart: 0,
      otaNow: Date.now(),
      otaWentDown: false,
      settingsDirty: false,
      isSavingSettings: false,
      smbSettingsTest: { running: false, result: null },
      bulkStagingSelect: 'Všechny formáty',
      bulkPowerSelect: 'AUTO',
      bulkDefaultsLoaded: false,
      isFileDragging: false,
      dragDepth: 0,
      selectedIds: [],
      lastCheckedIdx: null,
      selectedHistoryIds: [],
      busyJobs: {},
      confirmingMap: {},
      confirmTimers: {},
      rowShiftFreezeUntil: 0,
      isDragging: false,
      pausePollingUntil: 0,
      changelogOpenMap: { 0: true, 1: true },
      logsTabActiveWorker: 'ALL',
      logSources: [],
      logsNewCount: 0,
      logsUserScrolled: false,
      confirmingClearLogs: false,
      logsAutoScroll: true,
      logsSearchQuery: '',
      logsOnlyErrors: false,
      logsHideProgress: false,
      moveButtons: [
        { act: 'to_top', ico: 'chevron-double-up', tipKey: 'mv_to_top' },
        { act: 'up', ico: 'chevron-up', tipKey: 'mv_up' },
        { act: 'down', ico: 'chevron-down', tipKey: 'mv_down' },
        { act: 'to_bottom', ico: 'chevron-double-down', tipKey: 'mv_to_bottom' }
      ],
      bulkBarBtns: [
        { act: 'to_top', ico: 'chevron-double-up', labelKey: 'mv_to_top', cls: 'bg-slate-800 hover:bg-slate-700 border border-slate-600' },
        { act: 'to_bottom', ico: 'chevron-double-down', labelKey: 'mv_to_bottom', cls: 'bg-slate-800 hover:bg-slate-700 border border-slate-600' },
        { act: 'pause', ico: 'pause', labelKey: 'bulk_pause', cls: 'bg-indigo-700 hover:bg-indigo-600' },
        { act: 'resume', ico: 'refresh', labelKey: 'bulk_resume', cls: 'bg-emerald-700 hover:bg-emerald-600' },
        { act: 'cancel', ico: 'stop-sign', labelKey: 'bulk_stop', confirmKey: 'bulk_stop_confirm', destructive: true, cls: 'bg-amber-600 hover:bg-amber-500' },
        { act: 'delete', ico: 'trash', labelKey: 'bulk_delete', confirmKey: 'bulk_delete_confirm', destructive: true, cls: 'bg-rose-600 hover:bg-rose-500' }
      ],
      state: {
        manager_version: '',
        worker_target_version: '',
        settingsSection: 'general',
        maintenance_mode: false,
        output_storage: { free_gb: 0, total_gb: 0, effective_free_gb: 0, min_free_gb: 10, is_low_space: false },
        github_updater: { image: 'ghcr.io/maseklab/hb-manager:latest', remote_version: '', remote_digest: '', local_digest: '', update_available: false, digest_update: false, checked_at: 0, last_error: '', updating: false, phase: 'idle', docker_sock_available: false },
        ghcr_images: { manager: 'ghcr.io/maseklab/hb-manager:latest', worker: 'ghcr.io/maseklab/hb-worker:latest', project_url: 'https://github.com/maseklab/hb-cluster' },
        kofi_url: 'https://ko-fi.com/maseklab',
        storage_mounts: {},
        settings: {
          ui_lang: 'en',
          onboarding_done: false,
          auto_clean_history: false, convert_ass_to_srt: false, ass_srt_mode: 'keep_both', auto_clean_empty_subs: true,
          auto_extract_forced_subs: true, burn_in_subs: 'respect', dts_hd_mode: 'keep_both', write_mkv_stats: true, lpcm_passthru: true,
          smart_premux: false, ineffective_action: 'both', ineffective_prefix: '[HIGHER_BITRATE]_',
          ineffective_subfolder: '_HIGHER_BITRATE_THAN_SOURCE', preferred_builder_worker: 'AUTO',
          auto_build_on_first_run: true, auto_update_binaries: true, auto_update_cluster: true,
          default_staging_mode: 'Všechny formáty', default_power_mode: 'AUTO',
          default_use_local_temp: true, default_prioritize_4k: false,
          telemetry_source: 'both', worker_cpu_source: 'both', worker_ram_source: 'both', ram_format: 'gb_ratio',
          min_free_output_gb: 10.0, continue_encoding_on_disk_full: true,
          win_default_inputs_dir: '', win_default_outputs_dir: '',
          smb_username: '', smb_password: '',
          linux_default_inputs_dir: '/mnt/nas/inputs', linux_default_outputs_dir: '/mnt/nas/outputs', linux_default_temp_dir: '/mnt/nvme/hb_temp'
        },
        binaries: {},
        binary_builder: { status: 'IDLE', updating_tools: [], checklist: [], console_lines: [], upstream_versions: {}, missing_binaries: [], updates_available: [], binary_generation: 1 },
        workers: [],
        queue: [],
        analytics: {}
      },
      historyList: [],
      logsData: { manager_logs: [], workers_with_logs: [], selected_worker: 'ALL', worker_logs: [] },
      changelogText: '',
      inspectModal: { open: false, data: null },
      dupModal: { open: false, exact: [], diff: [], keepMode: 'newer', includeDiff: false },
      showOnboardingModal: false,
      stateLoaded: false,            // true po prvním načtení stavu → do té doby neukazovat „0 online“, průvodce ani „Fronta je prázdná“ (problikávalo při načtení stránky)
      onboardingStep: 1,
      _onboardingChecked: false,
      addWorkerModal: {
        open: false,
        tab: 'windows',
        preapprove: true,          // Zabezpečení: předschválit worker z balíčku (1 h)
        dlToken: '',
        dlExp: 0,
        workerName: 'Win-Worker-1',
        managerUrl: typeof window !== 'undefined' ? window.location.origin : 'http://192.168.1.100:8383',
        winInputsDir: '',
        winOutputsDir: '',
        winUseCustomTemp: false,
        winTempDir: 'C:\\HB_Temp',
        winAutostartMode: 'after_login',
        winSleepMode: 'encoding',
        winWakeCheck: true,
        winTray: true,
        winDesktopShortcut: true,
        winStartMenuShortcut: true,
        includeBinaries: false,
        showSmbSection: false,
        smbUsername: '',
        smbPassword: '',
        smbShowPassword: false,
        isTestingSmb: false,
        smbTestResult: null,
        linWorkerName: 'Linux-Worker-1',
        linDeployMode: 'portainer',
        linDataDir: '/opt/hb_worker/linux-worker-1',
        linInputsDir: '/mnt/nas/inputs',
        linOutputsDir: '/mnt/nas/outputs',
        linUseCustomTemp: false,
        linTempDir: '/mnt/nvme/hb_temp',
        linMountDockerSock: true
      },
      smbModal: {
        open: false,
        domain: '',
        username: '',
        password: '',
        showPassword: false
      },
      workerCapPrev: {},
      expandedWinSettings: {},
      expandedWorkerSettings: {},
      bulkExpanded: false,
      expandedCustomCpu: {},
      slotsFlash: {},
      dbBackups: [],
      workerWinDrafts: {},
      folderBrowser: {
        open: false,
        targetField: '',
        currentPath: '/',
        parentPath: '',
        entries: []
      },
      metricPopover: {
        visible: false,
        x: 0,
        y: 0,
        kind: 'video',
        title: '',
        rows: []
      },
      netFallbackTip: {
        visible: false,
        x: 0,
        y: 0,
        text: ''
      },
      toasts: [],
      selectedStatsWorker: 'Unknown',
      copiedKey: '',
      _copiedTimer: null,
      kofiHour: new Date().getHours(),
      kofiUrl: 'https://ko-fi.com/maseklab',
      kofiTimer: null,
      pollTimer: null,
      ghcrTimer: null,
      sortableInstance: null,
      resizeObserver: null
    };
  },
  computed: {
    otaActive() {
      return Boolean(this.updateWaitActive || this.isUpdatingFromGitHub || this.state.github_updater?.updating);
    },
    otaProgress() {
      const pr = this.state.github_updater?.progress || {};
      const total = Number(pr.total_steps || 4) || 4;
      let step = Number(pr.step || 0) || (this.otaActive ? 1 : 0);
      let pct = Number(pr.percent || 0);
      let detail = String(pr.detail || '');
      if (this.otaWentDown) {
        step = total;
        pct = Math.max(pct, 97);
        detail = this.t('ota_prog_waiting_back');
      }
      let elapsed = Number(pr.elapsed_sec || 0);
      if (this.otaClientStart) elapsed = Math.max(elapsed, Math.round((this.otaNow - this.otaClientStart) / 1000));
      step = Math.min(total, Math.max(1, step));
      return {
        step, total, detail, elapsed,
        percent: Math.min(100, Math.max(0, Math.round(pct))),
        label: this.t('ota_prog_step_' + step),
        target: String(pr.target_version || this.state.github_updater?.remote_version || '')
      };
    },
    isEveningKofi() {
      return this.kofiHour >= 18 || this.kofiHour < 5;
    },
    kofiIconName() {
      return this.isEveningKofi ? 'beer' : 'coffee';
    },
    kofiBtnText() {
      return this.isEveningKofi ? this.t('support_btn_beer') : this.t('support_btn_coffee');
    },
    kofiTooltip() {
      return this.isEveningKofi ? this.t('support_tip_beer') : this.t('support_tip_coffee');
    },
    kofiResolvedUrl() {
      return this.state?.kofi_url || this.kofiUrl || 'https://ko-fi.com/maseklab';
    },
    // Stav clusteru pro okénko v logu (stejná pravidla jako dřívější tečka u „HB Manager“).
    logoPendingApprovals() {
      return ((((this.state || {}).security || {}).workers || {}).entries || []).filter(r => r.status === 'pending').length;
    },
    // Důvod oranžového stavu (pro popisek loga); '' = žádný
    logoWarnKey() {
      if (this.logoPendingApprovals > 0) return 'logo_st_warn_pending';
      if (Boolean(this.state?.output_storage?.is_low_space) || (this.totalWaitingDiskCount > 0)) return 'logo_st_warn_disk';
      if (Boolean(this.state?.maintenance_mode) || !this.isClusterIntakeActive) return 'logo_st_warn_intake';
      if (this.totalFrozenActiveCount > 0) return 'logo_st_warn_paused';
      return '';
    },
    logoUpdateKind() {
      // fialová = aktualizace (dohodnutá barva stavů): Manager nebo workery mají novou verzi
      if (this.state.github_updater?.update_available) return 'mgr';
      if (this.outdatedWorkersCount > 0) return 'wk';
      return '';
    },
    logoStatus() {
      const remJobs = this.totalActiveCount + this.totalWaitingCount + this.totalWaitingDiskCount;
      if (this.otaActive) return 'updating';
      // chybějící nástroje nejsou chyba, dokud se právě sestavují (první spuštění, aktualizace nástrojů)
      const missingTools = (this.missingBinariesCount > 0) && !this.isBuildRunning;
      if ((this.errorCount > 0) || missingTools || (this.onlineWorkers.length === 0 && remJobs > 0)) return 'err';
      if (this.isBuildRunning) return 'building';
      if (this.logoWarnKey) return 'warn';
      if (this.logoUpdateKind) return 'upd';
      if (this.totalActiveCount > 0) return 'run';
      return 'idle';
    },
    logoClasses() {
      const st = this.logoStatus;
      return ['hb-logo-st-' + st, (this.logoMotion === 'on' && st === 'run') ? 'hb-logo-breathe' : '', (this.logoMotion !== 'off' && this.logoFx) ? 'hb-logo-fx-' + this.logoFx : '',
        (this.logoMotion !== 'off' && this.logoHovered) ? 'hb-logo-hover' : ''];
    },
    logoTitle() {
      const st = this.logoStatus;
      let txt = st === 'run' ? this.t('logo_st_run', this.totalActiveCount) : this.t('logo_st_' + st);
      if (st === 'warn' && this.logoWarnKey) txt = this.t(this.logoWarnKey, this.logoPendingApprovals);
      if (st === 'upd') txt = this.logoUpdateKind === 'mgr'
        ? this.t('logo_st_upd_mgr', this.state.github_updater?.remote_version || '?')
        : this.t('logo_st_upd_wk', this.outdatedWorkersCount);
      if (st === 'upd' && this.totalActiveCount > 0) txt += '\n' + this.t('logo_st_run', this.totalActiveCount);
      return txt + '\n' + this.t('w_title_ecosystem_tip');
    },
    smbAccountConfigured() {
      return Boolean((this.state?.settings?.smb_username || '').trim());
    },
    smbAccountSummary() {
      const u = (this.state?.settings?.smb_username || '').trim();
      if (!u) return this.t('set_smb_account_unset');
      return `${this.t('set_smb_account_set')} (${u})`;
    },
    outdatedOrMissingToolTargets() {
      const list = (this.toolUpdateCards || [])
        .filter(c => c.isMissing || c.hasUpdate)
        .map(c => c.target);
      const miss = this.state.binary_builder?.missing_binaries || [];
      if (miss.includes('windows_launcher') && !list.includes('launcher')) {
        list.push('launcher');
      }
      return list;
    },
    hasOutdatedOrMissingTools() {
      return this.outdatedOrMissingToolTargets.length > 0 || this.missingBinariesCount > 0 || this.anyToolNeedsUpdate;
    },
    anyToolNeedsUpdate() {
      return this.updatesAvailableCount > 0 || (this.toolUpdateCards || []).some(c => c.hasUpdate);
    },
    anyWorkerPendingToolSync() {
      return this.onlineWorkers.some(w => this.workerPendingBinaries(w).length > 0);
    },
    mainBuildButtonState() {
      if (this.hasOutdatedOrMissingTools) {
        return {
          cls: 'bg-sky-600 hover:bg-sky-500 text-white hb-sync-pulse',
          icon: 'cog',
          text: this.t('set_build_all')
        };
      }
      if (this.anyWorkerPendingToolSync) {
        return {
          cls: 'bg-indigo-950/90 text-sky-200 border border-sky-500/80',
          icon: 'download',
          text: this.t('set_build_pending_sync')
        };
      }
      return {
        cls: 'bg-slate-800 hover:bg-slate-700 text-emerald-300 border border-emerald-800/70 hover:border-emerald-600',
        icon: 'cog',
        text: this.t('set_build_uptodate')
      };
    },
    navTabGroups() {
      const qLen = (this.state.queue || []).length;
      const onLen = this.onlineWorkers.length;
      const compLen = this.state.analytics?.completed_count || this.historyList.length || 0;
      const outLen = this.outdatedWorkersCount;
      const ghUpd = Boolean(this.state.github_updater?.update_available);
      const binUpd = this.anyToolNeedsUpdate;
      const setBadge = this.isBuildRunning ? '' : ((this.missingBinariesCount > 0 || ghUpd) ? '!' : (binUpd ? '↑' : ''));
      const setCls = this.missingBinariesCount > 0 ? 'bg-rose-600 text-white hb-sync-pulse' : 'bg-violet-600 text-white hb-sync-pulse';
      const setIconCls = this.isBuildRunning ? 'text-sky-400 hb-sync-pulse' : '';
      const monBadge = binUpd ? `${onLen} ↑` : (onLen > 0 ? String(onLen) : '');
      const monCls = binUpd ? 'bg-violet-600 text-white hb-sync-pulse' : 'bg-emerald-600 text-white';

      return [
        [
          { id: 'queue', label: this.t('tab_queue'), icon: 'list', badgeText: qLen > 0 ? String(qLen) : '', badgeCls: 'bg-sky-600 text-white' },
          { id: 'history', label: this.t('tab_history'), icon: 'clock', badgeText: compLen > 0 ? String(compLen) : '', badgeCls: 'bg-slate-700 text-slate-200' }
        ],
        [
          { id: 'workers', label: this.t('tab_workers'), icon: 'cpu', badgeText: outLen > 0 ? ('↑' + outLen) : (onLen > 0 ? String(onLen) : ''), badgeCls: outLen > 0 ? 'bg-violet-600 text-white hb-sync-pulse' : 'bg-emerald-700 text-white' },
          { id: 'monitoring', label: this.t('tab_monitoring'), icon: 'chart', badgeText: monBadge, badgeCls: monCls },
          { id: 'logs', label: this.t('tab_logs'), icon: 'doc', badgeText: '', badgeCls: '' }
        ],
        [
          { id: 'settings', label: this.t('tab_settings'), icon: 'cog', iconCls: setIconCls, badgeText: setBadge, badgeCls: setCls, iconOnly: true }
        ]
      ];
    },
    // Jednorázový tip pro aktuální záložku (nejvýš jeden najednou)
    activeTip() {
      const byTab = { queue: 'queue_add', workers: 'workers_stop', history: 'history_menu', monitoring: 'monitoring_ip' };
      const id = byTab[this.activeTab];
      if (!id || (this.tipsSeen || []).includes(id)) return '';
      if (id === 'monitoring_ip' && !(this.state.workers || []).some(w => this.workerIpInfo(w).ip)) return '';
      return id;
    },
    // Mobil: spodní lišta = Fronta, Dokončené, Workery, Monitoring + „Více“ (Hledání, Živé logy, Nastavení)
    historySortOptions() {
      return [
        { k: 'completed_at', label: this.t('hist_col_completed') },
        { k: 'id', label: 'ID' },
        { k: 'worker_name', label: 'Worker' },
        { k: 'original_filename', label: this.t('hist_col_movie') },
        { k: 'res_out', label: this.t('hist_col_res') },
        { k: 'video_duration_sec', label: this.t('hist_col_len') },
        { k: 'duration_sec', label: this.t('hist_col_enc_time') },
        { k: 'saved_bytes', label: this.t('hist_col_br_size') }
      ];
    },
    secInfo() { return this.state.security || {}; },
    secNetSummary() {
      const s = this.secInfo, on = this.t('sec_ov_on').toLowerCase(), off = this.t('sec_ov_off').toLowerCase();
      return [
        `${this.t('sec_net_home')}: ${s.home_bypass ? on : off}`,
        `${this.t('sec_net_proxy')}: ${s.behind_proxy ? on : off}`,
        `${this.t('sec_net_stats')}: ${s.public_stats !== false ? on : off}`
      ].join(' · ');
    },
    secWk() { return this.secInfo.workers || { approval: false, auto_until: 0, entries: [] }; },
    secWkPending() { return (this.secWk.entries || []).filter(r => r.status === 'pending'); },
    // workery navázané na jiný Manager (hlásí se, ale odpovědi tohoto Manageru nepřijmou)
    wkBoundElsewhere() { return Array.isArray(this.state.workers_bound_elsewhere) ? this.state.workers_bound_elsewhere : []; },
    wkNameClash() { return Array.isArray(this.state.workers_name_clash) ? this.state.workers_name_clash : []; },
    secWkOthers() { return (this.secWk.entries || []).filter(r => r.status !== 'pending' && !r.test); },
    secAutoLeft() {
      // počet zbývajících minut okna „Schvalovat automaticky“ (přepočet s každým stavem z Manageru)
      void this.state;
      const until = Number(this.secWk.auto_until || 0) * 1000;
      return until > Date.now() ? Math.max(1, Math.ceil((until - Date.now()) / 60000)) : 0;
    },
    // Zabezpečení krok 2 (fáze A): které workery už posílají svůj klíč
    secWorkerKeyRows() {
      const keys = (this.secWk.entries || []).filter(x => !x.test);
      return (this.state.workers || []).map(w => {
        const k = keys.filter(x => x.name === w.worker_name).sort((a, b) => (b.last_seen || 0) - (a.last_seen || 0))[0];
        return { name: w.worker_name, hasKey: !!k, lastSeen: k ? k.last_seen : 0, version: w.version || '' };
      }).sort((a, b) => a.name.localeCompare(b.name));
    },
    isMobileSettings() { return this.windowWidth < 768; },   // stejná hranice jako spodní lišta (md)
    mobileBottomItems() {
      const all = this.navTabGroups.flat();
      return ['queue', 'history', 'workers', 'monitoring'].map(id => all.find(x => x.id === id)).filter(Boolean);
    },
    mobileMoreItems() {
      const all = this.navTabGroups.flat();
      return [{ id: 'search', label: this.t('mobile_search_lbl'), icon: 'search', badgeText: '', badgeCls: '' }]
        .concat(['logs', 'settings'].map(id => all.find(x => x.id === id)).filter(Boolean));
    },
    updatesAvailableSummary() {
      const cards = (this.toolUpdateCards || []).filter(c => c.hasUpdate);
      if (cards.length > 0) {
        return cards.map(c => `${c.shortTitle} v${c.onlineVer}`).join(', ');
      }
      return String(this.updatesAvailableCount);
    },
    toolUpdateCards() {
      const bins = this.state.binaries || {};
      const lin = bins.linux || {};
      const win = bins.windows || {};
      const up = this.state.binary_builder?.upstream_versions || {};
      const updTools = this.state.binary_builder?.updating_tools || [];
      const ffRecent = Boolean(this.state.binary_builder?.ffmpeg_recent);

      const hasLinWorker = this.linuxWorkersList.length > 0;
      const makeCard = (target, title, shortTitle, descKey, mainKey, extraLinKeys, extraWinKeys, upKey) => {
        const lObj = lin[mainKey] || {};
        const wObj = win[mainKey] || {};
        const instVer = String((hasLinWorker ? lObj.version : '') || wObj.version || lObj.version || '').replace(/^v/, '');
        let onlVer = String(up[upKey] || '').replace(/^v/, '');
        const allLinOk = !hasLinWorker || [mainKey, ...extraLinKeys].every(k => lin[k]?.available);
        const allWinOk = [mainKey, ...extraWinKeys].every(k => win[k]?.available);
        const isMissing = !allLinOk || !allWinOk;
        const isFfmpegNightly = (target === 'ffmpeg' && /^N-/i.test(instVer));
        const suppressFfmpegAlert = (target === 'ffmpeg' && ffRecent && !isFfmpegNightly);
        const hasUpdate = Boolean(!isMissing && !suppressFfmpegAlert && onlVer && onlVer !== 'latest' && instVer && instVer !== onlVer);
        const isUpdating = [mainKey, ...extraLinKeys, ...extraWinKeys].some(
          k => updTools.includes(`linux_${k}`) || updTools.includes(`windows_${k}`)
        );
        return {
          target,
          title,
          shortTitle,
          desc: this.t(descKey),
          installedVer: instVer,
          onlineVer: onlVer,
          isMissing,
          hasUpdate,
          isUpdating
        };
      };

      return [
        makeCard('hbcli', 'HandBrakeCLI', 'HBCLI', 'set_tool_hb_desc', 'hbcli', [], [], 'hb_latest'),
        makeCard('mkvtoolnix', 'MKVToolNix', 'MKVToolNix', 'set_tool_mkv_desc', 'mkvmerge', ['mkvpropedit'], ['mkvpropedit'], 'mkvtoolnix_latest'),
        makeCard('ffmpeg', 'FFmpeg & FFprobe', 'FFmpeg', 'set_tool_ff_desc', 'ffmpeg', ['ffprobe'], ['ffprobe'], 'ffmpeg_latest'),
        makeCard('7z', '7-Zip (7zz / 7z.exe)', '7-Zip', 'set_tool_7z_desc', '7z', [], ['7z_dll'], 'sevenzip_latest')
      ];
    },
    isSearchPanelOpen() {
      return this.activeTab === 'search' || this.activeTab === 'history' || this.queueFiltersExpanded;
    },
    searchableJobsPool() {
      const out = [];
      if (this.searchInQueue) {
        for (const j of (this.state.queue || [])) {
          if (!j.is_system_job) out.push(j);
        }
      }
      if (this.searchInCompleted) {
        for (const h of (this.historyList || [])) {
          out.push(h);
        }
      }
      return out;
    },
    unifiedFilterStats() {
      const resRank = { '4K → 4K': 1, '1080p → 1080p': 2, '4K → 1080p': 3, '720p → 720p': 4, '576p → 576p': 5 };
      const resMap = {};
      const presetSet = new Set();
      const workerSet = new Set();
      const verSet = new Set();
      let hasLegacyVer = false;
      let premuxOk = 0;
      let premuxFb = 0;
      let netFallback = 0;
      let ineffective = 0;

      for (const j of this.searchableJobsPool) {
        if (j.res_in && j.res_out) {
          const rk = `${j.res_in} → ${j.res_out}`;
          resMap[rk] = (resMap[rk] || 0) + 1;
        }
        if (j.premux_status === 'OK' || j.premux_applied) premuxOk++;
        else if (j.premux_status === 'FALLBACK' || (j.is_disc_source && !j.premux_applied)) premuxFb++;
        if (j.is_net_fallback) netFallback++;
        if (j.is_ineffective) ineffective++;
        if (j.preset_name) presetSet.add(j.preset_name);
        if (j.worker_name) workerSet.add(j.worker_name);
      }
      if (this.searchInCompleted) {
        for (const h of (this.historyList || [])) {
          const v = this.extractJobWorkerVersion(h);
          if (v) verSet.add(v);
          else hasLegacyVer = true;
        }
      }
      const resList = Object.entries(resMap)
        .map(([label, count]) => ({ label, count }))
        .sort((a, b) => ((resRank[a.label] || 99) - (resRank[b.label] || 99)) || a.label.localeCompare(b.label));
      const versions = Array.from(verSet).sort().reverse();
      if (hasLegacyVer) versions.push('LEGACY');

      return {
        resList,
        resMap,
        premuxOk,
        premuxFb,
        netFallback,
        ineffective,
        presets: Array.from(presetSet).sort((a, b) => a.localeCompare(b)),
        workers: Array.from(workerSet).sort((a, b) => a.localeCompare(b)),
        versions
      };
    },
    // Živé logy: jedna časová osa z Manageru i všech workerů. Řádky mají tvar "[YYYY-MM-DD HH:MM:SS] [ZDROJ] zpráva".
    allLogEntries() {
      const re = /^\[(\d{4}-\d\d-\d\d) (\d\d:\d\d:\d\d)\]\s*\[([^\]]+)\]\s*([\s\S]*)$/;
      const errRe = /(chyba|error|selhal|failed|critical|kritick|exception|traceback|✗)/i;
      const warnRe = /(warn|varování|timeout|odkládám|deferring|pauza|paused|waiting_disk|storage guard|přeskočen|skipped|nedostupn|unreachable|retry|zkusím)/i;
      const seenKeys = new Map();
      const hashLine = (str) => {
        let h = 2166136261;
        for (let i = 0; i < str.length; i++) {
          h ^= str.charCodeAt(i);
          h = Math.imul(h, 16777619);
        }
        return (h >>> 0).toString(36);
      };
      const parse = (raw, fallbackSrc) => {
        const s = String(raw || '');
        const m = s.match(re);
        const ts = m ? (m[1] + ' ' + m[2]) : '';
        const src = m ? m[3].trim() : fallbackSrc;
        const msg = m ? m[4] : s;
        const lvl = errRe.test(msg) ? 'err' : (warnRe.test(msg) ? 'warn' : 'info');
        const baseKey = fallbackSrc + '-' + hashLine(s) + '-' + s.length;
        const occ = (seenKeys.get(baseKey) || 0) + 1;
        seenKeys.set(baseKey, occ);
        return { ts, time: m ? m[2] : '', date: m ? m[1] : '', src, msg, lvl, raw: s, key: baseKey + '-' + occ };
      };
      const mgr = (this.logsData?.manager_logs || []).map((l) => parse(l, 'MANAGER'));
      const wrk = (this.logsData?.worker_logs || []).map((l) => parse(l, 'W'));
      return mgr.concat(wrk).map((e, i) => ({ ...e, ord: i })).sort((a, b) => (a.ts === b.ts ? a.ord - b.ord : (a.ts < b.ts ? -1 : 1)));
    },
    // Shrnutí nástrojů a úložiště pro sbalený řádek v Monitoringu
    monToolsSummary() {
      if (this.isBuildRunning) return { level: 'build', cls: 'bg-sky-950 border-sky-600 text-sky-200', text: this.t('mon_tools_building') };
      if (this.missingBinariesCount > 0) return { level: 'err', cls: 'bg-rose-950 border-rose-600 text-rose-200', text: this.t('mon_tools_missing', this.missingBinariesCount) };
      if (this.anyToolNeedsUpdate) return { level: 'upd', cls: 'bg-violet-950 border-violet-500 text-violet-200', text: this.t('mon_tools_update') };
      return { level: 'ok', cls: 'bg-emerald-950 border-emerald-700 text-emerald-300', text: this.t('mon_tools_ok') };
    },
    monToolsOpen() {
      if (this.monToolsUserToggled !== null) return this.monToolsUserToggled;
      const storageBad = Boolean(this.state?.storage_mounts) && (!this.state.storage_mounts.inputs_ok || Boolean(this.state.output_storage?.is_low_space));
      return this.monToolsSummary.level === 'err' || storageBad;
    },
    secNetExpanded() {
      if (this.secNetUserToggled !== null) return this.secNetUserToggled;
      return Boolean(this.secInfo?.proxy_hint);
    },
    canDragReorder() {
      return this.activeTab === 'queue' && !this.hasActiveFilters;
    },
    // Pořadí workerů v Monitoringu i na záložce Workery: pracující -> volné -> offline, pak podle jména
    sortedWorkersForDisplay() {
      const rank = (w) => (!w.is_online ? 2 : ((w.active_jobs_count || 0) > 0 ? 0 : 1));
      return (this.state?.workers || []).slice().sort((a, b) => rank(a) - rank(b) || String(a.worker_name).localeCompare(String(b.worker_name)));
    },
    logSourceNames() {
      const names = new Set((this.state?.workers || []).map(w => w.worker_name));
      (this.logsData?.workers_with_logs || []).forEach(n => names.add(n));
      return ['MANAGER'].concat(Array.from(names).filter(n => n && n !== 'MANAGER').sort());
    },
    visibleLogEntries() {
      const on = new Set(this.logSources || []);
      const q = (this.logsSearchQuery || '').toLowerCase().trim();
      const progRe = /(Stav úlohy\s+\d+:|Job\s+\d+\s+progress:|Stahování úlohy\s+\d+:|Staging job\s+\d+:|HTTP Relay stahování|HTTP Relay download|Kopírování do [Oo]utputs:|Copying to [Oo]utputs:|Pre-Mux\s+[12]\/2)/i;
      const out = this.allLogEntries.filter(e => {
        if (on.size > 0 && !on.has(e.src)) return false;
        if (this.logsOnlyErrors && e.lvl === 'info') return false;
        if (this.logsHideProgress && progRe.test(e.msg)) return false;
        if (q && !e.raw.toLowerCase().includes(q)) return false;
        return true;
      });
      return out.length > 2000 ? out.slice(-2000) : out;
    },
    monitoringKpis() {
      const cs = this.clusterStats;
      const an = this.state.analytics || {};
      return [
        {
          title: this.t('mon_cpu_title'),
          color: 'text-sky-400',
          main: cs.weightedCpu.toFixed(1) + ' %',
          sub: `HandBrake: <strong class="text-slate-200">${cs.weightedHbCpu.toFixed(1)} %</strong> (${cs.totalCores} ${this.t('mon_threads_lbl')})`
        },
        {
          title: this.t('mon_ram_title'),
          color: 'text-purple-400',
          main: `${cs.ramUsed.toFixed(1)} / ${cs.ramTotal.toFixed(1)} GB`,
          sub: `HandBrake RAM: <strong class="text-slate-200">${cs.ramHb.toFixed(2)} GB</strong>`
        },
        {
          title: this.t('mon_saved_title'),
          color: 'text-emerald-400',
          main: this.formatBytes(an.total_saved_bytes || 0),
          sub: `${this.formatBytes(an.total_src_bytes || 0)} → ${this.formatBytes(an.total_out_bytes || 0)} (<strong class="text-emerald-300">-${this.totalSavedPct}%</strong>)`
        },
        {
          title: this.t('mon_count_title'),
          color: 'text-amber-400',
          main: String(an.completed_count || 0),
          sub: `${this.t('mon_encoding_time')} <strong class="text-slate-200">${this.formatSecondsHM(an.total_encoding_sec || 0)}</strong>`
        }
      ];
    },
    onlineWorkers() { return (this.state.workers || []).filter(w => w.is_online); },
    linuxWorkersList() { return (this.state.workers || []).filter(w => String(w?.telemetry?.diag?.os || '').toLowerCase().includes('linux')); },
    isBuildRunning() { const s = this.state.binary_builder?.status; return s === 'QUEUED' || s === 'BUILDING'; },
    missingBinariesCount() { return (this.state.binary_builder?.missing_binaries || []).length; },
    updatesAvailableCount() { return (this.state.binary_builder?.updates_available || []).length; },
    enabledWorkersCount() { return (this.state.workers || []).filter(w => w.is_enabled).length; },
    localTempWorkersCount() { return (this.state.workers || []).filter(w => w.use_local_temp).length; },
    prio4kWorkersCount() { return (this.state.workers || []).filter(w => w.prioritize_4k).length; },
    allWorkersEnabled() { const l = this.state.workers || []; return l.length > 0 && this.enabledWorkersCount === l.length; },
    allWorkersLocalTemp() { const l = this.state.workers || []; return l.length > 0 && this.localTempWorkersCount === l.length; },
    allWorkersPrio4k() { const l = this.state.workers || []; return l.length > 0 && this.prio4kWorkersCount === l.length; },
    anyWorkerCanPremux() { const l = this.state.workers || []; return l.length > 0 && l.some(w => this.isWorkerPremuxReady(w)); },
    isClusterIntakeActive() {
      if (this.state.maintenance_mode) return false;
      const l = this.state.workers || [];
      return l.length === 0 || this.enabledWorkersCount > 0;
    },
    outdatedWorkersCount() {
      const t = this.state.worker_target_version;
      if (!t) return 0;
      return (this.state.workers || []).filter(w => w.is_online && w.version && w.version !== '?' && w.version !== t).length;
    },
    managerEngineBadge() {
      const me = this.state.storage_mounts?.manager_engine;
      if (!me) return { cls: 'bg-slate-800 border-slate-700 text-slate-300', text: 'Manager Engine: ...', tooltip: 'Loading...' };
      const allCentral = me.hbcli_central && me.ffprobe_central && me.sevenzip_central;
      const tip = [
        'Tools used directly by Manager (/data/binaries/linux):',
        `• HandBrakeCLI (v${me.hbcli_version}): Title scanning (--scan) for .ISO & BDMV`,
        `• FFprobe (v${me.ffprobe_version}): Async resolution & duration detection`,
        `• 7-Zip (v${me.sevenzip_version}): Parsing .MPLS playlists & .M2TS sizes inside .ISO`
      ].join('\n');
      if (me.all_ok && allCentral) return { cls: 'bg-emerald-950 border-emerald-700 text-emerald-300', text: 'Manager Engine: HBCLI + FFprobe + 7-Zip OK ✓', tooltip: tip };
      if (me.all_ok) return { cls: 'bg-teal-950 border-teal-700 text-teal-200', text: 'Manager Engine: Active (System fallback)', tooltip: tip };
      return { cls: 'bg-rose-950 border-rose-600 text-rose-200 hb-sync-pulse', text: '! Manager Engine: Missing Tool', tooltip: tip };
    },
    headerBubbleUniformWidthPx() {
      const cpuSrc = this.workerCpuSourcePref();
      const ramSrc = this.workerRamSourcePref();
      const ramFmt = this.ramFormatPref();
      const cpuChars = cpuSrc === 'hb' ? 12 : (cpuSrc === 'system' ? 9 : 19);
      const sysRamChars = (ramFmt === 'gb_pct' || ramFmt === 'pct_gb') ? 21 : (ramFmt === 'pct_only' ? 4 : 14);
      const ramChars = ramSrc === 'hb' ? 16 : (ramSrc === 'system' ? (5 + sysRamChars) : (5 + sysRamChars + 14));
      const telemWidthPx = Math.ceil(Math.max(cpuChars, ramChars) * 5.65 + 24);
      let maxTopRowPx = 148;
      for (const w of (this.onlineWorkers || [])) {
        const nameLen = String(w?.worker_name || '').length;
        let topPx = 28 + Math.ceil(nameLen * 6.8);
        if (this.needsWorkerUpdate(w)) topPx += 14;
        if ((w?.held_disk_jobs_count || 0) > 0) topPx += 66;
        if (this.workerMissingTools(w).length > 0 && !this.isBuildRunning) topPx += 64;
        else if (this.workerPendingBinaries(w).length > 0) topPx += 30;
        if (this.hasStorageIssue(w)) topPx += 36;
        if (!w?.is_enabled || this.state?.maintenance_mode) topPx += 44;
        if (topPx > maxTopRowPx) maxTopRowPx = topPx;
      }
      return Math.max(telemWidthPx, maxTopRowPx);
    },
    effectiveWorkersView() {
      if (this.workersPlacementMode === 'hidden') return 'hidden';
      if (this.workersPlacementMode === 'compact' || this.windowWidth < 1024) return 'compact';
      if (this.workersPlacementMode === 'sidebar') return this.windowWidth >= 1280 ? 'sidebar' : 'compact';
      if (this.headerOverflowing) {
        return this.windowWidth >= 1280 ? 'sidebar' : 'compact';
      }
      return 'bubbles';
    },
    totalQueueCount() { return (this.state.queue || []).length; },
    totalActiveCount() { return (this.state.queue || []).filter(j => RUNNING_STATES.includes(j.status)).length; },
    totalFrozenActiveCount() { return (this.state.queue || []).filter(j => RUNNING_STATES.includes(j.status) && j.is_paused).length; },
    allActiveFrozen() { return this.totalActiveCount > 0 && this.totalFrozenActiveCount === this.totalActiveCount; },
    totalWaitingCount() { return (this.state.queue || []).filter(j => j.status === 'WAITING').length; },
    totalWaitingDiskCount() { return (this.state.queue || []).filter(j => j.status === 'WAITING_DISK').length; },
    totalPausedCount() { return (this.state.queue || []).filter(j => j.status === 'PAUSED').length; },
    errorCount() { return (this.state.queue || []).filter(j => j.status === 'ERROR').length; },
    hasPanelFilterCriteria() {
      return Boolean(
        (this.searchQuery || '').trim() ||
        this.filterPreset ||
        this.filterRes ||
        this.filterWorker ||
        this.filterVersion ||
        this.filterPremux ||
        this.filterNetFallback ||
        this.filterIneffective
      );
    },
    queuePanelFilterCount() {
      let n = 0;
      if ((this.searchQuery || '').trim()) n++;
      if (this.filterPreset) n++;
      if (this.filterRes) n++;
      if (this.filterWorker) n++;
      if (this.filterVersion) n++;
      if (this.filterPremux) n++;
      if (this.filterNetFallback) n++;
      if (this.filterIneffective) n++;
      if (this.searchInCompleted) n++;
      return n;
    },
    hasUnseenChangelog() {
      const v = this.state?.manager_version || '';
      return Boolean(v) && this.changelogSeenVersion !== v;
    },
    hasActiveFilters() { return Boolean(this.filterStatus !== 'ALL' || this.hasPanelFilterCriteria); },
    // Pruh aktivních filtrů – vidět i při sbaleném panelu, každý filtr jde zrušit křížkem
    activeFilterChips() {
      const out = [];
      if (this.activeTab === 'queue' && this.filterStatus !== 'ALL') {
        const stKey = { ACTIVE: 'running_lbl', WAITING: 'waiting_lbl', WAITING_DISK: 'waiting_disk_lbl', PAUSED: 'paused_lbl', ERROR: 'errors_lbl' }[this.filterStatus];
        out.push({ key: 'status', label: stKey ? this.t(stKey) : this.filterStatus });
      }
      const q = (this.searchQuery || '').trim();
      if (q) out.push({ key: 'search', label: '„' + q + '“' });
      if (this.filterPreset) out.push({ key: 'preset', label: this.filterPreset });
      if (this.filterWorker) out.push({ key: 'worker', label: 'Worker: ' + this.filterWorker });
      if (this.filterVersion) out.push({ key: 'version', label: this.filterVersion === 'LEGACY' ? 'Legacy' : 'v' + this.filterVersion });
      if (this.filterRes) out.push({ key: 'res', label: this.filterRes });
      if (this.filterPremux) out.push({ key: 'premux', label: this.filterPremux === 'OK' ? this.t('premux_ok') : this.t('filter_premux_fb') });
      if (this.filterNetFallback) out.push({ key: 'net', label: this.t('filter_temp_save') });
      if (this.filterIneffective) out.push({ key: 'ineffective', label: this.t('hist_higher_br') });
      return out;
    },
    filterShownInfo() {
      const qAll = (this.state.queue || []).length;
      if (this.activeTab === 'history') return { shown: this.sortedHistory.length, total: this.historyList.length };
      if (this.activeTab === 'search') return { shown: this.filteredQueue.length + this.sortedHistory.length, total: qAll + this.historyList.length };
      return { shown: this.filteredQueue.length, total: qAll };
    },
    moreFiltersCount() {
      let n = 0;
      ['filterPreset', 'filterWorker', 'filterVersion', 'filterRes', 'filterPremux', 'filterNetFallback', 'filterIneffective'].forEach(k => { if (this[k]) n++; });
      return n;
    },
    filteredQueue() {
      return (this.state.queue || []).filter(j => {
        if (this.activeTab === 'queue') {
          if (this.filterStatus === 'ACTIVE' && !RUNNING_STATES.includes(j.status)) return false;
          if (this.filterStatus !== 'ALL' && this.filterStatus !== 'ACTIVE' && j.status !== this.filterStatus) return false;
        }
        return this.matchesJobUnifiedFilters(j);
      });
    },
    activeJobs() { return this.filteredQueue.filter(j => RUNNING_STATES.includes(j.status) || j.status === 'WAITING_DISK'); },
    otherJobs() { return this.filteredQueue.filter(j => !RUNNING_STATES.includes(j.status) && j.status !== 'WAITING_DISK'); },
    searchQueueRunningCount() { return this.filteredQueue.filter(j => RUNNING_STATES.includes(j.status)).length; },
    searchQueueWaitingCount() { return this.filteredQueue.filter(j => j.status === 'WAITING').length; },
    totalPages() {
      if (!this.enablePagination || this.perPage >= 10000) return 1;
      return Math.max(1, Math.ceil(this.otherJobs.length / this.perPage));
    },
    paginatedOtherJobs() {
      if (!this.enablePagination || this.perPage >= 10000) return this.otherJobs;
      const s = (this.queuePage - 1) * this.perPage;
      return this.otherJobs.slice(s, s + this.perPage);
    },
    ineffectiveExamplePath() {
      const act = this.state.settings?.ineffective_action || 'both';
      const pref = (act === 'prefix' || act === 'both') ? (this.state.settings?.ineffective_prefix || '[HIGHER_BITRATE]_') : '';
      const sub = (act === 'subfolder' || act === 'both') ? ((this.state.settings?.ineffective_subfolder || '_HIGHER_BITRATE_THAN_SOURCE') + '/') : '';
      return `/outputs/${sub}${pref}Movie (2024).mkv`;
    },
    filteredHistory() {
      return (this.historyList || []).filter(h => this.matchesJobUnifiedFilters(h));
    },
    sortedHistory() {
      const copy = [...this.filteredHistory];
      const k = this.historySortKey;
      const d = this.historySortDir === 'asc' ? 1 : -1;
      copy.sort((a, b) => {
        let va, vb;
        if (k === 'saved_bytes') {
          va = (a.v_bitrate_in_kbps || 0) - (a.v_bitrate_out_kbps || 0) || ((a.src_size_bytes || 0) - (a.out_size_bytes || 0));
          vb = (b.v_bitrate_in_kbps || 0) - (b.v_bitrate_out_kbps || 0) || ((b.src_size_bytes || 0) - (b.out_size_bytes || 0));
        } else if (k === 'completed_at') {
          va = a.completed_at ? new Date(a.completed_at).getTime() : 0;
          vb = b.completed_at ? new Date(b.completed_at).getTime() : 0;
        } else { va = a[k]; vb = b[k]; }
        if (typeof va === 'string' || typeof vb === 'string') return String(va || '').localeCompare(String(vb || ''), this.uiLang === 'cs' ? 'cs' : 'en') * d;
        return ((Number(va) || 0) - (Number(vb) || 0)) * d;
      });
      return copy;
    },
    historyTotalPages() {
      return Math.max(1, Math.ceil(this.sortedHistory.length / this.historyPerPage));
    },
    paginatedHistory() {
      const s = (this.historyPage - 1) * this.historyPerPage;
      return this.sortedHistory.slice(s, s + this.historyPerPage);
    },
    clusterStats() {
      const on = this.onlineWorkers.filter(w => w.telemetry);
      const totalCores = on.reduce((s, w) => s + (w.telemetry.cores || 0), 0);
      const weightedCpu = totalCores > 0 ? on.reduce((s, w) => s + (w.telemetry.cpu_total || 0) * (w.telemetry.cores || 1), 0) / totalCores : 0;
      const weightedHbCpu = totalCores > 0 ? on.reduce((s, w) => s + (w.telemetry.cpu_hb || 0) * (w.telemetry.cores || 1), 0) / totalCores : 0;
      const ramUsed = on.reduce((s, w) => s + (w.telemetry.ram_used_gb || 0), 0);
      const ramTotal = on.reduce((s, w) => s + (w.telemetry.ram_total_gb || 0), 0);
      const ramHb = on.reduce((s, w) => s + (w.telemetry.ram_hb_gb || 0), 0);
      return { totalCores, weightedCpu, weightedHbCpu, ramUsed, ramTotal, ramHb };
    },
    hasAnyHybridWorker() {
      return (this.state.workers || []).some(w => Boolean(w?.telemetry?.diag?.is_hybrid_cpu) || w?.power_mode === 'HYBRID_ECO');
    },
    workerChartRows() {
      const pw = this.state.analytics?.per_worker || {};
      const names = Object.keys(pw);
      if (!names.length) return [];
      const sumJobs = Math.max(1, names.reduce((s, k) => s + (pw[k].count || 0), 0));
      const maxJobs = Math.max(1, ...names.map(k => pw[k].count || 0));
      const maxSrc = Math.max(1, ...names.map(k => pw[k].src_bytes || 0));
      return names.map(name => {
        const it = pw[name] || {};
        const count = it.count || 0;
        const srcBytes = it.src_bytes || 0;
        const outBytes = it.out_bytes || 0;
        const savedPct = srcBytes > 0 ? Math.max(0, (100 - (outBytes / srcBytes) * 100)).toFixed(1) : '0.0';
        return {
          name, count,
          sharePct: Math.round((count / sumJobs) * 100),
          barPct: Math.max(4, Math.round((count / maxJobs) * 100)),
          srcBytes, outBytes, savedPct,
          srcBarPct: Math.max(4, Math.round((srcBytes / maxSrc) * 100)),
          outBarPct: srcBytes > 0 ? Math.max(2, Math.round((outBytes / maxSrc) * 100)) : 0
        };
      }).sort((a, b) => b.count - a.count);
    },
    totalSavedPct() {
      const s = this.state.analytics?.total_src_bytes || 0;
      const o = this.state.analytics?.total_out_bytes || 0;
      return s > 0 ? Math.max(0, (100 - (o / s) * 100)).toFixed(1) : '0.0';
    },
    totalQueueEtaSec() {
      const queue = (this.state.queue || []).filter(j => j && !j.is_system_job);
      if (!queue.length) return 0;

      const profiles = Array.isArray(this.state.analytics?.profiles)
        ? this.state.analytics.profiles
        : (Array.isArray(this.state.analytics?.stats_profiles) ? this.state.analytics.stats_profiles : []);
      const normResBucket = (rawRes) => {
        const s = String(rawRes || '').toUpperCase();
        if (s.includes('4K') || s.includes('2160')) return '4K';
        if (s.includes('1080')) return '1080p';
        if (s.includes('720')) return '720p';
        if (s.includes('576') || s.includes('480') || s.includes('SD')) return 'SD';
        return '1080p';
      };
      const defaultFpsForRes = (resBucket) => {
        if (resBucket === '4K') return 3.5;
        if (resBucket === '1080p') return 18.0;
        return 45.0;
      };
      const profResBucket = (p) => normResBucket(p?.res_out || p?.res_in || p?.resolution);
      const estimateFps = (job, workerName) => {
        const resBucket = normResBucket(job?.res_out || job?.res_in);
        const preset = String(job?.video_preset || job?.preset_name || '').trim();
        const wName = String(workerName || job?.worker_name || '').trim();

        if (wName && preset) {
          const exact = profiles.filter(p => p.worker_name === wName && (p.video_preset === preset || p.preset_name === preset) && profResBucket(p) === resBucket && Number(p.avg_fps || 0) > 0);
          if (exact.length) {
            const totJobs = exact.reduce((s, p) => s + (Number(p.jobs_count) || 1), 0);
            return exact.reduce((s, p) => s + Number(p.avg_fps) * (Number(p.jobs_count) || 1), 0) / Math.max(1, totJobs);
          }
        }
        if (wName) {
          const byWrkRes = profiles.filter(p => p.worker_name === wName && profResBucket(p) === resBucket && Number(p.avg_fps || 0) > 0);
          if (byWrkRes.length) {
            const totJobs = byWrkRes.reduce((s, p) => s + (Number(p.jobs_count) || 1), 0);
            return byWrkRes.reduce((s, p) => s + Number(p.avg_fps) * (Number(p.jobs_count) || 1), 0) / Math.max(1, totJobs);
          }
          const liveWrkJobs = queue.filter(q => q.worker_name === wName && q.status === 'IN_PROGRESS' && (Number(q.fps_avg || q.fps_current || 0) > 0));
          if (liveWrkJobs.length) {
            const sameRes = liveWrkJobs.filter(q => normResBucket(q.res_out || q.res_in) === resBucket);
            const pool = sameRes.length ? sameRes : liveWrkJobs;
            return pool.reduce((s, q) => s + Number(q.fps_avg || q.fps_current || 0), 0) / pool.length;
          }
        }
        const clusterRes = profiles.filter(p => profResBucket(p) === resBucket && Number(p.avg_fps || 0) > 0);
        if (clusterRes.length) {
          const totJobs = clusterRes.reduce((s, p) => s + (Number(p.jobs_count) || 1), 0);
          return clusterRes.reduce((s, p) => s + Number(p.avg_fps) * (Number(p.jobs_count) || 1), 0) / Math.max(1, totJobs);
        }
        return defaultFpsForRes(resBucket);
      };

      const estimateVideoDurationSec = (job) => {
        const d = Number(job?.video_duration_sec || 0);
        if (d > 0) return d;
        const szGb = Number(job?.src_size_bytes || 0) / Math.pow(1024, 3);
        if (szGb > 25) return 7200;
        if (szGb > 4) return 5400;
        if (szGb > 0.5) return 3600;
        return 5400;
      };

      const estimateFullEncodeSec = (job, workerName) => {
        const durSec = estimateVideoDurationSec(job);
        const fps = Math.max(0.25, estimateFps(job, workerName));
        return Math.round((durSec * 24.0) / fps);
      };

      const estimateTransferRemSec = (job, defaultMbps = 80.0) => {
        const directEta = Number(job?.eta_sec || 0);
        if (directEta > 0) return directEta;
        const pct = Math.max(0, Math.min(100, Number(job?.progress || 0)));
        const bytesTotal = Number(job?.src_size_bytes || 0) || (4 * Math.pow(1024, 3));
        const remMB = (bytesTotal * ((100.0 - pct) / 100.0)) / (1024 * 1024);
        const speed = Math.max(5.0, Number(job?.speed_mbps || job?.transfer_speed_mbps || 0) || defaultMbps);
        return Math.max(5, Math.round(remMB / speed));
      };

      const estimateActiveJobRemSec = (job) => {
        const st = job?.status;
        if (st === 'IN_PROGRESS') {
          const directEta = Number(job?.eta_sec || 0);
          if (directEta > 0) return directEta;
          const fullSec = estimateFullEncodeSec(job, job?.worker_name);
          const pct = Math.max(0, Math.min(99, Number(job?.progress || 0)));
          return Math.max(10, Math.round(fullSec * ((100.0 - pct) / 100.0)));
        }
        if (st === 'STAGING' || st === 'PREMUX') {
          const transferRem = estimateTransferRemSec(job, 85.0);
          const encodeSec = estimateFullEncodeSec(job, job?.worker_name);
          return transferRem + encodeSec;
        }
        if (st === 'COPYING') {
          return estimateTransferRemSec(job, 65.0);
        }
        return 0;
      };

      const jobWeight = (job) => {
        const rIn = normResBucket(job?.res_in);
        const rOut = normResBucket(job?.res_out || job?.res_in);
        if (rIn === '4K' || rOut === '4K') return 4;
        if (rIn === '1080p' || rOut === '1080p') return 2;
        return 1;
      };

      const pickEarliestSlots = (slots, need) => {
        const k = Math.max(1, Math.min(slots.length, need));
        const order = slots.map((t, i) => ({ t, i })).sort((a, b) => a.t - b.t);
        const idxs = order.slice(0, k).map(x => x.i);
        const availAt = order[k - 1].t;
        return { idxs, availAt };
      };

      const workerCanTakeJob = (w, job) => {
        const cap = String(w?.capability || 'all').toLowerCase();
        const wNeed = jobWeight(job);
        const is4k = (wNeed === 4);
        const is1080 = (wNeed === 2);
        if (cap === '4k') return is4k;
        if (cap === 'sd' || cap === '720p') return !is4k && !is1080;
        if (cap === '1080p') return !is4k;
        if (w?.allow_4k === false && is4k) return false;
        const maxSlots = Math.max(1, parseInt(w?.max_slots || 1, 10) || 1);
        if (maxSlots < wNeed) return false;
        return true;
      };

      const enabledOnline = (this.onlineWorkers || []).filter(w => w.is_enabled !== false);
      const workerPool = enabledOnline.length ? enabledOnline : (this.onlineWorkers || []);
      const workerSlotsMap = new Map();
      for (const w of workerPool) {
        const maxSlots = Math.max(1, parseInt(w.max_slots || 1, 10) || 1);
        workerSlotsMap.set(w.worker_name, { worker: w, slots: new Array(maxSlots).fill(0) });
      }

      let maxUnassignedActiveSec = 0;
      const runningJobs = queue.filter(j => RUNNING_STATES.includes(j.status));
      for (const j of runningJobs) {
        const remSec = estimateActiveJobRemSec(j);
        const entry = j.worker_name ? workerSlotsMap.get(j.worker_name) : null;
        if (entry && entry.slots.length > 0) {
          const picked = pickEarliestSlots(entry.slots, jobWeight(j));
          const finishAt = picked.availAt + remSec;
          for (const idx of picked.idxs) entry.slots[idx] = finishAt;
        } else {
          if (remSec > maxUnassignedActiveSec) maxUnassignedActiveSec = remSec;
        }
      }

      const waitingJobs = queue.filter(j => j.status === 'WAITING');
      if (waitingJobs.length > 0) {
        if (workerSlotsMap.size === 0) {
          let serialSec = maxUnassignedActiveSec;
          for (const j of waitingJobs) {
            serialSec += estimateFullEncodeSec(j, '');
          }
          return Math.round(serialSec);
        }
        for (const j of waitingJobs) {
          const allEntries = Array.from(workerSlotsMap.values());
          const eligible = allEntries.filter(e => workerCanTakeJob(e.worker, j));
          const candidates = eligible.length > 0 ? eligible : allEntries;
          const wNeed = jobWeight(j);

          let bestEntry = null;
          let bestPicked = null;
          let bestAvailAt = Infinity;
          let bestFinishAt = Infinity;

          for (const entry of candidates) {
            const jobDurOnWorker = estimateFullEncodeSec(j, entry.worker.worker_name);
            const picked = pickEarliestSlots(entry.slots, wNeed);
            const availAt = picked.availAt;
            const finishAt = availAt + jobDurOnWorker;
            if (availAt < bestAvailAt - 1 || (Math.abs(availAt - bestAvailAt) <= 1 && finishAt < bestFinishAt)) {
              bestAvailAt = availAt;
              bestFinishAt = finishAt;
              bestEntry = entry;
              bestPicked = picked;
            }
          }
          if (bestEntry && bestPicked) {
            for (const idx of bestPicked.idxs) bestEntry.slots[idx] = bestFinishAt;
          }
        }
      }

      let maxCompletionSec = maxUnassignedActiveSec;
      for (const entry of workerSlotsMap.values()) {
        for (const tSec of entry.slots) {
          if (tSec > maxCompletionSec) maxCompletionSec = tSec;
        }
      }
      return Math.round(maxCompletionSec);
    },
    generatedWinInstallPsCmd() {
      const m = this.addWorkerModal || {};
      const baseUrl = (m.managerUrl || (typeof window !== 'undefined' ? window.location.origin : 'http://192.168.1.100:8383')).trim().replace(/\/+$/, '');
      const qs = new URLSearchParams();
      qs.set('name', (m.workerName || 'Win-Worker-1').trim() || 'Win-Worker-1');
      if ((m.winInputsDir || '').trim()) qs.set('inputs', m.winInputsDir.trim());
      if ((m.winOutputsDir || '').trim()) qs.set('outputs', m.winOutputsDir.trim());
      if (m.winUseCustomTemp && (m.winTempDir || '').trim()) qs.set('temp', m.winTempDir.trim());
      qs.set('autostart', m.winAutostartMode || 'after_login');
      qs.set('sleep_mode', m.winSleepMode || 'encoding');
      qs.set('wake_check', m.winWakeCheck === false ? 'false' : 'true');
      qs.set('tray_minimize', m.winTray === false ? 'false' : 'true');
      qs.set('desktop_shortcut', m.winDesktopShortcut === false ? 'false' : 'true');
      qs.set('start_menu_shortcut', m.winStartMenuShortcut === false ? 'false' : 'true');
      qs.set('lang', this.uiLang || 'en');
      if (m.includeBinaries) qs.set('binaries', 'true');
      if (m.dlToken) qs.set('dl', m.dlToken);
      const zipUrl = `${baseUrl}/api/worker/download_windows_zip?${qs.toString()}`;
      return `mkdir "%LOCALAPPDATA%\\HB_Worker" 2>nul & curl.exe -fsSL "${zipUrl}" -o "%LOCALAPPDATA%\\HB_Worker\\hb_pkg.zip" && tar.exe -xf "%LOCALAPPDATA%\\HB_Worker\\hb_pkg.zip" -C "%LOCALAPPDATA%\\HB_Worker" && del "%LOCALAPPDATA%\\HB_Worker\\hb_pkg.zip" && "%LOCALAPPDATA%\\HB_Worker\\HBWorker.exe"`;
    },
    generatedLinuxComposeYaml() {
      const m = this.addWorkerModal || {};
      const cs = this.uiLang === 'cs';
      const tz = (() => { try { return Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC'; } catch (e) { return 'UTC'; } })();
      const wName = (m.linWorkerName || 'Linux-Worker-1').trim() || 'Linux-Worker-1';
      const mgrUrl = (m.managerUrl || window.location.origin).trim().replace(/\/+$/, '');
      const inDir = (m.linInputsDir || '').trim();
      const outDir = (m.linOutputsDir || '').trim();
      const dataDir = (m.linDataDir || this.linuxAutoDataDir(wName)).trim() || this.linuxAutoDataDir(wName);
      const tmpDir = this.linuxWorkerTempDir(m.linTempDir, wName);
      const workerImage = this.state.ghcr_images?.worker || 'ghcr.io/maseklab/hb-worker:latest';
      const envTmpLine = m.linUseCustomTemp
        ? `\n      - LOCAL_TEMP_DIR=/temp                                # ${cs ? 'Vlastní složka pro dočasné soubory (Staging)' : 'Custom directory for temporary staging files'}`
        : '';
      const volInLine = `      - ${inDir || '/mnt/nas/inputs'}:/inputs:ro          # ${cs ? 'Sdílený vstupní adresář se zdrojovými videi (jen pro čtení)' : 'Shared input directory with source videos (read-only)'}`;
      const volOutLine = `      - ${outDir || '/mnt/nas/outputs'}:/outputs          # ${cs ? 'Sdílený výstupní adresář pro hotová videa' : 'Shared output directory for encoded videos'}`;
      const volDataLine = `      - ${dataDir}:/data                                  # ${cs ? 'Sjednocené úložiště workeru (/data/temp, /data/binaries, /data/worker.log)' : 'Unified worker storage (/data/temp, /data/binaries, /data/worker.log)'}`;
      const volTmpLine = m.linUseCustomTemp
        ? `      - ${tmpDir}:/temp                                     # ${cs ? 'Rychlé lokální NVMe/SSD úložiště pro dočasné soubory' : 'Fast local NVMe/SSD storage for temp encoding files'}`
        : `      # - /mnt/nvme/hb_temp:/temp                           # ${cs ? 'VOLITELNÉ: Samostatný rychlý NVMe scratch disk (jinak se automaticky použije /data/temp)' : 'OPTIONAL: Separate fast NVMe scratch disk (otherwise /data/temp is used automatically)'}`;
      const sockLine = m.linMountDockerSock
        ? `\n      - /var/run/docker.sock:/var/run/docker.sock           # ${cs ? 'Automatické GHCR aktualizace z webu Managera' : 'Automatic GHCR self-updates triggered from Manager UI'}`
        : `\n      # - /var/run/docker.sock:/var/run/docker.sock         # ${cs ? 'VOLITELNÉ: Povolí automatické GHCR aktualizace kontejneru z Managera' : 'OPTIONAL: Enables automatic GHCR container self-updates from Manager'}`;
      return [
        `services:`,
        `  hb-worker-${wName.toLowerCase().replace(/[^a-z0-9_-]/g, '-')}:`,
        `    image: ${workerImage}`,
        `    container_name: hb-worker-${wName.toLowerCase().replace(/[^a-z0-9_-]/g, '-')}`,
        `    restart: unless-stopped`,
        `    environment:`,
        `      - WORKER_NAME=${wName}                                # ${cs ? 'Unikátní název tohoto workeru v clusteru' : 'Unique name of this worker in the cluster'}`,
        `      - MANAGER_URL=${mgrUrl}                               # ${cs ? 'Adresa běžícího HB Cluster Managera' : 'URL of the running HB Cluster Manager'}${envTmpLine}`,
        `      - TZ=${tz}                                            # ${cs ? 'Časové pásmo (pro správný čas v logu)' : 'Timezone (for accurate timestamps in logs)'}`,
        `    volumes:`,
        volInLine,
        volOutLine,
        volDataLine,
        `${volTmpLine}${sockLine}`,
        `    labels:`,
        `      - "com.centurylinklabs.watchtower.enable=false"       # ${cs ? 'Aktualizace řídí HB Manager (zabraňuje kolizi s Watchtower)' : 'Managed by HB Manager (prevents Watchtower collisions)'}`
      ].join('\n');
    },
    generatedLinuxCliCommand() {
      const m = this.addWorkerModal || {};
      const tz = (() => { try { return Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC'; } catch (e) { return 'UTC'; } })();
      const wName = (m.linWorkerName || 'Linux-Worker-1').trim() || 'Linux-Worker-1';
      const cName = `hb-worker-${wName.toLowerCase().replace(/[^a-z0-9_-]/g, '-')}`;
      const mgrUrl = (m.managerUrl || window.location.origin).trim().replace(/\/+$/, '');
      const inDir = (m.linInputsDir || '/mnt/nas/inputs').trim();
      const outDir = (m.linOutputsDir || '/mnt/nas/outputs').trim();
      const rawDataDir = (m.linDataDir || this.linuxAutoDataDir(wName)).trim() || this.linuxAutoDataDir(wName);
      const cliDataDir = rawDataDir.startsWith('./') ? `$(pwd)/${rawDataDir.slice(2)}` : (rawDataDir === '.' ? '$(pwd)' : rawDataDir);
      const tmpDir = this.linuxWorkerTempDir(m.linTempDir, wName);
      const workerImage = this.state.ghcr_images?.worker || 'ghcr.io/maseklab/hb-worker:latest';
      const parts = [
        `docker run -d --name ${cName} --restart unless-stopped`,
        `-e WORKER_NAME="${wName}"`,
        `-e MANAGER_URL="${mgrUrl}"`,
        `-e TZ="${tz}"`,
        `-v "${cliDataDir}:/data"`
      ];
      if (m.linUseCustomTemp) {
        parts.push(`-e LOCAL_TEMP_DIR="/temp"`, `-v "${tmpDir}:/temp"`);
      }
      parts.push(`-v "${inDir}:/inputs:ro"`, `-v "${outDir}:/outputs"`);
      if (m.linMountDockerSock) {
        parts.push(`-v /var/run/docker.sock:/var/run/docker.sock`);
      }
      parts.push(`--label "com.centurylinklabs.watchtower.enable=false"`, workerImage);
      return parts.join(' ');
    },
    parsedChangelogSections() {
      const raw = this.changelogText || '';
      if (!raw.trim()) return [];
      const esc = (s) => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
      const fmt = (s) => esc(s)
        .replace(/\*\*(.+?)\*\*/g, '<strong class="text-white font-bold">$1</strong>')
        .replace(/`([^`]+)`/g, '<code class="bg-slate-950 text-sky-300 border border-slate-700 px-1.5 py-0.5 rounded font-mono text-xs">$1</code>');

      const blocks = [];
      let curTitle = '';
      let curLines = [];

      for (const ln of raw.split(/\r?\n/)) {
        const t = ln.trim();
        if (t.startsWith('## ')) {
          if (curTitle) blocks.push({ title: curTitle, lines: curLines });
          curTitle = t.slice(3).trim();
          curLines = [];
        } else if (curTitle) {
          curLines.push(t);
        }
      }
      if (curTitle) blocks.push({ title: curTitle, lines: curLines });

      return blocks.map(b => {
        let html = '';
        let inUl = false;
        for (const t of b.lines) {
          if (!t) { if (inUl) { html += '</ul>'; inUl = false; } continue; }
          if (t.startsWith('### ')) {
            if (inUl) { html += '</ul>'; inUl = false; }
            html += `<h3 class="text-xs font-bold uppercase tracking-wider text-emerald-400 mt-3 mb-1.5 px-1">${fmt(t.slice(4))}</h3>`;
          } else if (t.startsWith('- ')) {
            if (!inUl) { html += '<ul class="space-y-2 pl-3 my-2">'; inUl = true; }
            html += `<li class="text-sm text-slate-300 leading-relaxed flex items-start gap-2.5"><span class="text-sky-400 font-bold mt-1">•</span><span class="flex-1">${fmt(t.slice(2))}</span></li>`;
          } else {
            if (inUl) { html += '</ul>'; inUl = false; }
            html += `<p class="text-sm text-slate-400 leading-relaxed">${fmt(t)}</p>`;
          }
        }
        if (inUl) html += '</ul>';
        return { title: b.title, html };
      });
    },
    settingsNavItems() {
      return [
        { id: 'general', icon: 'sliders', label: this.t('set_nav_general'), desc: this.t('set_nav_general_sub') },
        { id: 'appearance', icon: 'eye', label: this.t('set_nav_appearance'), desc: this.t('set_nav_appearance_sub') },
        { id: 'video', icon: 'film', label: this.t('set_nav_video'), desc: this.t('set_nav_video_sub') },
        { id: 'smb', icon: 'folder', label: this.t('set_nav_smb'), desc: this.t('set_nav_smb_sub') },
        { id: 'binaries', icon: 'wrench', label: this.t('set_nav_binaries'), desc: this.t('set_nav_binaries_sub') },
        { id: 'security', icon: 'shield', label: this.t('set_nav_security'), desc: this.t('set_nav_security_sub') },
        { id: 'about', icon: 'info', label: this.t('set_nav_about'), desc: this.t('set_nav_about_sub') }
      ];
    },
    statsWorkersList() {
      const raw = this.state.analytics?.stats_workers;
      if (Array.isArray(raw) && raw.length > 0) return raw;
      const pw = this.state.analytics?.per_worker || {};
      const list = Object.keys(pw).map(wn => ({
        worker_name: wn,
        count: Number(pw[wn]?.count || 0),
        src_bytes: Number(pw[wn]?.src_bytes || 0),
        out_bytes: Number(pw[wn]?.out_bytes || 0)
      }));
      list.sort((a, b) => (a.worker_name === 'Unknown' ? 0 : 1) - (b.worker_name === 'Unknown' ? 0 : 1) || (b.count - a.count) || a.worker_name.localeCompare(b.worker_name));
      return list;
    },
    headerClusterUpdateNotice() {
      // Během aktualizace štítek nad názvem neukazujeme: průběh je v pruhu pod záložkami (vidět ve všech záložkách)
      // a měnící se text štítku jen poskakoval do šířky (rozhodnutí autora 2026-11-17)
      if (this.otaActive) return null;
      if (this.state.github_updater?.update_available && !this.state.settings?.auto_update_cluster) {
        return {
          id: 'cluster_upd',
          // čitelnost: tmavší fialová (vyšší kontrast s bílou), polotučné písmo, pulzuje jen záře kolem – text zůstává plně ostrý
          cls: 'bg-violet-700 hover:bg-violet-600 text-white text-xs font-semibold tracking-wide shadow ring-1 ring-violet-300/80',
          pulseCls: 'hb-upd-glow',
          dotCls: '',
          icon: 'arrow-up',
          text: `${this.t('hdr_update_cluster_btn')} v${this.state.github_updater.remote_version || this.state.manager_version}`,
          // mobil: plný text se nevejde (přetékal přes čip workerů) → krátce „Aktualizovat“, verze je v bublině / po klepnutí
          shortText: this.t('hdr_update_short'),
          title: '',
          action: 'trigger_update',
          disabled: this.isUpdatingFromGitHub || this.updateWaitActive
        };
      }
      if (this.stateLoaded && !this.showOnboardingModal && (this.state.workers || []).length === 0) {
        return {
          id: 'onboarding_wizard',
          cls: 'bg-sky-500/95 hover:bg-sky-400 text-slate-950 text-xs font-black shadow ring-1 ring-sky-300',
          dotCls: '',
          icon: 'compass',
          text: this.t('hdr_setup_wizard_badge'),
          shortText: this.t('hdr_setup_wizard_short'),
          title: this.t('hdr_setup_wizard_tip'),
          action: 'open_onboarding',
          disabled: false
        };
      }
      return null;
    },
    headerOperationalNotices() {
      const items = [];
      if (this.secWkPending.length > 0) {
        items.push({
          id: 'wk_pending',
          cls: 'bg-amber-500 hover:bg-amber-400 text-slate-950 font-mono font-bold',
          icon: 'shield',
          text: this.t('hdr_wk_pending', this.secWkPending.length),
          title: '',
          action: 'open_workers_pending',
          disabled: false
        });
      }
      if (this.state.output_storage?.is_low_space || this.totalWaitingDiskCount > 0) {
        items.push({
          id: 'disk',
          cls: 'bg-amber-500 hover:bg-amber-400 text-slate-950 font-mono font-bold',
          icon: 'alert',
          text: `${this.t('output_disk_hdr')}: ${this.state.output_storage?.free_gb || 0} GB ${this.t('free_lbl')}${this.totalWaitingDiskCount > 0 ? ' (' + this.totalWaitingDiskCount + '×)' : ''}`,
          title: '',
          action: 'open_disk_hold',
          disabled: false
        });
      }
      if (this.isBuildRunning) {
        items.push({
          id: 'build',
          cls: 'bg-sky-600/90 hover:bg-sky-500 text-white font-mono font-bold',
          icon: 'cog',
          text: `${this.t('set_tool_preparing')} (${Number(this.state.binary_builder?.progress || 0).toFixed(0)}%)`,
          title: '',
          action: 'open_queue',
          disabled: false
        });
      } else if (this.missingBinariesCount > 0) {
        items.push({
          id: 'missing_tools',
          cls: 'bg-rose-600 hover:bg-rose-500 text-white font-mono font-bold',
          icon: 'alert',
          text: `${this.t('missing_tools_hdr')} (${this.missingBinariesCount})`,
          title: '',
          action: 'open_binaries',
          disabled: false
        });
      } else if (this.anyToolNeedsUpdate) {
        items.push({
          id: 'upd_tools',
          cls: 'bg-amber-400 hover:bg-amber-300 text-slate-950 font-mono font-bold',
          icon: 'arrow-up-circle',
          text: `${this.t('update_tools_hdr')}: ${this.updatesAvailableSummary}`,
          title: this.updatesAvailableSummary,
          action: 'open_binaries',
          disabled: false
        });
      }
      return items.slice(0, 2);
    }
  },
  watch: {
    'addWorkerModal.linWorkerName'(nv, ov) {
      // the suggested data folder follows the worker name (each worker needs its own /data); a typed folder stays
      const m = this.addWorkerModal;
      const n = this.normalizeWorkerName(nv);
      if (m.linDeployMode !== 'compose' && this.isLinuxAutoDataDir(m.linDataDir, ov)) m.linDataDir = this.linuxAutoDataDir(n);
      if (n !== nv) m.linWorkerName = n;   // diakritika / mezery se opraví rovnou při psaní
    },
    'addWorkerModal.workerName'(nv) {
      const n = this.normalizeWorkerName(nv);
      if (n !== nv) this.addWorkerModal.workerName = n;
    },
    canDragReorder(val) {
      if (this.sortableInstance) {
        this.sortableInstance.option('disabled', !val);
      }
    }
  },
  methods: {
    t(key, ...args) {
      const dict = (window.HB_LOCALES && window.HB_LOCALES[this.uiLang]) ? window.HB_LOCALES[this.uiLang] : (window.HB_LOCALES?.en || {});
      let s = dict[key] || (window.HB_LOCALES?.en?.[key]) || key;
      if (args.length > 0 && typeof s === 'string') {
        args.forEach((v, i) => { s = s.replaceAll(`{${i}}`, v ?? ''); });
      }
      return s;
    },
    isJobFrozenOrDiskPaused(job) {
      if (!job) return false;
      if (job.is_paused) return true;
      const hr = String(job.disk_hold_reason || '');
      return hr.startsWith('PAUZA') || hr.startsWith('PAUSED') || hr.startsWith('POZASTAVENO');
    },
    retryTooltip(job) {
      const cnt = Number(job?.attempt_count || 0);
      if (cnt <= 0) return '';
      const errTxt = this.translateError(job) || job?.error_detail || this.t('err_prev_attempt_failed');
      return this.t('retry_tooltip_fmt', cnt, errTxt);
    },
    buildHistorySvgPoints(histArr, key, maxVal = 100.0, width = 360, height = 56) {
      const list = Array.isArray(histArr) ? histArr : [];
      if (list.length < 2) {
        return { line: `0,${height} ${width},${height}`, area: `0,${height} ${width},${height} ${width},${height} 0,${height}` };
      }
      const effMax = Math.max(1.0, Number(maxVal) || 100.0);
      const stepX = width / Math.max(1, list.length - 1);
      const pts = list.map((item, idx) => {
        const val = Math.max(0.0, Math.min(effMax, Number(item?.[key] || 0.0)));
        const x = (idx * stepX).toFixed(1);
        const y = (height - (val / effMax) * (height - 6) - 3).toFixed(1);
        return `${x},${y}`;
      });
      const lineStr = pts.join(' ');
      const areaStr = `0,${height} ${lineStr} ${width},${height}`;
      return { line: lineStr, area: areaStr };
    },
    async uploadCustomBinary(ev) {
      const f = ev?.target?.files?.[0];
      if (!f) return;
      this.isUploadingCustomBinary = true;
      try {
        const fd = new FormData();
        fd.append('file', f);
        fd.append('is_custom', 'true');
        if (this.manualUploadVersion.trim()) {
          fd.append('version', this.manualUploadVersion.trim());
        }
        const r = await this.apiFetch(`/api/v2/binaries/upload/${encodeURIComponent(this.manualUploadOs)}/${encodeURIComponent(this.manualUploadTool)}`, {
          method: 'POST',
          body: fd
        }, 0);
        if (r.ok) {
          const d = await r.json();
          this.toast(this.t('toast_bin_uploaded', d.os_type, d.tool, d.version));
          this.manualUploadVersion = '';
          this.fetchState(true);
        } else {
          const err = await r.json().catch(() => ({}));
          this.toast(err.detail || 'Upload failed!', 'error');
        }
      } catch (e) {
        this.toast('Upload error!', 'error');
      } finally {
        this.isUploadingCustomBinary = false;
        if (ev?.target) ev.target.value = '';
      }
    },
    async apiFetch(url, opts = {}, timeoutMs = 20000) {
      // Jednotný obal pro všechna API volání: při krátkém restartu Managera (recreate
      // kontejneru, výpadek sítě) nikdy nevyhodí neošetřenou výjimku do konzole a vrátí
      // objekt kompatibilní s Response ({ ok: false, status: 0, json() }).
      const ctrl = (timeoutMs > 0 && !opts.signal) ? new AbortController() : null;
      const tid = ctrl ? setTimeout(() => ctrl.abort(), timeoutMs) : null;
      try {
        const res = await fetch(url, ctrl ? { ...opts, signal: ctrl.signal } : opts);
        // zapnuté přihlášení a vypršelá / odebraná relace → na přihlašovací stránku (jen jednou)
        if (res.status === 401 && !window._hbAuthRedirect && String(url).startsWith('/api/') && !String(url).startsWith('/api/auth/')) {
          window._hbAuthRedirect = true;
          window.location.href = '/login?next=' + encodeURIComponent(window.location.pathname + window.location.search);
        }
        return res;
      } catch (e) {
        return { ok: false, status: 0, statusText: String(e && e.name || 'NetworkError'), json: async () => ({}), text: async () => '' };
      } finally {
        if (tid) clearTimeout(tid);
      }
    },
    async checkGhcrUpdate(force = true, silent = false) {
      this.isCheckingGitHub = true;
      try {
        const r = await this.apiFetch('/api/v2/system/update_check', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ force })
        });
        if (r.ok) {
          const d = await r.json().catch(() => ({}));
          this.state.github_updater = d.github_updater || {};
          if (silent) return;
          if (this.state.github_updater.last_error) {
            this.toast(this.state.github_updater.last_error, 'warn');
          } else if (this.state.github_updater.update_available) {
            this.toast(this.t('toast_gh_new_ver', this.state.github_updater.remote_version || this.state.manager_version));
          } else {
            this.toast(this.t('toast_gh_uptodate', this.state.manager_version));
          }
        } else if (!silent) {
          this.toast(this.t('toast_mgr_unreachable'), 'warn');
        }
      } finally {
        this.isCheckingGitHub = false;
      }
    },
    async triggerClusterUpdate() {
      if (this.isUpdatingFromGitHub) return;
      this.isUpdatingFromGitHub = true;
      this.otaClientStart = Date.now();
      this.otaNow = Date.now();
      this.otaWentDown = false;
      const startVer = this.state.manager_version;
      const r = await this.apiFetch('/api/v2/system/update_apply', { method: 'POST' });
      if (!r.ok) {
        const err = await r.json().catch(() => ({}));
        this.toast(err.detail || this.t('toast_update_failed'), 'error');
        this.isUpdatingFromGitHub = false;
        this.otaClientStart = 0;
        return;
      }
      this.toast(this.t('toast_update_pulling'));
      this.waitForManagerRecreateAndReload(startVer);
    },
    async waitForManagerRecreateAndReload(startVer) {
      // Fáze 1: Manager stahuje obraz a přetváří vlastní kontejner. Polling živého postupu
      // (/api/selfupdate/status), detekce výpadku a návratu Managera -> reload s cache-busting parametrem.
      this.updateWaitActive = true;
      if (!this.otaClientStart) this.otaClientStart = Date.now();
      const deadline = Date.now() + 15 * 60 * 1000;
      let wentDown = false;
      while (Date.now() < deadline) {
        await new Promise(res => setTimeout(res, 1500));
        this.otaNow = Date.now();
        const r = await this.apiFetch('/api/selfupdate/status', { cache: 'no-store' }, 5000);
        if (!r.ok) { wentDown = true; this.otaWentDown = true; continue; }
        const d = await r.json().catch(() => ({}));
        const gu = d.github_updater || {};
        this.state.github_updater = gu;
        if ((d.manager_version && d.manager_version !== startVer) || (wentDown && !gu.updating)) {
          this.otaWentDown = false;
          this.toast(this.t('toast_update_done_reload'));
          setTimeout(() => { window.location.href = window.location.pathname + '?v=' + Date.now(); }, 1200);
          return;
        }
        if (!gu.updating && (gu.phase === 'error' || gu.phase === 'unchanged')) {
          this.toast(gu.last_error || this.t('toast_update_failed'), gu.phase === 'unchanged' ? 'warn' : 'error');
          break;
        }
      }
      this.updateWaitActive = false;
      this.isUpdatingFromGitHub = false;
      this.otaWentDown = false;
      this.otaClientStart = 0;
    },
    formatElapsedShort(sec) {
      const s = Math.max(0, Math.round(Number(sec) || 0));
      if (s < 60) return `${s} s`;
      return `${Math.floor(s / 60)} min ${String(s % 60).padStart(2, '0')} s`;
    },
    updateDynamicFavicon() {
      try {
        const totalRemaining = this.totalActiveCount + this.totalWaitingCount + this.totalWaitingDiskCount;
        const sysBuildFailed = String(this.state.binary_builder?.status || '').toUpperCase() === 'ERROR';
        const lowDiskCrit = Boolean(this.state.output_storage?.is_low_space);
        const hasErr = (this.errorCount > 0) || sysBuildFailed || ((this.missingBinariesCount > 0) && !this.isBuildRunning) || lowDiskCrit || (this.onlineWorkers.length === 0 && totalRemaining > 0);
        const hasGhUpd = Boolean(this.state.github_updater?.update_available);
        const hasWrkOrToolUpd = (this.outdatedWorkersCount > 0) || Boolean(this.anyToolNeedsUpdate);
        const hasUpd = hasGhUpd || hasWrkOrToolUpd;
        const isPausedOrWaitingDisk = Boolean(this.state.maintenance_mode) || !this.isClusterIntakeActive || this.allActiveFrozen || (this.totalWaitingDiskCount > 0) || (this.totalActiveCount === 0 && this.totalPausedCount > 0);

        const isIdleClean = (totalRemaining === 0 && !hasErr && !isPausedOrWaitingDisk);
        let bodyHex = '#0284c7';
        let rimHex = '#38bdf8';
        let mode = 'idle';

        if (!isIdleClean) {
          if (hasErr) {
            bodyHex = '#ef4444';
            rimHex = '#dc2626';
          } else if (isPausedOrWaitingDisk) {
            bodyHex = '#f59e0b';
            rimHex = '#fbbf24';
          } else {
            bodyHex = '#10b981';
            rimHex = '#34d399';
          }
          if (totalRemaining <= 0) {
            mode = hasErr ? 'err' : 'idle';
          } else if (totalRemaining <= 9) {
            mode = 'd1';
          } else if (totalRemaining <= 99) {
            mode = 'd2';
          } else {
            mode = 'inf';
          }
        }

        // Aktualizace (Manageru, workerů i nástrojů) = vždy fialový okraj
        const strokeHex = hasErr ? '#fecaca' : (hasUpd ? '#a78bfa' : rimHex);
        const favKey = `svg_v4l_${bodyHex}_${strokeHex}_${mode}_${totalRemaining > 99 ? 'inf' : totalRemaining}`;
        if (this._lastFaviconKey === favKey) return;
        this._lastFaviconKey = favKey;

        document.querySelectorAll('link[rel*="icon"]:not(#hb-favicon)').forEach(n => n.remove());
        let el = document.getElementById('hb-favicon');
        if (!el) {
          el = document.createElement('link');
          el.id = 'hb-favicon';
          el.rel = 'icon';
          document.head.appendChild(el);
        }

        const boxSvg = `<rect x="13" y="13" width="230" height="230" rx="46" fill="${bodyHex}" stroke="${strokeHex}" stroke-width="26"/>`;

        let inner = '';
        if (mode === 'idle') {
          const checkPath = 'M68 132 L110 174 L190 86';
          inner =
            `<path d="${checkPath}" fill="none" stroke="rgba(0,0,0,0.28)" stroke-width="38" stroke-linecap="round" stroke-linejoin="round"/>` +
            `<path d="${checkPath}" fill="none" stroke="#ffffff" stroke-width="28" stroke-linecap="round" stroke-linejoin="round"/>`;
        } else if (mode === 'inf') {
          const inf = favLemniscatePath(128, 128, 76, 40);
          inner =
            `<path d="${inf}" fill="none" stroke="rgba(0,0,0,0.38)" stroke-width="36" stroke-linejoin="round"/>` +
            `<path d="${inf}" fill="none" stroke="#ffffff" stroke-width="26" stroke-linejoin="round"/>`;
        } else if (mode === 'd1') {
          inner = favGlyphRow(String(totalRemaining), 162, 26, 0, '#ffffff', 1.08);
        } else if (mode === 'd2') {
          inner = favGlyphRow(String(totalRemaining), 130, 22, 8, '#ffffff', 1.0);
        } else {
          inner = favGlyphRow('!', 160, 28, 0, '#ffffff', 1.0);
        }

        const svgStr = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 256 256">${boxSvg}${inner}</svg>`;
        el.type = 'image/svg+xml';
        el.href = 'data:image/svg+xml;utf8,' + encodeURIComponent(svgStr);
      } catch (e) {}
    },
    filterLogLinesArray(lines) {
      if (!Array.isArray(lines)) return [];
      const q = (this.logsSearchQuery || '').toLowerCase().trim();
      const errRe = /(chyba|error|selhal|failed|warn|varování|timeout|nenalezen|missing|waiting_disk|storage guard|kritická|critical|rescue|přeskočen|skipped|vypnut|disabled|audit|pauza|frozen)/i;
      const progRe = /(Stav úlohy\s+\d+:|Job\s+\d+\s+progress:|Stahování úlohy\s+\d+:|Staging job\s+\d+:|HTTP Relay stahování|HTTP Relay download|Kopírování do [Oo]utputs:|Copying to [Oo]utputs:|Pre-Mux\s+[12]\/2)/i;
      return lines.filter(ln => {
        const s = String(ln || '');
        if (this.logsHideProgress && progRe.test(s)) return false;
        if (this.logsOnlyErrors && !errRe.test(s)) return false;
        if (q && !s.toLowerCase().includes(q)) return false;
        return true;
      });
    },
    async saveLanguagePref() {
      this.langLockUntil = Date.now() + 3000;
      localStorage.setItem('hb_ui_lang', this.uiLang);
      document.documentElement.setAttribute('lang', this.uiLang);
      if (this.state && this.state.settings) {
        this.state.settings.ui_lang = this.uiLang;
      }
      await this.saveGlobalSettings();
    },
    resetFilterPages() {
      this.queuePage = 1;
      this.historyPage = 1;
    },
    matchesJobUnifiedFilters(j) {
      if (!j) return false;
      if (this.filterPreset && j.preset_name !== this.filterPreset) return false;
      if (this.filterRes && `${j.res_in} → ${j.res_out}` !== this.filterRes) return false;
      if (this.filterWorker && j.worker_name !== this.filterWorker) return false;
      if (this.filterPremux === 'OK' && !(j.premux_status === 'OK' || j.premux_applied)) return false;
      if (this.filterPremux === 'FALLBACK' && !(j.premux_status === 'FALLBACK' || (j.is_disc_source && !j.premux_applied && j.premux_status !== 'OK'))) return false;
      if (this.filterNetFallback && !j.is_net_fallback) return false;
      if (this.filterIneffective && !j.is_ineffective) return false;
      if (this.filterVersion && this.searchInCompleted) {
        const v = this.extractJobWorkerVersion(j);
        if (this.filterVersion === 'LEGACY' && v) return false;
        if (this.filterVersion !== 'LEGACY' && v !== this.filterVersion) return false;
      }
      const q = (this.searchQuery || '').toLowerCase().trim();
      if (q) {
        const v = this.extractJobWorkerVersion(j);
        const m1 = j.original_filename && j.original_filename.toLowerCase().includes(q);
        const m2 = j.dest_filename && j.dest_filename.toLowerCase().includes(q);
        const m3 = j.worker_name && j.worker_name.toLowerCase().includes(q);
        const m4 = String(j.id).includes(q);
        const m5 = j.status && j.status.toLowerCase().includes(q);
        const m6 = j.preset_name && j.preset_name.toLowerCase().includes(q);
        const m7 = (j.source_path && j.source_path.toLowerCase().includes(q)) || (j.dest_path && j.dest_path.toLowerCase().includes(q));
        const m8 = v && v.toLowerCase().includes(q);
        if (!m1 && !m2 && !m3 && !m4 && !m5 && !m6 && !m7 && !m8) return false;
      }
      return true;
    },
    pruneUnavailableFilters() {
      const st = this.unifiedFilterStats;
      if (this.filterRes && !st.resMap[this.filterRes]) this.filterRes = '';
      if (this.filterPremux === 'OK' && st.premuxOk <= 0) this.filterPremux = '';
      if (this.filterPremux === 'FALLBACK' && st.premuxFb <= 0) this.filterPremux = '';
      if (this.filterNetFallback && st.netFallback <= 0) this.filterNetFallback = false;
      if (this.filterIneffective && st.ineffective <= 0) this.filterIneffective = false;
      if (this.filterPreset && !st.presets.includes(this.filterPreset)) this.filterPreset = '';
      if (this.filterWorker && !st.workers.includes(this.filterWorker)) this.filterWorker = '';
      if (this.filterVersion && (!this.searchInCompleted || !st.versions.includes(this.filterVersion))) this.filterVersion = '';
    },
    focusUnifiedSearch() {
      this.$nextTick(() => {
        const el = this.$refs.unifiedSearchInput;
        if (el && typeof el.focus === 'function') {
          el.focus();
          if (typeof el.select === 'function' && this.searchQuery) el.select();
        }
      });
    },
    toggleQueueFilters(forceOpen = null) {
      const nextOpen = (forceOpen === null) ? !this.queueFiltersExpanded : Boolean(forceOpen);
      this.queueFiltersExpanded = nextOpen;
      if (nextOpen) {
        if (forceOpen === null) {
          this.searchInQueue = true;
          this.searchInCompleted = false;
          this.$nextTick(() => this.pruneUnavailableFilters());
        }
        this.focusUnifiedSearch();
      }
    },
    openGlobalSearch() {
      this.switchTab('search');
    },
    toggleMonTools() {
      this.monToolsUserToggled = !this.monToolsOpen;
    },
    toggleSecNet() {
      this.secNetUserToggled = !this.secNetExpanded;
    },
    onGlobalKeyDown(ev) {
      if (!ev) return;
      const ae = document.activeElement;
      const isEditable = ae && (['INPUT', 'TEXTAREA', 'SELECT'].includes(ae.tagName) || ae.isContentEditable);
      if (ev.key === 'Escape') {
        if (this.folderBrowser?.open) { ev.preventDefault(); this.folderBrowser.open = false; return; }
        if (this.showOnboardingModal) { return; } // Průvodce je chráněn proti nechtěnému zavření klávesou Escape
        if (this.smbModal?.open) { ev.preventDefault(); this.smbModal.open = false; this.confirmingClearSmb = false; return; }
        if (this.addWorkerModal?.open) { ev.preventDefault(); this.addWorkerModal.open = false; return; }
        if (this.dupModal?.open) { ev.preventDefault(); this.dupModal.open = false; return; }
        if (this.inspectModal?.open) { ev.preventDefault(); this.inspectModal.open = false; return; }
        if (this.moreActionsOpen) { ev.preventDefault(); this.closeMoreActions(); return; }
        if (this.mobileMoreOpen) { ev.preventDefault(); this.mobileMoreOpen = false; return; }
        if (this.isSearchPanelOpen && ae === this.$refs.unifiedSearchInput) {
          if ((this.searchQuery || '').trim()) {
            this.searchQuery = '';
            this.resetFilterPages();
          } else {
            ae.blur();
          }
        }
        return;
      }
      if (ev.key === '/' && !ev.ctrlKey && !ev.metaKey && !ev.altKey && !isEditable) {
        ev.preventDefault();
        this.openGlobalSearch();
      }
    },
    onUnifiedSearchInput() {
      this.resetFilterPages();
      if (this.searchInCompleted && this.historyList.length === 0) {
        this.fetchHistory();
      }
    },
    // 4K úloha, kterou žádný worker nevezme (žádný povolený worker se 4K a aspoň 4 sloty) – P2
    async loadDbBackups() {
      const r = await this.apiFetch('/api/v2/backups');
      if (r.ok) { const d = await r.json().catch(() => ({})); this.dbBackups = Array.isArray(d.backups) ? d.backups : []; }
    },
    job4kNobody(job) {
      return !!job && job.status === 'WAITING' && job.res_out === '4K' && this.state.queue_4k_unservable === true;
    },
    workerCooldownMin(w) {
      const s = Number(w?.cooldown_left_s || 0);
      return s > 0 ? Math.max(1, Math.ceil(s / 60)) : 0;
    },
    workerBoundElsewhere(w) {
      return this.wkBoundElsewhere.some(b => b.name === w?.worker_name);
    },
    ico(name, sizeCls = 'w-4 h-4') {
      const inner = SVG_ICONS[name] || SVG_ICONS['info'];
      return `<svg class="${sizeCls}" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24">${inner}</svg>`;
    },
    hasValidInputBitrate(j) {
      const v = Number(j?.v_bitrate_in_kbps || 0);
      return v > 0 && v !== 2500;
    },
    isChangelogOpen(idx) {
      return Boolean(this.changelogOpenMap[idx]);
    },
    toggleChangelogSection(idx) {
      this.changelogOpenMap[idx] = !this.changelogOpenMap[idx];
    },
    expandAllChangelog(openAll) {
      const nextMap = {};
      this.parsedChangelogSections.forEach((_, idx) => {
        nextMap[idx] = openAll ? true : (idx < 2);
      });
      this.changelogOpenMap = nextMap;
    },
    toggleGuiPref(key) {
      this[key] = !this[key];
      if (key === 'showQueueEta') this.saveQueueEtaSetting();
      else if (key === 'enablePagination') this.togglePaginationSetting();
      else if (key === 'requireTwoClickConfirm') this.saveClickConfirmSetting();
    },
    hasFilesInDragEvent(ev) {
      if (!ev || !ev.dataTransfer) return false;
      const types = Array.from(ev.dataTransfer.types || []);
      return types.includes('Files') || types.includes('application/x-moz-file');
    },
    onWindowDragEnter(ev) {
      if (this.hasFilesInDragEvent(ev)) {
        ev.preventDefault();
        ev.stopPropagation();
        this.dragDepth++;
        this.isFileDragging = true;
      }
    },
    onWindowDragOver(ev) {
      if (this.hasFilesInDragEvent(ev)) {
        ev.preventDefault();
        ev.stopPropagation();
        ev.dataTransfer.dropEffect = 'copy';
        this.isFileDragging = true;
      }
    },
    onWindowDragLeave(ev) {
      if (this.hasFilesInDragEvent(ev)) {
        ev.preventDefault();
        ev.stopPropagation();
        this.dragDepth = Math.max(0, this.dragDepth - 1);
        if (this.dragDepth === 0) this.isFileDragging = false;
      }
    },
    async onWindowDrop(ev) {
      if (!this.hasFilesInDragEvent(ev)) return;
      ev.preventDefault();
      ev.stopPropagation();
      this.dragDepth = 0;
      this.isFileDragging = false;
      const files = ev.dataTransfer?.files;
      if (!files || !files.length) return;
      await this.importQueueFiles(Array.from(files));
    },
    // Import jednoho i více .json souborů (výběr v dialogu i přetažení na stránku); u více souborů jedno souhrnné hlášení.
    async importQueueFiles(fileList) {
      const files = Array.from(fileList || []);
      const jsons = files.filter(f => f.name.toLowerCase().endsWith('.json'));
      files.filter(f => !f.name.toLowerCase().endsWith('.json')).forEach(f => this.toast(this.t('toast_not_json_fmt', f.name), 'warn'));
      if (!jsons.length) return;
      if (jsons.length === 1) { await this.importQueueFileObj(jsons[0]); return; }
      let added = 0, skipped = 0, failed = 0;
      for (const f of jsons) {
        const res = await this.importQueueFileObj(f, true);
        if (res.ok) { added += res.added; skipped += res.skipped; } else { failed += 1; }
      }
      this.toast(this.t('toast_import_multi_fmt', jsons.length, added, skipped, failed), failed > 0 ? 'warn' : 'success');
      this.switchTab('queue'); this.fetchState(true);
    },
    async importQueueFileObj(file, silent = false) {
      const fd = new FormData(); fd.append('file', file);
      const r = await this.apiFetch('/api/v2/jobs/import', { method: 'POST', body: fd }, 0);
      if (r.ok) {
        const d = await r.json();
        if (!silent) {
          this.toast(this.t('toast_import_ok_fmt', file.name, d.added, d.skipped));
          this.switchTab('queue'); this.fetchState(true);
        }
        return { ok: true, added: Number(d.added || 0), skipped: Number(d.skipped || 0) };
      }
      if (!silent) this.toast(this.t('toast_import_err_fmt', file.name), 'error');
      return { ok: false, added: 0, skipped: 0 };
    },
    // Klik na verzi v hlavičce -> historie změn v Nastavení (samostatná záložka zrušena)
    openChangelog() {
      this.switchTab('settings', 'changelog');
    },
    markChangelogSeen() {
      const v = this.state?.manager_version || '';
      if (!v) return;
      this.changelogSeenVersion = v;
      this.saveUiPref('changelog_seen', v);
    },
    async triggerStopAllWorkers() {
      if (this.requireTwoClickConfirm && !this.confirmingStopAll) {
        this.confirmingStopAll = true;
        setTimeout(() => { this.confirmingStopAll = false; }, 4000);
        return;
      }
      this.confirmingStopAll = false;
      const r = await this.apiFetch('/api/v2/cluster/action', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ action: 'stop_all_active' }) });
      if (r && r.ok) {
        this.toast(this.t('toast_all_workers_stopped'), 'warn');
      } else {
        this.toast(this.t('toast_action_failed'), 'error');
      }
      this.fetchState(true);
    },
    async triggerStopWorker(w) {
      const name = w.worker_name;
      if (this.requireTwoClickConfirm && !this.confirmingWorkerStop[name]) {
        this.confirmingWorkerStop[name] = true;
        setTimeout(() => { delete this.confirmingWorkerStop[name]; }, 4000);
        return;
      }
      delete this.confirmingWorkerStop[name];
      const r = await this.apiFetch(`/api/v2/workers/${encodeURIComponent(name)}/stop`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ disable_worker: true }) });
      if (r.ok) {
        const d = await r.json();
        this.toast(this.t('toast_worker_stopped_fmt', name, d.stopped_jobs || 0), 'warn');
        this.fetchState(true);
      }
    },
    async triggerRemoveWorker(w) {
      if (w.active_jobs_count > 0 || w.held_disk_jobs_count > 0) {
        this.toast(this.t('wrk_locked_trash_tip'), 'warn');
        return;
      }
      const name = w.worker_name;
      if (this.requireTwoClickConfirm && !this.confirmingWorkerDelete[name]) {
        this.confirmingWorkerDelete[name] = true;
        setTimeout(() => { delete this.confirmingWorkerDelete[name]; }, 4000);
        return;
      }
      delete this.confirmingWorkerDelete[name];
      const r = await this.apiFetch(`/api/v2/workers/${encodeURIComponent(name)}`, { method: 'DELETE' });
      if (r.ok) { this.toast(this.t('toast_worker_deleted_fmt', name)); this.fetchState(true); }
      else {
        const err = await r.json().catch(() => ({}));
        this.toast(err.detail || 'Cannot delete worker!', 'error');
      }
    },
    isWorkerPremuxReady(w) {
      if (!w || !w.use_local_temp) return false;
      if (w.premux_capable !== undefined) return Boolean(w.premux_capable);
      return Boolean(w.telemetry?.diag?.mkvmerge_ok);
    },
    toolDisplayLabel(k) {
      const m = { hbcli: 'HBCLI', ffmpeg: 'FFMPEG', ffprobe: 'FFPROBE', mkvpropedit: 'MKVPROPEDIT', mkvmerge: 'MKVMERGE', '7z': '7-ZIP' };
      return m[k] || k.toUpperCase();
    },
    toolTooltipLabel(k) {
      const m = { hbcli: 'HandBrakeCLI', ffmpeg: 'FFmpeg', ffprobe: 'FFprobe', mkvpropedit: 'MKVpropedit', mkvmerge: 'MKVmerge', '7z': '7-Zip' };
      return m[k] || String(k || '').toUpperCase();
    },
    getWorkerOsKey(w) {
      const s = String(w?.telemetry?.diag?.os || '').toLowerCase();
      return s.includes('linux') ? 'linux' : (s.includes('win') ? 'windows' : '');
    },
    isWorkerToolPendingSync(w, toolKey) {
      if (!w || !w.is_online || !w.telemetry?.diag) return false;
      const osKey = this.getWorkerOsKey(w);
      if (!osKey) return false;
      const central = this.state.binaries?.[osKey]?.[toolKey];
      if (!central || !central.available || !central.md5) return false;
      const d = w.telemetry.diag;
      const map = { hbcli: d.hb_md5, ffmpeg: d.ffmpeg_md5, ffprobe: d.ffprobe_md5, mkvpropedit: d.mkvpropedit_md5, mkvmerge: d.mkvmerge_md5, '7z': d.sevenzip_md5 };
      if (map[toolKey]) return map[toolKey] !== central.md5;
      return false;
    },
    workerPendingBinaries(w) {
      return ['hbcli', 'ffmpeg', 'ffprobe', 'mkvpropedit', 'mkvmerge', '7z'].filter(k => this.isWorkerToolPendingSync(w, k)).map(k => this.toolDisplayLabel(k));
    },
    workerPendingToolNames(w) {
      return ['hbcli', 'ffmpeg', 'ffprobe', 'mkvpropedit', 'mkvmerge', '7z'].filter(k => this.isWorkerToolPendingSync(w, k)).map(k => this.toolTooltipLabel(k));
    },
    workerMissingTools(w) {
      const d = w?.telemetry?.diag;
      if (!d) return [];
      const arr = [];
      if (!d.hb_version || d.hb_version === 'CHYBÍ' || d.hb_version === 'MISSING') arr.push('HBCLI');
      if (!d.ffmpeg_ok || !d.ffprobe_ok) arr.push('FFmpeg');
      if (!d.mkvmerge_ok || !d.mkvpropedit_ok) arr.push('MKVToolNix');
      if (d.sevenzip_ok === false) arr.push('7-Zip');
      return arr;
    },
    getWorkerToolBadges(w) {
      const d = w?.telemetry?.diag || {};
      const osKey = this.getWorkerOsKey(w);
      const bins = (this.state.binaries && osKey) ? (this.state.binaries[osKey] || {}) : {};
      const bld = this.isBuildRunning;
      const items = [];

      const hbC = bins.hbcli || {};
      const hbPend = this.isWorkerToolPendingSync(w, 'hbcli');
      const hbMiss = !d.hb_version || d.hb_version === 'CHYBÍ' || d.hb_version === 'MISSING';
      const hbSync = Boolean(hbC.md5 && d.hb_md5 && hbC.md5 === d.hb_md5);
      items.push({
        key: 'hbcli',
        label: hbMiss ? (bld ? '⚙️ HBCLI: Build...' : '! HBCLI: MISSING') : (hbPend ? 'HBCLI: Sync...' : `HBCLI: v${String(d.hb_version).replace(/^v/, '')}${hbSync ? ' ✓' : ''}`),
        cls: hbMiss ? (bld ? 'bg-sky-950 border-sky-400 text-sky-200 hb-sync-pulse' : 'bg-rose-950 border-rose-500 text-rose-200 hb-sync-pulse') : (hbPend ? 'bg-sky-950 border-sky-500 text-sky-200 hb-sync-pulse' : 'bg-emerald-950 border-emerald-800 text-emerald-300'),
        tooltip: `HandBrakeCLI ${hbMiss ? 'MISSING' : ('v' + String(d.hb_version).replace(/^v/, ''))} | x265 ${d.x265_version || '4.1'}\nMD5: ${d.hb_md5 || 'N/A'}`
      });

      const ffC = bins.ffmpeg || {};
      const ffPend = this.isWorkerToolPendingSync(w, 'ffmpeg') || this.isWorkerToolPendingSync(w, 'ffprobe');
      const ffMiss = !d.ffmpeg_ok || !d.ffprobe_ok;
      const ffSync = Boolean(ffC.md5 && d.ffmpeg_md5 && ffC.md5 === d.ffmpeg_md5);
      const ffVer = String(ffC.version || d.ffmpeg_version || 'OK').replace(/^v/, '');
      items.push({
        key: 'ffmpeg',
        label: ffMiss ? (bld ? '⚙️ FFmpeg: Build...' : '! FFmpeg: MISSING') : (ffPend ? 'FFmpeg: Sync...' : `FFmpeg: v${ffVer}${ffSync ? ' ✓' : ''}`),
        cls: ffMiss ? (bld ? 'bg-sky-950 border-sky-400 text-sky-200 hb-sync-pulse' : 'bg-rose-950 border-rose-500 text-rose-200 hb-sync-pulse') : (ffPend ? 'bg-sky-950 border-sky-500 text-sky-200 hb-sync-pulse' : 'bg-emerald-950 border-emerald-800 text-emerald-300'),
        tooltip: `FFmpeg + FFprobe (v${ffVer})\nMD5: ${d.ffmpeg_md5 || 'N/A'}`
      });

      const mkvC = bins.mkvmerge || {};
      const mkvPend = this.isWorkerToolPendingSync(w, 'mkvmerge') || this.isWorkerToolPendingSync(w, 'mkvpropedit');
      const mkvMiss = !d.mkvmerge_ok || !d.mkvpropedit_ok;
      const mkvSync = Boolean(mkvC.md5 && d.mkvmerge_md5 && mkvC.md5 === d.mkvmerge_md5);
      const mkvVer = String(d.mkv_version || mkvC.version || 'OK').replace(/^v/, '');
      items.push({
        key: 'mkv',
        label: mkvMiss ? (bld ? '⚙️ MKVToolNix: Build...' : '! MKVToolNix: MISSING') : (mkvPend ? 'MKVToolNix: Sync...' : `MKVToolNix: v${mkvVer}${mkvSync ? ' ✓' : ''}`),
        cls: mkvMiss ? (bld ? 'bg-sky-950 border-sky-400 text-sky-200 hb-sync-pulse' : 'bg-rose-950 border-rose-500 text-rose-200 hb-sync-pulse') : (mkvPend ? 'bg-sky-950 border-sky-500 text-sky-200 hb-sync-pulse' : 'bg-emerald-950 border-emerald-800 text-emerald-300'),
        tooltip: `MKVToolNix v${mkvVer}\nMD5: ${d.mkvmerge_md5 || 'N/A'}`
      });

      const szC = bins['7z'] || {};
      const szPend = this.isWorkerToolPendingSync(w, '7z');
      const szMiss = d.sevenzip_ok === false;
      const szSync = Boolean(szC.md5 && d.sevenzip_md5 && szC.md5 === d.sevenzip_md5);
      const szVer = String((d.sevenzip_version && d.sevenzip_version !== 'OK') ? d.sevenzip_version : (szC.version || '26.06')).replace(/^v/, '');
      items.push({
        key: '7z',
        label: szMiss ? (bld ? '⚙️ 7-Zip: Build...' : '! 7-Zip: MISSING') : (szPend ? '7-Zip: Sync...' : `7-Zip: v${szVer}${szSync ? ' ✓' : ''}`),
        cls: szMiss ? (bld ? 'bg-sky-950 border-sky-400 text-sky-200 hb-sync-pulse' : 'bg-rose-950 border-rose-500 text-rose-200 hb-sync-pulse') : (szPend ? 'bg-sky-950 border-sky-500 text-sky-200 hb-sync-pulse' : 'bg-emerald-950 border-emerald-800 text-emerald-300'),
        tooltip: `7-Zip v${szVer} (.ISO UDF support)\nMD5: ${d.sevenzip_md5 || 'N/A'}`
      });

      return items;
    },
    getUpstreamVerForTool(toolKey) {
      const up = this.state.binary_builder?.upstream_versions || {};
      if (toolKey === 'hbcli') return String(up.hb_latest || '').replace(/^v/, '');
      if (toolKey === 'mkvmerge' || toolKey === 'mkvpropedit') return String(up.mkvtoolnix_latest || '').replace(/^v/, '');
      if (toolKey === 'ffmpeg' || toolKey === 'ffprobe') return String(up.ffmpeg_latest || '').replace(/^v/, '');
      if (toolKey === '7z') return String(up.sevenzip_latest || '').replace(/^v/, '');
      return '';
    },
    getBinaryBarBadge(osKey, toolKey) {
      const c = this.state.binaries?.[osKey]?.[toolKey];
      const pend = this.onlineWorkers.filter(w => this.getWorkerOsKey(w) === osKey && this.isWorkerToolPendingSync(w, toolKey));
      if ((c && c.updating) || (!c?.available && this.isBuildRunning)) {
        return { cls: 'bg-sky-950 border-sky-400 text-sky-200 font-bold hb-sync-pulse', text: '⚙️ ' + this.t('set_tool_preparing'), tooltip: 'SYSTEM_BUILD in progress.' };
      }
      if (c && c.available) {
        const mb = (c.size / 1048576).toFixed(1) + ' MB';
        const curVer = c.version ? String(c.version).replace(/^v/, '') : '';
        let upVer = this.getUpstreamVerForTool(toolKey);
        if ((toolKey === 'ffmpeg' || toolKey === 'ffprobe') && curVer.startsWith('N-') && /^\d{4}-\d{2}-\d{2}$/.test(upVer)) {
          upVer = curVer;
        }
        const hasUpd = Boolean(upVer && upVer !== 'latest' && curVer && curVer !== upVer);
        const v = curVer ? `v${curVer} ` : '';
        if (pend.length > 0) {
          return {
            cls: 'bg-sky-950 border-sky-500 text-sky-200 hb-sync-pulse',
            text: `${v}(${mb}) -> Sync (${pend.length})`,
            tooltip: `Waiting sync to: ${pend.map(x => x.worker_name).join(', ')}`
          };
        }
        if (hasUpd) {
          return {
            cls: 'bg-amber-950/90 border-amber-400 text-amber-200 font-bold hb-sync-pulse hover:bg-amber-900',
            text: `v${curVer} → v${upVer} ↑ (${mb})`,
            tooltip: `${this.t('set_tool_click_hint')}\nFile: ${c.filename}\nMD5: ${c.md5}`
          };
        }
        return {
          cls: 'bg-emerald-950 border-emerald-700 text-emerald-300 hover:border-emerald-500',
          text: `${v || 'OK '}(${mb})`,
          tooltip: `File: ${c.filename}\nMD5: ${c.md5}`
        };
      }
      return { cls: 'bg-rose-950 border-rose-600 text-rose-200 font-bold hb-sync-pulse', text: '! MISSING', tooltip: `Missing in /data/binaries/${osKey}/` };
    },
    async checkUpstreamVersions() {
      const r = await this.apiFetch('/api/v2/binaries/check_upstream', { method: 'POST' });
      if (r && r.ok) { this.toast(this.t('toast_upstream_checked')); this.fetchState(true); }
      else { this.toast(this.t('toast_action_failed'), 'error'); }
    },
    async handleMainBuildBtn() {
      if (this.isBuildRunning) return;
      if (this.hasOutdatedOrMissingTools) return this.startOutdatedBinaryBuild();
      return this.checkUpstreamVersions();
    },
    async triggerForceReinstallAll() {
      if (this.isBuildRunning) return;
      if (this.requireTwoClickConfirm && !this.confirmingForceReinstall) {
        this.confirmingForceReinstall = true;
        if (this._forceReinstallTimer) clearTimeout(this._forceReinstallTimer);
        this._forceReinstallTimer = setTimeout(() => { this.confirmingForceReinstall = false; }, 4500);
        return;
      }
      this.confirmingForceReinstall = false;
      if (this._forceReinstallTimer) { clearTimeout(this._forceReinstallTimer); this._forceReinstallTimer = null; }
      return this.startBinaryBuild(['all'], true, true);
    },
    async startOutdatedBinaryBuild() {
      const tgts = this.outdatedOrMissingToolTargets;
      if (!tgts.length) {
        this.toast(this.t('set_build_uptodate'));
        return;
      }
      return this.startBinaryBuild(tgts, false, false);
    },
    async startBinaryBuild(targets, forceRebuild = false, forceAll = false) {
      const targetList = Array.isArray(targets) && targets.length > 0 ? targets : ['all'];
      const r = await this.apiFetch('/api/v2/binaries/start_build', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          targets: targetList,
          target_tools: targetList,
          force_rebuild: Boolean(forceRebuild || forceAll),
          force_all: Boolean(forceAll),
          preferred_worker: this.state.settings?.preferred_builder_worker || 'AUTO'
        })
      });
      if (r.ok) {
        const d = await r.json();
        if (d.skipped) {
          this.toast(d.detail || this.t('set_build_uptodate'));
          this.fetchState(true);
          return;
        }
        this.toast(this.t('toast_build_queued_fmt', d.assigned_worker));
        this.switchTab('queue'); this.fetchState(true);
      } else {
        const err = await r.json().catch(() => ({}));
        this.toast(err.detail || 'Cannot start build!', 'error');
      }
    },
    workerStorageDirectOk(w) {
      const d = w?.telemetry?.diag;
      if (d) {
        const inOk = d.inputs_ok !== undefined ? d.inputs_ok : d.nas_dl_ok;
        const outOk = d.outputs_ok !== undefined ? d.outputs_ok : d.nas_out_ok;
        return Boolean(inOk && outOk);
      }
      if (w?.handshake?.verified_at) {
        return Boolean(w.handshake.inputs_ok && w.handshake.outputs_ok);
      }
      return true;
    },
    hasStorageIssue(w) {
      return !this.workerStorageDirectOk(w);
    },
    needsWorkerUpdate(w) {
      const t = this.state.worker_target_version;
      return Boolean(t && w.version && w.version !== '?' && w.version !== t);
    },
    workerUpdateBadgeText(w) {
      return `v${w.version} → v${this.state.worker_target_version} ${this.workerUpdateBlockedUntil(w) ? '⏸' : '↑'}`;
    },
    workerUpdateBadgeTitle(w) {
      const tv = this.state.worker_target_version;
      const until = this.workerUpdateBlockedUntil(w);
      if (until) {
        const o = this.workerOta(w);
        return this.t('wrk_update_blocked_tip', o.target || tv, this.workerUpdateReasonText(o.reason) + (o.detail ? ` – ${o.detail}` : ''), this.formatEpoch(until));
      }
      return (w.active_jobs_count > 0) ? this.t('wrk_update_after_job_tip', tv) : this.t('wrk_update_pending_tip', tv);
    },
    // O4: stav aktualizace hlášený workerem (telemetrie diag.ota) – kanál a čekání po neúspěchu
    workerOta(w) {
      const o = w?.telemetry?.diag?.ota;
      return (o && typeof o === 'object') ? o : {};
    },
    workerChannelMismatch(w) {
      const wch = this.workerOta(w).channel;
      const mch = this.state.ghcr_images?.channel || this.state.github_updater?.channel;
      return Boolean(w?.is_windows === false && wch && mch && wch !== mch);
    },
    workerUpdateBlockedUntil(w) {
      const until = Number(this.workerOta(w).blocked_until || 0);
      return (until * 1000 > Date.now()) ? until : 0;
    },
    workerUpdateReasonText(r) {
      const map = {
        rollback: 'wrk_upd_r_rollback', selftest_failed: 'wrk_upd_r_selftest', image_unchanged: 'wrk_upd_r_unchanged',
        helper_failed: 'wrk_upd_r_helper', pull_failed: 'wrk_upd_r_pull', download: 'wrk_upd_r_download',
        integrity: 'wrk_upd_r_integrity', no_docker_sock: 'wrk_upd_r_no_sock'
      };
      return map[r] ? this.t(map[r]) : (r || this.t('wrk_upd_r_error'));
    },
    managerUpdateFailure() {
      const f = this.state.github_updater?.failure;
      return (f && f.until && f.until * 1000 > Date.now()) ? f : null;
    },
    managerUpdateFailureText() {
      const f = this.managerUpdateFailure();
      if (!f) return '';
      const why = f.kind === 'rollback' ? (f.reason || '?') : (this.workerUpdateReasonText(f.kind) + (f.reason ? ` – ${f.reason}` : ''));
      return this.t(f.kind === 'rollback' ? 'set_ota_failure_rollback' : 'set_ota_failure_line', f.to || '?', why, this.formatEpoch(f.until));
    },
    async triggerWorkerUpdateNow(w) {
      const name = w.worker_name;
      if (this.requireTwoClickConfirm && !this.confirmingWorkerUpdate[name]) {
        this.confirmingWorkerUpdate[name] = true;
        setTimeout(() => { delete this.confirmingWorkerUpdate[name]; }, 4000);
        return;
      }
      delete this.confirmingWorkerUpdate[name];
      const r = await this.apiFetch(`/api/v2/workers/${encodeURIComponent(name)}/update_now`, { method: 'POST' });
      if (r.ok) {
        const d = await r.json().catch(() => ({}));
        this.toast(this.t(d.queued ? 'toast_update_now_sent' : 'toast_update_now_current', name), d.queued ? 'success' : 'warn');
      } else {
        this.toast(this.t('toast_update_failed'), 'error');
      }
    },
    workersBubbleSignature() {
      return this.headerBubbleUniformWidthPx + ':' + this.onlineWorkers.map(w => [
        w.worker_name, this.needsWorkerUpdate(w) ? 1 : 0, w.held_disk_jobs_count || 0,
        this.hasStorageIssue(w) ? 1 : 0, (w.is_enabled && !this.state.maintenance_mode) ? 1 : 0,
        w.active_jobs_count || 0, this.workerMissingTools(w).length, this.workerPendingBinaries(w).length
      ].join('|')).join(';');
    },
    // Jediné místo, které určuje stavovou barvu workeru (čipy v hlavičce, Monitoring, Workery).
    // Modrá = v klidu, zelená = pracuje, oranžová = chce pozornost, červená = chyba / offline, fialová pulzace = aktualizace.
    workerStatusInfo(w) {
      const missing = this.workerMissingTools(w);
      const offline = !w?.is_online;
      const err = offline || (missing.length > 0 && !this.isBuildRunning);
      const heldDisk = (w?.held_disk_jobs_count || 0) > 0;
      const premuxNotReady = Boolean(this.state?.settings?.smart_premux) && !this.isWorkerPremuxReady(w) && !this.isBuildRunning;
      const intakeOff = !w?.is_enabled || Boolean(this.state?.maintenance_mode);
      const warn = intakeOff || heldDisk || premuxNotReady;
      const enc = (w?.active_jobs_count || 0) > 0;
      const needsUpd = this.needsWorkerUpdate(w);
      const updSuffix = needsUpd ? ' · ' + this.t('st_reason_update') : '';
      // Windows worker, který smí nechat PC usnout a je offline: spíš spí, než že by byla chyba
      const ws = w?.windows_settings || {};
      if (offline && w?.is_windows !== false && ws.reported && ws.sleep_prevention_mode && ws.sleep_prevention_mode !== 'always') {
        return { boxCls: 'bg-slate-900/90 border-indigo-800', dotCls: 'bg-indigo-400', lineCls: 'text-indigo-200', updateGlowCls: '',
                 cardCls: 'border-indigo-800/80', stripeCls: 'border-l-4 border-l-indigo-500', reason: this.t('st_reason_sleeping') + updSuffix, sleeping: true };
      }
      if (err) {
        const reason = offline ? this.t('st_reason_offline') : this.t('st_reason_missing_tools', missing.join(', '));
        return { boxCls: 'bg-rose-950/90 border-rose-700', dotCls: 'bg-rose-400 hb-sync-pulse', lineCls: 'text-rose-200', updateGlowCls: '',
                 cardCls: 'border-rose-700/80', stripeCls: 'border-l-4 border-l-rose-500', reason: reason + updSuffix };
      }
      if (warn) {
        const reason = heldDisk ? this.t('st_reason_held_disk') : (intakeOff ? this.t('st_reason_intake_off') : this.t('st_reason_premux'));
        return { boxCls: 'bg-amber-900/90 border-amber-700', dotCls: 'bg-amber-400 hb-sync-pulse', lineCls: 'text-amber-200', updateGlowCls: needsUpd ? 'hb-bubble-upd-amber' : '',
                 cardCls: 'border-amber-700/80', stripeCls: 'border-l-4 border-l-amber-400', reason: reason + updSuffix };
      }
      if (enc) {
        return { boxCls: 'bg-emerald-900/90 border-emerald-700', dotCls: 'bg-emerald-400', lineCls: 'text-emerald-200', updateGlowCls: needsUpd ? 'hb-bubble-upd-green' : '',
                 cardCls: 'border-emerald-700/80', stripeCls: 'border-l-4 border-l-emerald-500', reason: this.t('st_reason_working', w.active_jobs_count) + updSuffix };
      }
      return { boxCls: 'bg-sky-950/90 border-sky-700/80', dotCls: 'bg-sky-400', lineCls: 'text-sky-200', updateGlowCls: needsUpd ? 'hb-bubble-upd-blue' : '',
               cardCls: 'border-sky-800/80', stripeCls: 'border-l-4 border-l-sky-500', reason: this.t('st_reason_idle') + updSuffix };
    },
    // Barva systémové RAM: neutrální, oranžová nad 90 %, červená nad 97 %.
    ramLevel(t) {
      const tot = Number(t?.ram_total_gb || 0);
      const p = tot > 0 ? Number(t?.ram_used_gb || 0) / tot : 0;
      return p > 0.97 ? 'crit' : (p > 0.90 ? 'warn' : 'ok');
    },
    ramLevelBarCls(t, okCls = 'bg-slate-300') {
      const l = this.ramLevel(t);
      return l === 'crit' ? 'bg-rose-500' : (l === 'warn' ? 'bg-amber-400' : okCls);
    },
    ramLevelTextCls(t) {
      const l = this.ramLevel(t);
      return l === 'crit' ? 'text-rose-400' : (l === 'warn' ? 'text-amber-400' : 'text-slate-300');
    },
    showWorkerBubbleTip(ev, w) {
      const el = ev?.currentTarget;
      if (!el || !w) return;
      const rect = el.getBoundingClientRect();
      const t = w.telemetry || {};
      const d = t.diag || {};
      const needsUpd = this.needsWorkerUpdate(w);
      const pendingTools = this.workerPendingToolNames(w);
      const syncingTools = pendingTools.length > 0;
      const tipW = 310;
      const tipH = 98 + (needsUpd ? 44 : 0) + (syncingTools ? 40 : 0);
      let left = rect.left + (rect.width / 2) - (tipW / 2);
      left = Math.max(12, Math.min((window.innerWidth || 1280) - tipW - 12, left));
      let top = rect.bottom + 10;
      if (top + tipH > (window.innerHeight || 800) - 12) {
        top = Math.max(12, rect.top - tipH - 10);
      }
      this.workerBubbleTip = {
        visible: true,
        x: Math.round(left),
        y: Math.round(top),
        workerName: w.worker_name,
        version: String(w.version || '?'),
        targetVersion: String(this.state?.worker_target_version || '?'),
        needsUpdate: needsUpd,
        syncingTools,
        pendingTools,
        pendingToolsCount: pendingTools.length,
        activeJobs: Number(w.active_jobs_count || 0),
        usedSlots: Number(w.used_slots || 0),
        maxSlots: Number(w.max_slots || 0),
        cpuLine: this.formatWorkerBubbleCpu(w),
        ramLine: this.formatWorkerBubbleRam(w),
        diagLine: `HB: ${(!d.hb_version || d.hb_version === 'CHYBÍ' || d.hb_version === 'MISSING') ? 'MISSING' : ('v' + String(d.hb_version).replace(/^v/, ''))} | MKV: v${d.mkv_version || '?'} | 7z: v${d.sevenzip_version || '?'}`
      };
    },
    hideWorkerBubbleTip() {
      if (this.workerBubbleTip && this.workerBubbleTip.visible) {
        this.workerBubbleTip.visible = false;
      }
    },
    syncPulseAnimations() {
      // Sjednocení fází všech pulzujících prvků (bubliny workerů, tečky, šipky, notifikace),
      // aby dynamicky přimontované elementy vždy blikaly přesně ve stejném taktu.
      try {
        if (typeof document === 'undefined' || typeof document.getAnimations !== 'function') return;
        for (const anim of document.getAnimations()) {
          const name = anim && anim.animationName ? String(anim.animationName) : '';
          if (name === 'hbSyncPulse' || name.startsWith('hbPulseUpd') || name === 'hbNotifPulse') {
            if (anim.startTime === null || Math.abs(anim.startTime) > 1) {
              anim.startTime = 0;
            }
          }
        }
      } catch (e) {}
    },
    checkHeaderWorkersOverflow() {
      this.recalcHeaderWorkersFit();
    },
    recalcHeaderWorkersFit() {
      // Deterministický výpočet místa v 1. řádku hlavičky (mezi logem vlevo a akčními tlačítky vpravo):
      // používáme klidovou šířku pravých tlačítek + pevnou rezervu (185 px) pro hover rozbalení,
      // aby najetí myší na Ko-fi / Pauzu / Příjem úloh nikdy neposunulo bubliny workerů mimo hlavičku.
      this.windowWidth = window.innerWidth;
      if (this.workersPlacementMode !== 'auto') return;
      const n = this.onlineWorkers.length;
      if (n === 0) { this.headerOverflowing = false; return; }
      if (this.windowWidth < 1024) { this.headerOverflowing = true; return; }
      const rEl = this.$refs.headerTopRow;
      const lEl = this.$refs.headerLogoBox;
      const rightEl = this.$refs.headerRightActions;
      if (!rEl || !lEl) return;
      const rowGap = 8;
      let rightCollapsedW = rightEl ? rightEl.offsetWidth : 0;
      if (rightEl) {
        rightEl.querySelectorAll('.bulk-stop-expand:not(.is-confirm)').forEach(sp => {
          if (sp.offsetWidth > 0) rightCollapsedW = Math.max(0, rightCollapsedW - sp.offsetWidth - 8);
        });
      }
      const rightExpandReserve = 175; // popisek kafe/pivo je ve skutečnosti užší než 175 px; se 185 se 4 workery nevešly ani na velkém monitoru (chyběl 1 px)
      const avail = rEl.clientWidth - lEl.offsetWidth - rightCollapsedW - rightExpandReserve - rowGap * 2 - 8;
      const sig = this.workersBubbleSignature();
      const bm = this.bubblesMeasure;
      const uniformW = this.headerBubbleUniformWidthPx;
      const deterministicW = n * uniformW + 6 * Math.max(0, n - 1) + 8;
      const bEl = this.$refs.headerBubbles;
      if (bEl && bEl.children && bEl.children.length === n) {
        let measured = 0;
        for (const ch of bEl.children) measured += Math.max(uniformW, ch.offsetWidth);
        measured += 6 * Math.max(0, n - 1) + 8;
        if (measured > 0) { bm.sig = sig; bm.needW = measured; }
      }
      const needW = (bm.sig === sig && bm.needW) ? bm.needW : deterministicW;
      bm.avail = avail;
      const overflow = needW > avail;
      if (overflow !== this.headerOverflowing) {
        this.headerOverflowing = overflow;
        if (!overflow) this.$nextTick(() => this.recalcHeaderWorkersFit());
      }
    },
    // ---- Nastavení webového rozhraní: ukládají se na Manager (stejná ve všech prohlížečích a na všech PC).
    // localStorage slouží jen jako rychlá mezipaměť pro první vykreslení před načtením stavu z Manageru.
    saveUiPref(key, value) {
      this._uiPrefTs = this._uiPrefTs || {};
      this._uiPrefTs[key] = Date.now();
      this.cacheUiPref(key, value);
      this.apiFetch('/api/v2/ui_prefs', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ prefs: { [key]: value } }) }).catch(() => {});
    },
    cacheUiPref(key, value) {
      const legacy = { workers_placement: 'hb_workers_placement', sidebar_side: 'hb_sidebar_side', card_dest: 'hb_card_dest', card_preset: 'hb_card_preset',
        card_tech: 'hb_card_tech', enable_pagination: 'hb_enable_pagination', show_eta: 'hb_show_eta', two_click: 'hb_two_click', changelog_seen: 'hb_changelog_seen' };
      try {
        if (legacy[key]) localStorage.setItem(legacy[key], String(value));
        else localStorage.setItem('hb_pref_' + key, JSON.stringify(value));
      } catch (e) {}
    },
    applyUiPrefs(p) {
      if (!p || typeof p !== 'object') return;
      const ts = this._uiPrefTs || {};
      const take = (k) => (k in p) && !(ts[k] && Date.now() - ts[k] < 8000);  // vlastní čerstvou změnu nepřepisovat starší odpovědí
      Object.keys(p).forEach(k => { if (take(k)) this.cacheUiPref(k, p[k]); });
      if (take('workers_placement') && this.workersPlacementMode !== p.workers_placement) {
        this.workersPlacementMode = p.workers_placement || 'auto';
        this.$nextTick(() => this.checkHeaderWorkersOverflow());
      }
      if (take('sidebar_side')) this.sidebarSide = p.sidebar_side === 'left' ? 'left' : 'right';
      if (take('logo_motion')) this.logoMotion = ['on', 'events', 'off'].includes(p.logo_motion) ? p.logo_motion : 'on';
      if (take('card_dest')) this.cardShowDestPath = Boolean(p.card_dest);
      if (take('card_preset')) this.cardShowPreset = Boolean(p.card_preset);
      if (take('card_tech')) this.cardShowTechBadges = Boolean(p.card_tech);
      if (take('queue_per_page') && Number(p.queue_per_page) > 0) {
        this.queuePerPagePref = Number(p.queue_per_page);
        if (this.enablePagination && this.perPage !== this.queuePerPagePref) { this.perPage = this.queuePerPagePref; this.queuePage = 1; }
      }
      if (take('history_per_page') && Number(p.history_per_page) > 0 && this.historyPerPage !== Number(p.history_per_page)) {
        this.historyPerPage = Number(p.history_per_page);
        this.historyPage = 1;
      }
      if (take('enable_pagination') && this.enablePagination !== Boolean(p.enable_pagination)) {
        this.enablePagination = Boolean(p.enable_pagination);
        this.perPage = this.enablePagination ? (this.queuePerPagePref || 50) : 10000;
        this.queuePage = 1;
      }
      if (take('show_eta')) this.showQueueEta = Boolean(p.show_eta);
      if (take('two_click')) this.requireTwoClickConfirm = Boolean(p.two_click);
      if (take('changelog_seen')) this.changelogSeenVersion = String(p.changelog_seen || '');
      if (take('log_sources') && Array.isArray(p.log_sources)) this.logSources = p.log_sources.slice();
      if (take('tips_seen') && Array.isArray(p.tips_seen)) this.tipsSeen = p.tips_seen.slice();
    },
    saveWorkersPlacementMode() { this.saveUiPref('workers_placement', this.workersPlacementMode); this.$nextTick(() => this.checkHeaderWorkersOverflow()); },
    setSidebarSide(s) { this.sidebarSide = s; this.saveUiPref('sidebar_side', s); },
    saveCardDisplaySettings() {
      this.saveUiPref('card_dest', this.cardShowDestPath);
      this.saveUiPref('card_preset', this.cardShowPreset);
      this.saveUiPref('card_tech', this.cardShowTechBadges);
    },
    extractJobWorkerVersion(j) { return (j && j.worker_version) ? j.worker_version : ''; },
    formatBitrate(kbps) {
      const v = Number(kbps || 0);
      if (v <= 0) return '-';
      return v >= 1000 ? (v / 1000.0).toFixed(1) + ' Mb/s' : Math.round(v) + ' kb/s';
    },
    togglePaginationSetting() { this.saveUiPref('enable_pagination', this.enablePagination); this.perPage = this.enablePagination ? (this.queuePerPagePref || 50) : 10000; this.queuePage = 1; },
    saveQueueEtaSetting() { this.saveUiPref('show_eta', this.showQueueEta); },
    saveClickConfirmSetting() { this.saveUiPref('two_click', this.requireTwoClickConfirm); },
    toggleSmartPremux() {
      if (!this.anyWorkerCanPremux) { this.toast(this.t('toast_premux_req'), 'warn'); return; }
      this.state.settings.smart_premux = !this.state.settings.smart_premux;
      this.saveGlobalSettings();
    },
    async bulkUpdateWorkers(payloadObj, msgText) {
      this.pausePollingUntil = Date.now() + 1200;
      if (Array.isArray(this.state.workers)) {
        this.state.workers.forEach(w => { Object.keys(payloadObj).forEach(k => { w[k] = payloadObj[k]; }); });
      }
      const r = await this.apiFetch('/api/v2/workers/bulk_policy', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payloadObj) });
      if (r.ok) {
        const d = await r.json();
        if (d.smart_premux !== undefined && this.state.settings) this.state.settings.smart_premux = d.smart_premux;
        this.toast(msgText || this.t('toast_bulk_saved'));
        if (d.slots_4k_fixed > 0) this.toast(this.t('toast_bulk_slots_4k', d.slots_4k_fixed), 'warn');
      }
      this.pausePollingUntil = 0; this.fetchState(true);
    },
    async resetClusterAnalytics() {
      if (!this.confirmingResetStats) { this.confirmingResetStats = true; setTimeout(() => { this.confirmingResetStats = false; }, 4000); return; }
      this.confirmingResetStats = false;
      const r = await this.apiFetch('/api/v2/cluster/action', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ action: 'reset_analytics' }) });
      if (r && r.ok) {
        this.toast(this.t('toast_stats_reset'));
      } else {
        this.toast(this.t('toast_action_failed'), 'error');
      }
      this.fetchState(true);
    },
    handleHeaderNotificationClick(notif) {
      if (!notif) return;
      if (notif.action === 'open_system') this.switchTab('settings', 'system');
      else if (notif.action === 'open_security') this.switchTab('settings', 'security');
      else if (notif.action === 'open_workers_pending') { this.switchTab('workers'); this.secScrollTo('wkPendingCard'); }
      else if (notif.action === 'trigger_update') this.triggerClusterUpdate();
      else if (notif.action === 'open_onboarding') this.openOnboardingWizard(1);
      else if (notif.action === 'open_queue') this.switchTab('queue');
      else if (notif.action === 'open_disk_hold') { this.switchTab('queue'); this.filterStatus = 'WAITING_DISK'; }
      else if (notif.action === 'open_binaries') this.switchTab('settings', 'binaries');
    },
    openOnboardingWizard(step = 1) {
      const s = this.state.settings || {};
      this.ensureDlToken();
      const m = this.addWorkerModal;
      m.managerUrl = typeof window !== 'undefined' ? window.location.origin : 'http://192.168.1.100:8383';
      m.winInputsDir = s.win_default_inputs_dir || '';
      m.winOutputsDir = s.win_default_outputs_dir || '';
      m.smbUsername = s.smb_username || '';
      m.smbPassword = s.smb_password || '';
      m.linInputsDir = s.linux_default_inputs_dir || '/mnt/nas/inputs';
      m.linOutputsDir = s.linux_default_outputs_dir || '/mnt/nas/outputs';
      m.linTempDir = s.linux_default_temp_dir || '/mnt/nvme/hb_temp';
      this.onboardingStep = Number(step) || 1;
      this.showOnboardingModal = true;
    },
    async goToOnboardingStep(step) {
      const nextStep = Math.max(1, Math.min(4, Number(step) || 1));
      if (!this.addWorkerModal.managerUrl && typeof window !== 'undefined') {
        this.addWorkerModal.managerUrl = window.location.origin;
      }
      if (this.onboardingStep === 1 && nextStep !== 1) {
        const s = this.state.settings || {};
        this.addWorkerModal.winInputsDir = s.win_default_inputs_dir || '';
        this.addWorkerModal.winOutputsDir = s.win_default_outputs_dir || '';
        this.addWorkerModal.smbUsername = s.smb_username || '';
        this.addWorkerModal.smbPassword = s.smb_password || '';
        this.addWorkerModal.linInputsDir = s.linux_default_inputs_dir || '/mnt/nas/inputs';
        this.addWorkerModal.linOutputsDir = s.linux_default_outputs_dir || '/mnt/nas/outputs';
        this.saveGlobalSettings(false);
      } else if (this.onboardingStep === 3 && nextStep !== 3) {
        this.saveGlobalSettings(false);
      }
      this.onboardingStep = nextStep;
    },
    async skipOrFinishOnboarding() {
      this.showOnboardingModal = false;
      if (!this.state.settings) this.state.settings = {};
      this.state.settings.onboarding_done = true;
      await this.saveGlobalSettings(false);
    },
    cancelDeleteWorkerStatsConfirm() {
      this.confirmingDeleteWorkerStats = '';
      if (this._delStatsTimer) {
        clearTimeout(this._delStatsTimer);
        this._delStatsTimer = null;
      }
    },
    requestDeleteSelectedWorkerStats() {
      const list = this.statsWorkersList;
      if (!list.length) return;
      const target = list.find(x => x.worker_name === this.selectedStatsWorker) || list[0];
      if (!target) return;
      this.selectedStatsWorker = target.worker_name;
      if (this.confirmingDeleteWorkerStats !== target.worker_name) {
        this.confirmingDeleteWorkerStats = target.worker_name;
        if (this._delStatsTimer) clearTimeout(this._delStatsTimer);
        this._delStatsTimer = setTimeout(() => {
          if (this.confirmingDeleteWorkerStats === target.worker_name) {
            this.confirmingDeleteWorkerStats = '';
          }
        }, 4000);
        return;
      }
      this.cancelDeleteWorkerStatsConfirm();
      this.executeDeleteWorkerStats(target.worker_name);
    },
    async executeDeleteWorkerStats(workerName) {
      const wName = String(workerName || '').trim();
      if (!wName) return;
      const r = await this.apiFetch(`/api/stats/worker/${encodeURIComponent(wName)}`, { method: 'DELETE' });
      if (r.ok) {
        const d = await r.json().catch(() => ({}));
        if (d.analytics) this.state.analytics = d.analytics;
        const rem = this.statsWorkersList;
        this.selectedStatsWorker = rem.length ? rem[0].worker_name : '';
        this.toast(this.t('toast_wrk_stats_deleted', wName));
        this.fetchState(true);
      } else {
        this.toast(this.t('toast_mgr_unreachable'), 'warn');
      }
    },
    dismissTip(id) {
      if (!id || this.tipsSeen.includes(id)) return;
      this.tipsSeen = this.tipsSeen.concat([id]);
      this.saveUiPref('tips_seen', this.tipsSeen);
    },
    resetTips() {
      this.tipsSeen = [];
      this.saveUiPref('tips_seen', []);
      this.toast(this.t('toast_tips_reset'));
    },
    clearFilterChip(key) {
      if (key === 'status') { this.filterStatus = 'ALL'; this.queuePage = 1; return; }
      const map = { search: ['searchQuery', ''], preset: ['filterPreset', ''], worker: ['filterWorker', ''], version: ['filterVersion', ''],
        res: ['filterRes', ''], premux: ['filterPremux', ''], net: ['filterNetFallback', false], ineffective: ['filterIneffective', false] };
      if (map[key]) { this[map[key][0]] = map[key][1]; this.resetFilterPages(); }
    },
    // Nabídka ⋯: duplicity hned, mazání historie vždy až na druhé kliknutí (nevratné)
    runMenuAction(act) {
      if (act === 'dups') { this.moreActionsOpen = false; this.openDuplicatesModal(); return; }
      if (this.confirmingMenuAction !== act) {
        this.confirmingMenuAction = act;
        clearTimeout(this._menuConfirmTimer);
        this._menuConfirmTimer = setTimeout(() => { this.confirmingMenuAction = ''; }, 4000);
        return;
      }
      this.confirmingMenuAction = '';
      this.moreActionsOpen = false;
      this.clusterAction(act);
    },
    closeMoreActions() { this.moreActionsOpen = false; this.confirmingMenuAction = ''; },
    onQueuePerPageChange() { this.queuePage = 1; this.queuePerPagePref = this.perPage; this.saveUiPref('queue_per_page', this.perPage); },
    onHistoryPerPageChange() { this.historyPage = 1; this.saveUiPref('history_per_page', this.historyPerPage); },
    toggleStatusFilter(st) { this.filterStatus = (this.filterStatus === st) ? 'ALL' : st; this.queuePage = 1; },
    toggleResFilter(r) { this.filterRes = (this.filterRes === r) ? '' : r; this.resetFilterPages(); },
    togglePremuxFilter(val) { this.filterPremux = (this.filterPremux === val) ? '' : val; this.resetFilterPages(); },
    focusWorkerCard(workerName) {
      if (!workerName) return;
      this.hideWorkerBubbleTip();
      if (this._highlightWorkerTimer) clearTimeout(this._highlightWorkerTimer);
      this.highlightedWorker = workerName;
      this.switchTab('workers');
      this.$nextTick(() => {
        const cards = Array.from(document.querySelectorAll('[data-worker-card]'));
        const el = cards.find(c => c.dataset.workerCard === workerName);
        if (el && typeof el.scrollIntoView === 'function') {
          el.scrollIntoView({ behavior: 'smooth', block: 'center' });
        }
      });
      this._highlightWorkerTimer = setTimeout(() => {
        if (this.highlightedWorker === workerName) this.highlightedWorker = '';
      }, 2600);
    },
    resetAllFilters() {
      this.searchQuery = '';
      this.filterStatus = 'ALL';
      this.filterPreset = '';
      this.filterRes = '';
      this.filterWorker = '';
      this.filterVersion = '';
      this.filterPremux = '';
      this.filterNetFallback = false;
      this.filterIneffective = false;
      this.resetFilterPages();
    },
    openFilteredInHistoryTab() {
      this.switchTab('history', null, true);
    },
    setHistorySortKey(k) {
      if (!k || this.historySortKey === k) return;
      this.historySortKey = k;
      this.historySortDir = (k === 'original_filename' || k === 'worker_name') ? 'asc' : 'desc';
    },
    sortHistoryBy(k) {
      if (this.historySortKey === k) this.historySortDir = (this.historySortDir === 'asc') ? 'desc' : 'asc';
      else { this.historySortKey = k; this.historySortDir = (k === 'original_filename' || k === 'worker_name') ? 'asc' : 'desc'; }
    },
    getTechBadges(j) {
      if (j.video_encoder) {
        const arr = [j.video_encoder];
        if (j.video_rf !== undefined && j.video_rf !== null) arr.push(`RF:${j.video_rf}`);
        if (j.video_preset) arr.push(j.video_preset);
        if (j.audio_count !== undefined) arr.push(`Audio:${j.audio_count}×`);
        if (j.subs_count !== undefined) arr.push(`Sub:${j.subs_count}×`);
        return arr;
      }
      if (j.encoder_summary) return j.encoder_summary.split('|').map(s => s.trim()).filter(Boolean);
      return [];
    },
    toast(msg, type = 'info') {
      const id = Date.now() + Math.random();
      this.toasts.push({ id, msg, type });
      // filter místo splice(findIndex) – splice(-1, 1) by smazal cizí (poslední) toast
      setTimeout(() => { this.toasts = this.toasts.filter(x => x.id !== id); }, type === 'error' ? 6000 : 3500);
    },
    // Vlastní popisky (tooltips) v barvách aplikace místo systémových: atribut title se při najetí myší převede na data-hb-tip
    // (systémový popisek se tak neukáže) a zobrazí se jeden společný panel #hb-tip. Funguje pro všechny existující title
    // i pro ty, které Vue později změní (MutationObserver). Volitelně data-hb-tip-tone = idle | run | warn | err (barva proužku).
    initHbTooltips() {
      if (this._hbTipInit) return;
      this._hbTipInit = true;
      const tip = document.createElement('div');
      tip.id = 'hb-tip';
      tip.setAttribute('role', 'tooltip');
      document.body.appendChild(tip);
      let cur = null; let timer = null;
      const convert = (el) => {
        const t = el.getAttribute && el.getAttribute('title');
        if (!t) return;
        el.setAttribute('data-hb-tip', t);
        el.removeAttribute('title');
        if (!el.hasAttribute('aria-label') && !(el.textContent || '').trim()) el.setAttribute('aria-label', t);
        else if (!el.hasAttribute('aria-description')) el.setAttribute('aria-description', t);
      };
      const place = (el) => {
        const r = el.getBoundingClientRect();
        const tw = tip.offsetWidth; const th = tip.offsetHeight;
        let x = r.left + r.width / 2 - tw / 2;
        x = Math.max(8, Math.min(x, window.innerWidth - tw - 8));
        let y = r.bottom + 8;
        if (y + th > window.innerHeight - 8) y = r.top - th - 8;
        tip.style.left = Math.round(x) + 'px';
        tip.style.top = Math.round(Math.max(8, y)) + 'px';
      };
      const hide = () => { clearTimeout(timer); tip.classList.remove('show'); cur = null; };
      const show = (el) => {
        const txt = el.getAttribute('data-hb-tip') || '';
        if (!txt || !el.isConnected) return;
        tip.textContent = txt;
        tip.className = 'tone-' + (el.getAttribute('data-hb-tip-tone') || 'brand');
        place(el);
        requestAnimationFrame(() => { if (cur === el) tip.classList.add('show'); });
      };
      document.addEventListener('pointerover', (e) => {
        if (e.pointerType && e.pointerType !== 'mouse') return;
        const el = e.target && e.target.closest ? e.target.closest('[title],[data-hb-tip]') : null;
        if (!el) { if (cur) hide(); return; }
        convert(el);
        if (el === cur) return;
        hide();
        if (!el.getAttribute('data-hb-tip')) return;
        cur = el;
        timer = setTimeout(() => { if (cur === el) show(el); }, 350);
      }, true);
      document.addEventListener('pointerout', (e) => {
        if (!cur) return;
        const to = e.relatedTarget;
        if (to && cur.contains(to)) return;
        hide();
      }, true);
      document.addEventListener('pointerdown', hide, true);
      window.addEventListener('scroll', hide, true);
      window.addEventListener('blur', hide);
      try {
        new MutationObserver((muts) => {
          for (const m of muts) {
            const el = m.target;
            if (!el.getAttribute || !el.getAttribute('title')) continue;
            convert(el);
            if (el === cur && tip.classList.contains('show')) show(el);   // text se změnil během zobrazení (např. stav v logu)
          }
        }).observe(document.body, { attributes: true, attributeFilter: ['title'], subtree: true });
      } catch (e) {}
    },
    // Krátká jednorázová animace loga: cycle = rozeslání do rohů, hub = pulz středu, nodes = postupně bliknou rohy,
    // flow = tečky z rohů do středu, pins = postupně se rozsvítí nožičky čipu.
    playLogoFx(kind) {
      if (this.logoMotion === 'off' || !kind) return;
      clearTimeout(this._logoFxTimer);
      this.logoFx = '';
      this.$nextTick(() => {
        requestAnimationFrame(() => {
          this.logoFx = kind;
          this._logoFxTimer = setTimeout(() => { this.logoFx = ''; }, 2400);
        });
      });
    },
    // Najetí myší: střed pulzne a rohy se postupně rozsvítí a ZŮSTANOU svítit – z loga Manageru se stane logo HB Cluster.
    // Po odjetí myší se rohy zase plynule zhasnou.
    logoHover() {
      this.logoHovered = true;
    },
    logoLeave() {
      this.logoHovered = false;
    },
    saveLogoMotion() {
      if (!['on', 'events', 'off'].includes(this.logoMotion)) this.logoMotion = 'on';
      this.saveUiPref('logo_motion', this.logoMotion);
      if (this.logoMotion !== 'off') this.playLogoFx('cycle');
    },
    switchTab(t, settingsSec = null, preserveSearch = false) {
      {
        // krátká animace loga podle záložky: fronta/dokončené/hledání = pulz středu, workery/monitoring = rohy,
        // živé logy = tečky z rohů do středu (logy tečou od workerů k Manageru), nastavení = postupně se rozsvítí nožičky čipu
        const fx = { queue: 'hub', history: 'hub', search: 'hub', workers: 'nodes', monitoring: 'nodes', logs: 'flow', settings: 'pins', changelog: 'pins', import: 'hub' }[t];
        if (fx && this.activeTab !== t) this.playLogoFx(fx);
      }
      if (t === 'changelog') { t = 'settings'; settingsSec = 'changelog'; }
      if (t === 'import') { t = 'queue'; }
      if (t === 'workers' && (this.state.workers || []).length === 0) {
        // žádný worker: „+“ Přidat worker jemně zapulzuje (stejně jako + ve Frontě)
        this.pulseAddWorkerBtn = true;
        clearTimeout(this._pulseAddWorkerTimer);
        this._pulseAddWorkerTimer = setTimeout(() => { this.pulseAddWorkerBtn = false; }, 6000);
      }
      if (t === 'queue' && this.totalQueueCount === 0) {
        // prázdná fronta: tlačítko + jemně zapulzuje, ať je vidět, kudy přidat úlohy
        this.pulseAddBtn = true;
        clearTimeout(this._pulseAddTimer);
        this._pulseAddTimer = setTimeout(() => { this.pulseAddBtn = false; }, 6000);
      }
      this.closeMoreActions();
      const prevTab = this.activeTab;
      if (prevTab === 'queue' && !['queue', 'search', 'history'].includes(t) && !preserveSearch && !this.hasPanelFilterCriteria) {
        this.queueFiltersExpanded = false;
      }
      this.activeTab = t;
      if (t === 'settings') this.mobileSetList = !settingsSec;   // mobil: bez konkrétní sekce → seznam sekcí
      if (t === 'settings' && settingsSec) this.setSettingsSection(settingsSec);
      if (t === 'search') {
        this.searchInQueue = true;
        this.searchInCompleted = true;
        this.resetFilterPages();
        if (this.historyList.length === 0) {
          this.fetchHistory().then(() => this.$nextTick(() => this.pruneUnavailableFilters()));
        } else {
          this.$nextTick(() => this.pruneUnavailableFilters());
        }
        this.$nextTick(() => this.initSortable());
        this.focusUnifiedSearch();
      }
      if (t === 'history') {
        this.searchInCompleted = true;
        this.searchInQueue = false;
        this.resetFilterPages();
        this.fetchHistory().then(() => this.$nextTick(() => this.pruneUnavailableFilters()));
        this.$nextTick(() => this.pruneUnavailableFilters());
      }
      if (t === 'changelog') this.fetchChangelog();
      if (t === 'logs') this.fetchLogs();
      if (t === 'queue') {
        this.searchInQueue = true;
        this.searchInCompleted = false;
        if (prevTab !== 'queue') {
          if (preserveSearch || this.hasPanelFilterCriteria) {
            this.queueFiltersExpanded = true;
          } else if (prevTab === 'search' || prevTab === 'history') {
            this.queueFiltersExpanded = false;
          }
        }
        this.$nextTick(() => {
          this.pruneUnavailableFilters();
          this.initSortable();
        });
      }
      this.$nextTick(() => this.syncPulseAnimations());
      this.pushNavHistory();
    },
    buildNavHash(tab, section, mobileSetList) {
      const t = tab || 'queue';
      if (t === 'settings') {
        return mobileSetList ? '#settings' : `#settings/${section || 'general'}`;
      }
      return `#${t}`;
    },
    parseNavHash() {
      if (typeof window === 'undefined') return null;
      const raw = String(window.location.hash || '').replace(/^#\/?/, '').trim();
      if (!raw) return null;
      const [head, sub] = raw.split('/');
      const validTabs = ['queue', 'history', 'search', 'workers', 'monitoring', 'logs', 'settings'];
      if (!validTabs.includes(head)) return null;
      if (head === 'settings') {
        if (sub && SETTINGS_SECTIONS.includes(sub)) {
          return { tab: 'settings', section: sub, mobileSetList: false };
        }
        return { tab: 'settings', section: this.state?.settingsSection || 'general', mobileSetList: true };
      }
      return { tab: head, section: this.state?.settingsSection || 'general', mobileSetList: true };
    },
    pushNavHistory(replace = false) {
      if (typeof window === 'undefined' || !window.history || this._applyingPopState) return;
      const st = {
        hbNav: true,
        tab: this.activeTab || 'queue',
        section: this.state?.settingsSection || 'general',
        mobileSetList: Boolean(this.mobileSetList)
      };
      const cur = window.history.state;
      if (!replace && cur && cur.hbNav && !cur.modal && cur.tab === st.tab && cur.section === st.section && Boolean(cur.mobileSetList) === st.mobileSetList) {
        return;
      }
      const hash = this.buildNavHash(st.tab, st.section, st.mobileSetList);
      try {
        window.history[replace ? 'replaceState' : 'pushState'](st, '', hash);
      } catch (e) {}
    },
    pushModalHistory() {
      if (typeof window === 'undefined' || !window.history || this._applyingPopState) return;
      const cur = window.history.state;
      if (cur && cur.hbNav && cur.modal) return;
      const st = {
        hbNav: true,
        modal: true,
        tab: this.activeTab || 'queue',
        section: this.state?.settingsSection || 'general',
        mobileSetList: Boolean(this.mobileSetList)
      };
      try {
        window.history.pushState(st, '', this.buildNavHash(st.tab, st.section, st.mobileSetList));
      } catch (e) {}
    },
    onPopState(ev) {
      // Gesto / tlačítko Zpět na mobilu i desktopu: nejdřív zavřít otevřené okno/nabídku, pak vrátit předchozí záložku nebo sekci Nastavení.
      if (this.folderBrowser?.open) { this.folderBrowser.open = false; return; }
      if (this.smbModal?.open) { this.smbModal.open = false; this.confirmingClearSmb = false; return; }
      if (this.addWorkerModal?.open) { this.addWorkerModal.open = false; return; }
      if (this.dupModal?.open) { this.dupModal.open = false; return; }
      if (this.inspectModal?.open) { this.inspectModal.open = false; return; }
      if (this.moreActionsOpen) { this.closeMoreActions(); return; }
      if (this.mobileMoreOpen) { this.mobileMoreOpen = false; return; }

      const st = (ev && ev.state && ev.state.hbNav) ? ev.state : this.parseNavHash();
      if (!st) return;
      this._applyingPopState = true;
      try {
        if (st.tab === 'settings') {
          this.switchTab('settings', st.mobileSetList ? null : (st.section || 'general'));
          this.mobileSetList = Boolean(st.mobileSetList);
        } else {
          this.switchTab(st.tab || 'queue');
        }
      } finally {
        this._applyingPopState = false;
      }
    },
    // ---------------------------------------------------------------- ZABEZPEČENÍ (krok 1: přihlášení do webu)
    _secErrMsg(d) {
      const det = d && d.detail;
      if (det === 'password_too_short') return this.t('toast_sec_pw_short');
      if (det === 'bad_password') return this.t('toast_sec_bad_pw');
      if (det === 'too_many_attempts') return this.t('toast_sec_wait', d.retry_after || 60);
      return this.t('toast_sec_failed');
    },
    async securityRefreshSession() {
      const r = await this.apiFetch('/api/auth/status', { cache: 'no-store' });
      if (r.ok) { const d = await r.json().catch(() => ({})); this.secHasSession = !!d.session; }
    },
    securityOpen() {
      if (!this.stateLoaded) return;   // počkat na první fetchState(), aby se secNet/secDevices/secPk nevyplnily prázdnými výchozími hodnotami
      const s = this.secInfo;
      this.secNet = {
        home_bypass: !!s.home_bypass,
        networksText: (s.home_networks || []).join('\n'),
        behind_proxy: !!s.behind_proxy,
        proxiesText: (s.trusted_proxies || []).join('\n'),
        trust_cf_header: !!s.trust_cf_header,
        public_stats: s.public_stats !== false
      };
      this.secForm = { ...this.secForm, new1: '', new2: '', cur: '', disablePw: '' };
      this.securityRefreshSession();
      if (s.enabled) { this.securityLoadDevices(); this.secPasskeysLoad(); }
    },
    _secApply(d) {
      if (d && d.security) this.state.security = { ...(this.state.security || {}), ...d.security };
    },
    async securityEnable() {
      const f = this.secForm;
      if ((f.new1 || '').length < 8) { this.toast(this.t('toast_sec_pw_short'), 'warn'); return; }
      if (f.new1 !== f.new2) { this.toast(this.t('toast_sec_pw_mismatch'), 'warn'); return; }
      f.busy = true;
      const r = await this.apiFetch('/api/v2/security/enable', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ password: f.new1, remember: !!f.remember }) });
      const d = await r.json().catch(() => ({}));
      f.busy = false;
      if (!r.ok) { this.toast(this._secErrMsg(d), 'error'); return; }
      this._secApply(d);
      this.secForm = { ...this.secForm, new1: '', new2: '' };
      this.secHasSession = true;
      this.toast(this.t('toast_sec_enabled'));
      this.securityLoadDevices();
    },
    async securityChangePassword() {
      const f = this.secForm;
      if ((f.new1 || '').length < 8) { this.toast(this.t('toast_sec_pw_short'), 'warn'); return; }
      if (f.new1 !== f.new2) { this.toast(this.t('toast_sec_pw_mismatch'), 'warn'); return; }
      f.busy = true;
      const r = await this.apiFetch('/api/v2/security/password', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ current: f.cur, new: f.new1, logout_others: !!f.logoutOthers }) });
      const d = await r.json().catch(() => ({}));
      f.busy = false;
      if (!r.ok) { this.toast(this._secErrMsg(d), 'error'); return; }
      this._secApply(d);
      this.secForm = { ...this.secForm, cur: '', new1: '', new2: '' };
      this.toast(this.t('toast_sec_pw_changed'));
      this.securityLoadDevices();
    },
    _secWkApply(d) {
      if (d && d.workers) this.state.security = { ...(this.state.security || {}), workers: d.workers };
    },
    secWkStatusLabel(r) {
      if (r.status === 'approved') return '✓ ' + this.t('sec_wk_st_approved');
      if (r.status === 'known') return '✓ ' + this.t('sec_wk_has_key');
      if (r.status === 'rejected') return this.t('sec_wk_st_rejected', this.formatEpoch(r.until));
      if (r.status === 'ignored') return this.t('sec_wk_st_ignored');
      return r.status;
    },
    async secAutoWindow(on) {
      const r = await this.apiFetch('/api/v2/security/workers/auto', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ enabled: !!on }) });
      const d = await r.json().catch(() => ({}));
      if (r.ok) this._secWkApply(d);
    },
    async secWorkerAct(r, action) {
      if ((action === 'reject' || action === 'ignore') && !this.secConfirmed((action === 'reject' ? 'rej_' : 'ign_') + r.id)) return;
      const res = await this.apiFetch(`/api/v2/security/workers/${encodeURIComponent(r.id)}/${action}`, { method: 'POST' });
      const d = await res.json().catch(() => ({}));
      if (!res.ok || !d.ok) { this.toast(this.t('toast_sec_failed'), 'error'); return; }
      this._secWkApply(d);
      this.toast(this.t('toast_sec_wk_' + (d.action || action), r.name));
    },
    async ensureDlToken(force = false) {
      // jednorázový odkaz (1 h) pro stažení Windows balíčku; se zaškrtnutým předschválením se worker schválí sám
      const m = this.addWorkerModal;
      if (!force && m.dlToken && (m.dlExp * 1000 - Date.now()) > 10 * 60000) return;
      const r = await this.apiFetch('/api/v2/security/join_token', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ join: !!m.preapprove }) });
      const d = await r.json().catch(() => ({}));
      if (r.ok && d.token) { m.dlToken = d.token; m.dlExp = d.expires || 0; }
    },
    secScrollTo(refName) {
      this.$nextTick(() => {
        const el = this.$refs[refName];
        if (el && el.scrollIntoView) el.scrollIntoView({ behavior: 'smooth', block: 'start' });
      });
    },
    _b2a(s) { s = String(s || '').replace(/-/g, '+').replace(/_/g, '/'); while (s.length % 4) s += '='; const bin = atob(s); const u = new Uint8Array(bin.length); for (let i = 0; i < bin.length; i++) u[i] = bin.charCodeAt(i); return u.buffer; },
    _a2b(buf) { const u = new Uint8Array(buf); let s = ''; for (let i = 0; i < u.length; i++) s += String.fromCharCode(u[i]); return btoa(s).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, ''); },
    async secPasskeysLoad() {
      const r = await this.apiFetch('/api/v2/security/passkeys', { cache: 'no-store' });
      if (r.ok) { const d = await r.json().catch(() => ({})); this.secPk = { usable: !!d.usable, https: !!d.https, lib: d.lib !== false, rp_id: d.rp_id || '', items: d.items || [] }; }
    },
    async secPasskeyAdd() {
      if (!window.PublicKeyCredential || !window.isSecureContext) { this.toast(this.t('sec_pk_need_https'), 'warn'); return; }
      this.secForm.busy = true;
      try {
        const r = await this.apiFetch('/api/v2/security/passkeys/register/options', { method: 'POST' });
        const o = await r.json().catch(() => ({}));
        if (!r.ok || !o.options) throw new Error('options');
        const pk = o.options;
        pk.challenge = this._b2a(pk.challenge);
        pk.user.id = this._b2a(pk.user.id);
        (pk.excludeCredentials || []).forEach(c => { c.id = this._b2a(c.id); });
        const cr = await navigator.credentials.create({ publicKey: pk });
        const body = { challenge_id: o.challenge_id, name: this.uaLabel(navigator.userAgent), credential: {
          id: cr.id, rawId: this._a2b(cr.rawId), type: cr.type, clientExtensionResults: {},
          response: { clientDataJSON: this._a2b(cr.response.clientDataJSON), attestationObject: this._a2b(cr.response.attestationObject),
                      transports: (cr.response.getTransports ? cr.response.getTransports() : []) } } };
        const v = await this.apiFetch('/api/v2/security/passkeys/register/verify', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
        if (!v.ok) throw new Error('verify');
        this.toast(this.t('toast_sec_pk_added'));
        this.secPasskeysLoad();
      } catch (e) {
        if (e && e.name === 'InvalidStateError') {
          this.toast(this.t('toast_sec_pk_exists'), 'warn');
        } else {
          this.toast(this.t('toast_sec_pk_failed'), 'warn');
        }
      } finally {
        this.secForm.busy = false;
      }
    },
    async secPasskeyRemove(p) {
      if (!this.secConfirmed('pk_' + p.id)) return;
      const r = await this.apiFetch('/api/v2/security/passkeys/' + encodeURIComponent(p.id), { method: 'DELETE' });
      if (r.ok) { this.toast(this.t('toast_sec_pk_removed')); this.secPasskeysLoad(); }
    },
    secToggleProxy() {
      // zapnutí „Používám reverse proxy“: prázdné pole předvyplnit adresou proxy, kterou Manager právě vidí
      this.secNet.behind_proxy = !this.secNet.behind_proxy;
      if (this.secNet.behind_proxy && !String(this.secNet.proxiesText || '').trim() && this.secInfo.proxy_hint && this.secInfo.peer_ip) {
        this.secNet.proxiesText = this.secInfo.peer_ip;
      }
    },
    secConfirmed(key) {
      // 1. klik = „Opravdu …?“ (4 s), 2. klik = provést
      clearTimeout(this._secConfirmTimer);
      if (this.secConfirm === key) { this.secConfirm = ''; return true; }
      this.secConfirm = key;
      this._secConfirmTimer = setTimeout(() => { this.secConfirm = ''; }, 4000);
      return false;
    },
    async securityDisable() {
      const f = this.secForm;
      if (!f.disablePw) { this.toast(this.t('toast_sec_bad_pw'), 'warn'); return; }
      if (!this.secConfirmed('disable')) return;
      f.busy = true;
      const r = await this.apiFetch('/api/v2/security/disable', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ password: f.disablePw }) });
      const d = await r.json().catch(() => ({}));
      f.busy = false;
      if (!r.ok) { this.toast(this._secErrMsg(d), 'error'); return; }
      this._secApply(d);
      this.secForm = { ...this.secForm, disablePw: '' };
      this.secDevices = [];
      this.secHasSession = false;
      this.toast(this.t('toast_sec_disabled'));
    },
    async securitySaveNetwork() {
      const split = (txt) => String(txt || '').split(/[\s,;]+/).map(x => x.trim()).filter(Boolean);
      const n = this.secNet;
      const body = this.secInfo.addresses_masked
        ? { public_stats: !!n.public_stats }
        : {
            home_bypass: !!n.home_bypass, home_networks: split(n.networksText),
            behind_proxy: !!n.behind_proxy, trusted_proxies: split(n.proxiesText),
            trust_cf_header: !!n.trust_cf_header, public_stats: !!n.public_stats
          };
      this.secForm.busy = true;
      const r = await this.apiFetch('/api/v2/security/network', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
      const d = await r.json().catch(() => ({}));
      this.secForm.busy = false;
      if (!r.ok) { this.toast(this._secErrMsg(d), 'error'); return; }
      this._secApply(d);
      this.securityOpen();
      this.toast(this.t('toast_sec_saved'));
    },
    async securityLoadDevices() {
      const r = await this.apiFetch('/api/v2/security/devices', { cache: 'no-store' });
      if (r.ok) { const d = await r.json().catch(() => ({})); this.secDevices = d.devices || []; }
    },
    async securityRemoveDevice(id) {
      if (!this.secConfirmed('dev_' + id)) return;
      const r = await this.apiFetch('/api/v2/security/devices/' + encodeURIComponent(id), { method: 'DELETE' });
      if (r.ok) { this.toast(this.t('toast_sec_device_removed')); this.securityLoadDevices(); }
    },
    async securityLogoutOthers() {
      if (!this.secConfirmed('others')) return;
      const r = await this.apiFetch('/api/v2/security/devices/logout_others', { method: 'POST' });
      if (r.ok) { this.toast(this.t('toast_sec_device_removed')); this.securityLoadDevices(); }
    },
    async logoutWeb() {
      await this.apiFetch('/api/auth/logout', { method: 'POST' });
      window.location.href = '/login';
    },
    uaLabel(ua) {
      const s = String(ua || '');
      const os = /Android/i.test(s) ? 'Android' : /iPhone/i.test(s) ? 'iPhone' : /iPad/i.test(s) ? 'iPad' : /Windows/i.test(s) ? 'Windows'
        : /Mac OS X|Macintosh/i.test(s) ? 'macOS' : /CrOS/i.test(s) ? 'ChromeOS' : /Linux/i.test(s) ? 'Linux' : '';
      const br = /Edg\//.test(s) ? 'Edge' : /OPR\//.test(s) ? 'Opera' : /Firefox\//.test(s) ? 'Firefox' : /SamsungBrowser/.test(s) ? 'Samsung Internet'
        : /Chrome\//.test(s) ? 'Chrome' : /Safari\//.test(s) ? 'Safari' : '';
      return [br, os].filter(Boolean).join(' · ') || this.t('sec_device_unknown');
    },
    formatEpoch(sec) {
      if (!sec) return '-';
      try { return new Date(sec * 1000).toLocaleString(this.uiLang === 'cs' ? 'cs-CZ' : 'en-GB', { day: 'numeric', month: 'numeric', hour: '2-digit', minute: '2-digit' }); } catch (e) { return '-'; }
    },
    mobileSettingsBack() {
      this.mobileSetList = true;
      window.scrollTo({ top: 0 });
      this.pushNavHistory();
    },
    setSettingsSection(sec) {
      // „Systém“ a „Historie změn“ jsou od dev.5 součástí sekce „O aplikaci“ (staré odkazy přesměrovat)
      const toChangelog = (sec === 'changelog');
      if (sec === 'system' || sec === 'changelog') sec = 'about';
      if (!this.state) this.state = {};
      this.state.settingsSection = SETTINGS_SECTIONS.includes(sec) ? sec : 'general';
      if (this.isMobileSettings && this.mobileSetList) {
        this.mobileSetList = false;
        if (!toChangelog) window.scrollTo({ top: 0 });
      }
      if (this.state.settingsSection === 'security') this.securityOpen();
      if (this.state.settingsSection === 'about') {
        this.markChangelogSeen();
        this.fetchChangelog();
        if (toChangelog) {
          this.$nextTick(() => setTimeout(() => {
            const el = this.$refs.aboutChangelog;
            if (el && el.scrollIntoView) el.scrollIntoView({ behavior: 'smooth', block: 'start' });
          }, 60));
        }
      }
      if (this.activeTab === 'settings') this.pushNavHistory();
    },
    async fetchState(force = false) {
      if (!force && (this.isDragging || Date.now() < this.pausePollingUntil)) return;
      // skrytá záložka prohlížeče: stav jen 1× za 30 s (dřív každé 2 s i na pozadí), po návratu hned
      if (!force && document.hidden && Date.now() - (this._lastStateFetchAt || 0) < 30000) return;
      this._lastStateFetchAt = Date.now();
      if (this.isFetchingState) {
        if (force) this.pendingForcedFetch = true;
        return;
      }
      this.isFetchingState = true;
      const fetchSettingsSeq = this._settingsMutationSeq;
      const ctrl = new AbortController();
      const tid = setTimeout(() => ctrl.abort(), 8000);
      try {
        const r = await this.apiFetch('/api/v2/state', { cache: 'no-store', signal: ctrl.signal });
        if (r.ok) {
          const d = await r.json();
          const srvVer = String(d.version || d.manager_version || '').trim();
          if (srvVer) {
            if (!window._hbInitialVersion) {
              window._hbInitialVersion = srvVer;
            } else if (srvVer !== window._hbInitialVersion && !window._hbReloadingForVersion) {
              window._hbReloadingForVersion = true;
              (async () => {
                try {
                  if (window.caches && typeof window.caches.keys === 'function') {
                    const cacheNames = await window.caches.keys();
                    await Promise.all(cacheNames.map(k => window.caches.delete(k)));
                  }
                } catch (e) {}
                try {
                  await fetch(window.location.href, { cache: 'reload', credentials: 'same-origin' });
                } catch (e) {}
                window.location.reload();
              })();
              return;
            }
          }
          if (!d.settings) d.settings = {};
          const curSec = this.state?.settingsSection || 'general';
          d.settingsSection = SETTINGS_SECTIONS.includes(curSec) ? curSec : 'general';  // jediný seznam sekcí (dřív zde zastaralý → skok na Obecné)
          const prevComp = this.state.analytics?.completed_count || 0;
          // Periodický polling NIKDY nepřepisuje rozpracovaná ani právě uložená nastavení
          // (ani při race condition mezi probíhajícím GETem a POST update_settings).
          if ((this.isSettingsEditLocked() || fetchSettingsSeq !== this._settingsMutationSeq) && this.state.settings) {
            d.settings = this.state.settings;
          } else {
            this.settingsSnapshot = JSON.stringify(d.settings);
          }
          // Nevracet rozpracovanou hodnotu max_slots, pokud uživatel právě píše do pole na kartě workeru.
          const activeEl = typeof document !== 'undefined' ? document.activeElement : null;
          if (activeEl && activeEl.tagName === 'INPUT' && typeof activeEl.name === 'string' && activeEl.name.startsWith('slots_') && Array.isArray(d.workers)) {
            const focusedWorker = activeEl.name.slice(6);
            const prevW = (this.state?.workers || []).find(w => w.worker_name === focusedWorker);
            const nextW = d.workers.find(w => w.worker_name === focusedWorker);
            if (prevW && nextW) nextW.max_slots = prevW.max_slots;
          }
          this.state = d;
          this.stateLoaded = true;
          {
            // logo: jedno kolo animace po otevření Manageru a při každém nově spuštěném převodu
            const act = this.totalActiveCount;
            if (this._logoActPrev === undefined || act > this._logoActPrev) this.playLogoFx('cycle');
            this._logoActPrev = act;
          }
          {
            // nový worker čeká na schválení → upozornit (jen při nárůstu, ne při načtení stránky)
            const pend = (((d.security || {}).workers || {}).entries || []).filter(r => r.status === 'pending' && !r.test).length;
            if (this._wkPendPrev !== undefined && pend > this._wkPendPrev) this.toast(this.t('toast_sec_wk_new'), 'warn');
            this._wkPendPrev = pend;
          }
          if (!this._onboardingChecked) {
            this._onboardingChecked = true;
            const isCleanInstall = (
              d.settings?.onboarding_done === false &&
              (d.workers || []).length === 0 &&
              (d.queue || []).length === 0 &&
              (d.analytics?.completed_count || 0) === 0
            );
            if (isCleanInstall) {
              this.openOnboardingWizard(1);
            }
          }
          if (!this.statsWorkersList.some(x => x.worker_name === this.selectedStatsWorker)) {
            this.selectedStatsWorker = this.statsWorkersList[0]?.worker_name || '';
          }
          if (Array.isArray(d.workers)) {
            const nextCapMap = {};
            d.workers.forEach(w => { if (w && w.worker_name) nextCapMap[w.worker_name] = w.capability || 'all'; });
            this.workerCapPrev = nextCapMap;
          }
          const newComp = d.analytics?.completed_count || 0;
          if (newComp !== prevComp) {
            this.fetchHistory();
          }
          const remJobs = this.totalActiveCount + this.totalWaitingCount + this.totalWaitingDiskCount;
          document.title = remJobs > 0 ? `(${remJobs}) HB Manager` : 'HB Manager';
          if (d.settings.ui_lang && ['en', 'cs'].includes(d.settings.ui_lang) && Date.now() > this.langLockUntil) {
            if (this.uiLang !== d.settings.ui_lang) {
              this.uiLang = d.settings.ui_lang;
              localStorage.setItem('hb_ui_lang', this.uiLang);
              document.documentElement.setAttribute('lang', this.uiLang);
            }
          }
          if (d.settings && d.settings.ui_prefs) this.applyUiPrefs(d.settings.ui_prefs);
          if (!this._emptyPulseChecked && Array.isArray(d.queue)) {
            // první načtení stránky rovnou na prázdné frontě: + jednou zapulzuje (stejně jako při přepnutí záložky)
            this._emptyPulseChecked = true;
            if (this.activeTab === 'workers' && (d.workers || []).length === 0) {
              this.pulseAddWorkerBtn = true;
              clearTimeout(this._pulseAddWorkerTimer);
              this._pulseAddWorkerTimer = setTimeout(() => { this.pulseAddWorkerBtn = false; }, 6000);
            }
            if (this.activeTab === 'queue' && d.queue.length === 0) {
              this.pulseAddBtn = true;
              clearTimeout(this._pulseAddTimer);
              this._pulseAddTimer = setTimeout(() => { this.pulseAddBtn = false; }, 6000);
            }
          }
          if (!this.bulkDefaultsLoaded && d.settings) {
            if (d.settings.default_staging_mode) this.bulkStagingSelect = d.settings.default_staging_mode;
            if (d.settings.default_power_mode) this.bulkPowerSelect = d.settings.default_power_mode;
            this.bulkDefaultsLoaded = true;
          }
          if (this.activeTab === 'logs') {
            this.fetchLogs();
          }
          this.updateDynamicFavicon();
          this.$nextTick(() => {
            this.checkHeaderWorkersOverflow();
            this.syncPulseAnimations();
          });
        }
      } catch (e) {
      } finally {
        clearTimeout(tid);
        this.isFetchingState = false;
        if (this.pendingForcedFetch) {
          this.pendingForcedFetch = false;
          this.fetchState(true);
        }
      }
    },
    async fetchHistory() {
      try {
        const r = await this.apiFetch('/api/v2/history', { cache: 'no-store' });
        if (r.ok) {
          const d = await r.json();
          // Zmrazený seznam: Vue nevytváří hluboké reaktivní proxy pro tisíce řádků historie
          // (položky se v UI nikdy nemutují) -> výrazně rychlejší render záložky Dokončené.
          this.historyList = Object.freeze((d.history || []).map(h => Object.freeze(h)));
        }
      } catch (e) {}
    },
    isSettingsEditLocked() {
      if (this.settingsDirty || this.isSavingSettings) return true;
      if (this.showOnboardingModal || this.addWorkerModal?.open || this.folderBrowser?.open || this.smbModal?.open) return true;
      const ae = typeof document !== 'undefined' ? document.activeElement : null;
      if (this.activeTab === 'settings' && ae && ['INPUT', 'TEXTAREA', 'SELECT'].includes(ae.tagName)) return true;
      return false;
    },
    markSettingsDirty(ev) {
      const tg = ev?.target;
      if (!tg || tg.type === 'file') return;
      if (tg.tagName === 'SELECT' || tg.dataset?.instantSave === '1' || tg.name === 'manual_upload_version') return;
      if (['INPUT', 'TEXTAREA'].includes(tg.tagName)) {
        this.settingsDirty = true;
      }
    },
    discardSettingsChanges() {
      if (this.settingsSnapshot) {
        try {
          const restored = JSON.parse(this.settingsSnapshot);
          this.state.settings = restored;
          if (restored.ui_lang && ['en', 'cs'].includes(restored.ui_lang) && restored.ui_lang !== this.uiLang) {
            this.uiLang = restored.ui_lang;
            localStorage.setItem('hb_ui_lang', this.uiLang);
            document.documentElement.setAttribute('lang', this.uiLang);
          }
        } catch (e) {}
      }
      this.settingsDirty = false;
      const ae = typeof document !== 'undefined' ? document.activeElement : null;
      if (ae && ['INPUT', 'TEXTAREA', 'SELECT'].includes(ae.tagName) && typeof ae.blur === 'function') {
        ae.blur();
      }
    },
    async fetchChangelog() {
      try { const r = await this.apiFetch('/api/v2/changelog'); if (r.ok) { const d = await r.json(); this.changelogText = d.markdown || ''; } } catch (e) {}
    },
    async fetchLogs() {
      if (this.isFetchingLogs) return;
      this.isFetchingLogs = true;
      const ctrl = new AbortController();
      const tid = setTimeout(() => ctrl.abort(), 6000);
      try {
        const r = await this.apiFetch('/api/v2/logs?worker_name=ALL', { cache: 'no-store', signal: ctrl.signal });
        if (r.ok) {
          const before = this.visibleLogEntries.length;
          const lastKey = before ? this.visibleLogEntries[before - 1].raw : '';
          this.logsData = await r.json();
          if (this.logsAutoScroll && !this.logsUserScrolled) {
            this.scrollLogsToBottom();
          } else {
            // uživatel čte starší řádky -> neposouvat, jen ukázat počet nových
            const arr = this.visibleLogEntries;
            let idx = -1;
            for (let i = arr.length - 1; i >= 0; i--) { if (arr[i].raw === lastKey) { idx = i; break; } }
            const added = idx >= 0 ? (arr.length - 1 - idx) : 0;
            if (added > 0) this.logsNewCount += added;
          }
        }
      } catch (e) {
      } finally {
        clearTimeout(tid);
        this.isFetchingLogs = false;
      }
    },
    workerActiveJobs(w) {
      const act = RUNNING_STATES.concat(['WAITING_DISK']);
      return (this.state?.queue || []).filter(j => j.worker_name === w.worker_name && act.includes(j.status) && !j.is_system_job);
    },
    // Ikona a popis systému workeru (stejné v Monitoringu i na záložce Workery)
    workerOsInfo(w) {
      const d = w?.telemetry?.diag || {};
      const os = String(d.os || '').trim();
      if (w?.is_windows !== false) {
        return { icon: 'windows', label: /^Windows\s+\d+/i.test(os) ? os : 'Windows' };
      }
      const host = String(d.host_os || '').trim();
      return { icon: 'docker', label: 'Linux · Docker' + (host ? ` (${host})` : '') };
    },
    // IP workeru: Windows / Linux s host sítí pošle svou skutečnou adresu (rozhraní k Manageru);
    // dockerová vnitřní (172.16–31.x) u Linuxu se nahradí adresou, kterou vidí Manager
    workerIpInfo(w) {
      const net = w?.telemetry?.diag?.net || {};
      const isDockerNet = (ip) => w?.is_windows === false && /^172\.(1[6-9]|2\d|3[01])\./.test(ip || '');
      let ip = String(net.route_ip || '').trim();
      if (!ip || isDockerNet(ip)) ip = String(w?.seen_ip || '').trim();
      if (ip === '127.0.0.1' || ip === '::1') ip = '';
      const others = (Array.isArray(net.ips) ? net.ips : []).filter(x => x && x !== ip && !isDockerNet(x)).slice(0, 6);
      return { ip, others };
    },
    async copyWorkerIp(ip) {
      if (!ip) return;
      let ok = false;
      try { if (navigator.clipboard && window.isSecureContext) { await navigator.clipboard.writeText(ip); ok = true; } } catch (e) { ok = false; }
      if (!ok) {
        // http:// v LAN nemá clipboard API → záložní kopírování přes skryté pole
        try {
          const ta = document.createElement('textarea');
          ta.value = ip; ta.setAttribute('readonly', ''); ta.style.position = 'fixed'; ta.style.opacity = '0';
          document.body.appendChild(ta); ta.select(); ok = document.execCommand('copy'); document.body.removeChild(ta);
        } catch (e) { ok = false; }
      }
      if (ok) this.toast(this.t('toast_ip_copied', ip));
    },
    formatLastSeen(ts) {
      const n = Number(ts || 0);
      if (!n) return '?';
      const d = new Date(n * 1000);
      const now = new Date();
      const pad = (x) => String(x).padStart(2, '0');
      const hm = `${pad(d.getHours())}:${pad(d.getMinutes())}`;
      return d.toDateString() === now.toDateString() ? hm : `${d.getDate()}. ${d.getMonth() + 1}. ${hm}`;
    },
    isLogSourceOn(src) { return (this.logSources || []).includes(src); },
    setLogSources(list) {
      this.logSources = Array.from(new Set(list || []));
      this.saveUiPref('log_sources', this.logSources);
      this.$nextTick(() => this.scrollLogsToBottom());
    },
    toggleLogSource(src) {
      const cur = new Set(this.logSources || []);
      if (cur.has(src)) cur.delete(src); else cur.add(src);
      this.setLogSources(Array.from(cur));
    },
    openWorkerLogs(name) {
      this.switchTab('logs');
      this.setLogSources(['MANAGER', name]);
      this.fetchLogs();
    },
    logSourceColor(src) {
      if (src === 'MANAGER') return '#38bdf8';
      const pal = ['#34d399', '#fbbf24', '#f472b6', '#a3e635', '#fb923c', '#2dd4bf', '#c084fc', '#f87171', '#facc15', '#60a5fa'];
      let h = 0;
      for (const ch of String(src || '')) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
      return pal[h % pal.length];
    },
    onLogsScroll(ev) {
      const el = ev?.target;
      if (!el) return;
      const nearBottom = (el.scrollHeight - el.scrollTop - el.clientHeight) < 40;
      this.logsUserScrolled = !nearBottom;
      if (nearBottom) this.logsNewCount = 0;
    },
    scrollLogsToBottom() {
      this.$nextTick(() => {
        const el = this.$refs.logsContainer;
        if (el) el.scrollTop = el.scrollHeight;
        this.logsUserScrolled = false;
        this.logsNewCount = 0;
      });
    },
    downloadLogs() {
      const lines = this.visibleLogEntries.map(e => e.raw).join('\n') + '\n';
      const blob = new Blob([lines], { type: 'text/plain;charset=utf-8' });
      const d = new Date();
      const pad = (n) => String(n).padStart(2, '0');
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = `hb-cluster-log-${d.getFullYear()}${pad(d.getMonth() + 1)}${pad(d.getDate())}-${pad(d.getHours())}${pad(d.getMinutes())}.txt`;
      document.body.appendChild(a); a.click(); a.remove();
      setTimeout(() => URL.revokeObjectURL(a.href), 2000);
    },
    async triggerClearLogs() {
      if (this.requireTwoClickConfirm && !this.confirmingClearLogs) {
        this.confirmingClearLogs = true;
        setTimeout(() => { this.confirmingClearLogs = false; }, 4000);
        return;
      }
      this.confirmingClearLogs = false;
      await this.clearLogs('all');
    },
    async clearLogs(target) {
      const r = await this.apiFetch('/api/v2/logs/clear', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ target }) });
      if (r && r.ok) {
        this.toast(this.t('toast_logs_cleared'));
      } else {
        this.toast(this.t('toast_action_failed'), 'error');
      }
      this.fetchLogs();
    },
    triggerDestructiveAction(jid, act) {
      if (Date.now() < this.rowShiftFreezeUntil) return;
      if (this.requireTwoClickConfirm && this.confirmingMap[jid] !== act) {
        this.confirmingMap[jid] = act;
        if (this.confirmTimers[jid]) clearTimeout(this.confirmTimers[jid]);
        this.confirmTimers[jid] = setTimeout(() => { if (this.confirmingMap[jid] === act) delete this.confirmingMap[jid]; }, 3500);
        return;
      }
      delete this.confirmingMap[jid];
      this.rowShiftFreezeUntil = Date.now() + 450;
      this.jobAction(jid, act);
    },
    renumberLocalQueue(list) {
      let c = 1;
      for (const it of list) {
        if (it.is_system_job) it.priority = 0;
        else if (it.status === 'WAITING' || it.status === 'PAUSED') it.priority = c++;
      }
    },
    async jobAction(jid, act) {
      if (this.busyJobs[jid]) return;
      this.busyJobs[jid] = true;
      this.pausePollingUntil = Date.now() + 1200;
      const q = [...this.state.queue];
      const idx = q.findIndex(j => j.id === jid);
      if (idx !== -1) {
        if (act === 'delete') { q.splice(idx, 1); this.selectedIds = this.selectedIds.filter(x => x !== jid); }
        else if (act === 'cancel') { q[idx].status = 'CANCELLED'; q[idx].is_paused = false; }
        else if (act === 'freeze') { q[idx].is_paused = true; }
        else if (act === 'unfreeze') { q[idx].is_paused = false; }
        else if (act === 'pause') {
          if (RUNNING_STATES.includes(q[idx].status)) q[idx].is_paused = true;
          else q[idx].status = 'PAUSED';
        }
        else if (act === 'resume' || act === 'requeue') {
          if (RUNNING_STATES.includes(q[idx].status)) {
            q[idx].is_paused = false;
          } else {
            const [it] = q.splice(idx, 1);
            it.status = 'WAITING'; it.is_paused = false; it.progress = 0; it.attempt_count = 0; it.error_code = '';
            const fIdx = q.findIndex(x => !RUNNING_STATES.includes(x.status) && !x.is_system_job);
            q.splice(fIdx >= 0 ? fIdx : 0, 0, it);
          }
        } else if (act === 'release_disk') {
          q[idx].status = 'COPYING';
        } else if (act === 'up' && idx > 0) { [q[idx - 1], q[idx]] = [q[idx], q[idx - 1]]; }
        else if (act === 'down' && idx < q.length - 1) { [q[idx + 1], q[idx]] = [q[idx], q[idx + 1]]; }
        else if (act === 'to_top') {
          const [it] = q.splice(idx, 1);
          const fIdx = q.findIndex(x => !RUNNING_STATES.includes(x.status) && !x.is_system_job);
          q.splice(fIdx >= 0 ? fIdx : 0, 0, it);
        } else if (act === 'to_bottom') { const [it] = q.splice(idx, 1); q.push(it); }
        this.renumberLocalQueue(q);
        this.state.queue = q;
        this.updateDynamicFavicon();
      }
      try {
        const r = await this.apiFetch('/api/v2/jobs/action', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ job_ids: [jid], action: act }) });
        const d = (r && r.ok) ? await r.json().catch(() => ({})) : {};
        if (act === 'release_disk' && d.lost_count > 0) {
          this.toast(this.t('toast_held_output_lost'), 'warn');
        }
      } finally {
        delete this.busyJobs[jid];
        this.pausePollingUntil = Date.now() + 300;
        this.fetchState(true);
      }
    },
    async triggerBulkAction(act) {
      if (!this.selectedIds.length) return;
      const isDestructive = (act === 'cancel' || act === 'delete');
      if (isDestructive && this.requireTwoClickConfirm && this.confirmingBulkAction !== act) {
        this.confirmingBulkAction = act;
        if (this._bulkConfirmTimer) clearTimeout(this._bulkConfirmTimer);
        this._bulkConfirmTimer = setTimeout(() => { if (this.confirmingBulkAction === act) this.confirmingBulkAction = ''; }, 4000);
        return;
      }
      this.confirmingBulkAction = '';
      if (this._bulkConfirmTimer) { clearTimeout(this._bulkConfirmTimer); this._bulkConfirmTimer = null; }
      return this.bulkAction(act);
    },
    async bulkAction(act) {
      if (!this.selectedIds.length) return;
      const ids = [...this.selectedIds];
      this.pausePollingUntil = Date.now() + 1500;
      const eff = (act === 'resume') ? 'requeue' : act;
      const r = await this.apiFetch('/api/v2/jobs/action', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ job_ids: ids, action: eff }) });
      if (r && r.ok) {
        this.selectedIds = [];
      } else {
        this.toast(this.t('toast_action_failed'), 'error');
      }
      this.pausePollingUntil = 0; this.fetchState(true);
    },
    toggleSelectJob(jid, idx, ev) {
      const visibleList = this.activeTab === 'queue' ? this.paginatedOtherJobs : this.otherJobs;
      if (ev.shiftKey && this.lastCheckedIdx !== null) {
        const a = Math.min(this.lastCheckedIdx, idx); const b = Math.max(this.lastCheckedIdx, idx);
        const set = new Set(this.selectedIds);
        visibleList.slice(a, b + 1).forEach(j => set.add(j.id));
        this.selectedIds = Array.from(set);
      } else {
        if (this.selectedIds.includes(jid)) this.selectedIds = this.selectedIds.filter(x => x !== jid);
        else this.selectedIds.push(jid);
      }
      this.lastCheckedIdx = idx;
    },
    selectAllVisibleWaiting() {
      const visibleList = this.activeTab === 'queue' ? this.paginatedOtherJobs : this.otherJobs;
      this.selectedIds = visibleList.map(j => j.id);
    },
    initSortable() {
      if (this.sortableInstance) {
        this.sortableInstance.destroy();
        this.sortableInstance = null;
      }
      const el = this.$refs.sortableContainer;
      if (!el || typeof Sortable === 'undefined') return;
      this.sortableInstance = new Sortable(el, {
        handle: '.drag-handle', animation: 150, ghostClass: 'sortable-ghost', dragClass: 'sortable-drag',
        disabled: !this.canDragReorder,
        onStart: () => { this.isDragging = true; },
        onEnd: async (evt) => {
          this.isDragging = false;
          if (!this.canDragReorder || evt.oldIndex === evt.newIndex) return;
          const sub = [...this.paginatedOtherJobs];
          const [mv] = sub.splice(evt.oldIndex, 1);
          sub.splice(evt.newIndex, 0, mv);
          const st = this.enablePagination ? (this.queuePage - 1) * this.perPage : 0;
          const all = [...this.otherJobs];
          all.splice(st, sub.length, ...sub);
          this.pausePollingUntil = Date.now() + 1500;
          const r = await this.apiFetch('/api/v2/jobs/reorder', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ ordered_ids: all.map(j => j.id) }) });
          if (!r || !r.ok) this.toast(this.t('toast_action_failed'), 'error');
          this.pausePollingUntil = 0; this.fetchState(true);
        }
      });
    },
    async inspectJob(jid) {
      const r = await this.apiFetch(`/api/v2/jobs/${jid}/inspect`);
      if (r.ok) { this.inspectModal.data = await r.json(); this.inspectModal.open = true; this.pushModalHistory(); }
    },
    async openDuplicatesModal() {
      const r = await this.apiFetch('/api/v2/jobs/duplicates');
      if (r.ok) {
        const d = await r.json();
        if (!d.exact_groups.length && !d.diff_groups.length) { this.toast(this.t('toast_no_dups')); return; }
        this.dupModal.exact = d.exact_groups; this.dupModal.diff = d.diff_groups; this.dupModal.includeDiff = false; this.dupModal.open = true; this.pushModalHistory();
      }
    },
    async resolveDuplicates(act) {
      const r = await this.apiFetch('/api/v2/jobs/duplicates/resolve', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action: act, keep_mode: this.dupModal.keepMode, include_diff: this.dupModal.includeDiff })
      });
      if (r.ok) { this.dupModal.open = false; this.fetchState(true); }
    },
    async clusterAction(act) {
      this.pausePollingUntil = Date.now() + 1500;
      const r = await this.apiFetch('/api/v2/cluster/action', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ action: act }) });
      if (!r || !r.ok) {
        this.toast(this.t('toast_action_failed'), 'error');
      } else {
        const d = await r.json().catch(() => ({}));
        if (act === 'release_all_disk') {
          if (d.lost_count > 0) this.toast(this.t('toast_held_output_lost'), 'warn');
          else this.toast(this.t('try_upload_all_now'));
        }
      }
      this.pausePollingUntil = 0;
      this.fetchState(true);
      if (this.activeTab === 'history') this.fetchHistory();
    },
    async saveGlobalSettings(showToast = true) {
      if (this.state && this.state.settings) {
        this.state.settings.ui_lang = this.uiLang;
      }
      this._settingsMutationSeq = (this._settingsMutationSeq || 0) + 1;
      this.isSavingSettings = true;
      this.pausePollingUntil = Date.now() + 3000;
      try {
        const r = await this.apiFetch('/api/v2/cluster/action', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ action: 'update_settings', settings: this.state.settings }) });
        const d = r.ok ? await r.json().catch(() => ({})) : {};
        if (r.ok && d.ok !== false) {
          if (d.smart_premux !== undefined) this.state.settings.smart_premux = d.smart_premux;
          this.settingsSnapshot = JSON.stringify(this.state.settings);
          this.settingsDirty = false;
          this.pausePollingUntil = Date.now() + 1200;
          if (showToast) this.toast(this.t('toast_settings_saved'));
          return true;
        }
        this.toast(d.detail || this.t('toast_settings_save_failed'), 'error');
        return false;
      } finally {
        this.isSavingSettings = false;
      }
    },
    openSmbModal() {
      const s = this.state.settings || {};
      const rawUser = (s.smb_username || '').trim();
      let dom = '';
      let usr = rawUser;
      if (rawUser.includes('\\')) {
        const idx = rawUser.indexOf('\\');
        dom = rawUser.slice(0, idx).trim();
        usr = rawUser.slice(idx + 1).trim();
      }
      this.smbSettingsTest = { running: false, result: null };
      this.confirmingClearSmb = false;
      this.smbModal = {
        tested: false,
        open: true,
        domain: dom,
        username: usr,
        password: s.smb_password || '',
        showPassword: false
      };
      this.pushModalHistory();
    },
    async saveSmbModal() {
      const m = this.smbModal;
      const u = (m.username || '').trim();
      const d = (m.domain || '').trim();
      const combinedUser = (d && u && !u.includes('\\')) ? `${d}\\${u}` : u;
      if (!this.state.settings) this.state.settings = {};
      this.state.settings.smb_username = combinedUser;
      this.state.settings.smb_password = m.password || '';
      this.addWorkerModal.smbUsername = combinedUser;
      this.addWorkerModal.smbPassword = m.password || '';
      const ok = await this.saveGlobalSettings();
      if (ok) {
        // Uložit a rovnou otestovat Windows cesty s novým účtem; výsledek se ukáže přímo v okně.
        m.tested = true;
        await this.testSmbSettingsCard();
      }
    },
    async triggerClearSmbModal() {
      if (this.requireTwoClickConfirm && !this.confirmingClearSmb) {
        this.confirmingClearSmb = true;
        if (this._clearSmbTimer) clearTimeout(this._clearSmbTimer);
        this._clearSmbTimer = setTimeout(() => { this.confirmingClearSmb = false; }, 4000);
        return;
      }
      this.confirmingClearSmb = false;
      if (this._clearSmbTimer) { clearTimeout(this._clearSmbTimer); this._clearSmbTimer = null; }
      return this.clearSmbModal();
    },
    async clearSmbModal() {
      if (!this.state.settings) this.state.settings = {};
      this.state.settings.smb_username = '';
      this.state.settings.smb_password = '';
      this.smbModal = { open: false, domain: '', username: '', password: '', showPassword: false };
      this.addWorkerModal.smbUsername = '';
      this.addWorkerModal.smbPassword = '';
      await this.saveGlobalSettings();
    },
    async testSmbSettingsCard() {
      const s = this.state.settings || {};
      this._settingsMutationSeq = (this._settingsMutationSeq || 0) + 1;
      this.smbSettingsTest = { running: true, result: null };
      try {
        const r = await this.apiFetch('/api/v2/settings/test_smb_and_save', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            win_default_inputs_dir: (s.win_default_inputs_dir || '').trim(),
            win_default_outputs_dir: (s.win_default_outputs_dir || '').trim(),
            smb_username: (s.smb_username || '').trim(),
            smb_password: s.smb_password || '',
            linux_default_inputs_dir: (s.linux_default_inputs_dir || '').trim(),
            linux_default_outputs_dir: (s.linux_default_outputs_dir || '').trim(),
            linux_default_temp_dir: (s.linux_default_temp_dir || '').trim()
          })
        }, 60000);
        if (!r.ok) {
          this.smbSettingsTest = { running: false, result: { ok: false, rows: [{ label: 'Manager', ok: false, detail: this.t('toast_mgr_unreachable') }] } };
          return;
        }
        const d = await r.json().catch(() => ({}));
        this.settingsSnapshot = JSON.stringify(this.state.settings || {});
        this.settingsDirty = false;
        this.smbSettingsTest = {
          running: false,
          result: {
            ok: Boolean(d.ok),
            rows: [
              { label: 'Inputs', path: d.win_in?.path, ok: Boolean(d.win_in?.ok), skipped: Boolean(d.win_in?.skipped), detail: d.win_in?.detail || '' },
              { label: 'Outputs', path: d.win_out?.path, ok: Boolean(d.win_out?.ok), skipped: Boolean(d.win_out?.skipped), detail: d.win_out?.detail || '' }
            ]
          }
        };
        this.toast(d.ok ? this.t('toast_smb_test_ok') : this.t('toast_smb_test_fail'), d.ok ? 'info' : 'warn');
      } catch (e) {
        this.smbSettingsTest = { running: false, result: { ok: false, rows: [{ label: 'Error', ok: false, detail: String(e) }] } };
      }
    },
    defaultSlotsForCap(cap) {
      const c = String(cap || 'all').toLowerCase();
      if (c === 'sd' || c === '720p') return 1;
      if (c === '1080p') return 2;
      return 4;
    },
    async onWorkerCapabilityChange(w) {
      const prevCap = this.workerCapPrev[w.worker_name] || 'all';
      const oldDef = this.defaultSlotsForCap(prevCap);
      const newDef = this.defaultSlotsForCap(w.capability);
      if (parseInt(w.max_slots, 10) === oldDef) {
        w.max_slots = newDef;
      }
      this.workerCapPrev[w.worker_name] = w.capability || 'all';
      await this.saveWorkerPolicy(w);
    },
    getWorkerCpuTopology(w) {
      const diag = w?.telemetry?.diag || {};
      const topo = diag.cpu_topology || {};
      const tot = Math.max(1, parseInt(topo.total_threads || w?.telemetry?.cores_total || w?.telemetry?.cores || 8, 10) || 8);
      const phys = Math.max(1, parseInt(topo.physical_cores || diag.physical_cores || tot, 10) || tot);
      const isHybrid = Boolean(topo.is_hybrid ?? diag.is_hybrid_cpu);
      const hasSmt = Boolean(topo.has_smt ?? diag.has_smt ?? (tot > phys));
      const eIndices = Array.isArray(topo.e_core_indices) ? topo.e_core_indices : [];
      const pIndices = Array.isArray(topo.p_core_indices) && topo.p_core_indices.length > 0
        ? topo.p_core_indices
        : Array.from({ length: tot }, (_, i) => i).filter(i => !eIndices.includes(i));
      const noSmtIndices = Array.isArray(topo.no_smt_indices) && topo.no_smt_indices.length > 0
        ? topo.no_smt_indices
        : (hasSmt ? Array.from({ length: tot }, (_, i) => i).filter(i => i % 2 === 0) : Array.from({ length: tot }, (_, i) => i));
      return {
        is_hybrid: isHybrid,
        total_threads: tot,
        physical_cores: phys,
        has_smt: hasSmt,
        p_core_indices: pIndices,
        e_core_indices: eIndices,
        no_smt_indices: noSmtIndices,
        p_core_count: pIndices.length,
        e_core_count: eIndices.length || parseInt(diag.e_core_count || 0, 10) || 0
      };
    },
    formatWorkerCpuTopoBadge(w) {
      const t = this.getWorkerCpuTopology(w);
      if (t.is_hybrid) {
        return `${t.physical_cores}C/${t.total_threads}T (P:${t.p_core_count}t | E:${t.e_core_count}t)`;
      }
      if (t.has_smt) {
        return `${t.physical_cores}C/${t.total_threads}T (HT/SMT)`;
      }
      return `${t.total_threads}C/${t.total_threads}T`;
    },
    getWorkerCustomCoresList(w) {
      const t = this.getWorkerCpuTopology(w);
      const raw = (Array.isArray(w?.custom_cores) && w.custom_cores.length > 0)
        ? w.custom_cores
        : ((Array.isArray(w?.telemetry?.custom_cores) && w.telemetry.custom_cores.length > 0) ? w.telemetry.custom_cores : []);
      const valid = raw.map(x => parseInt(x, 10)).filter(x => Number.isInteger(x) && x >= 0 && x < t.total_threads);
      if (valid.length > 0) return Array.from(new Set(valid)).sort((a, b) => a - b);
      return Array.from({ length: t.total_threads }, (_, i) => i);
    },
    isWorkerCoreSelected(w, idx) {
      return this.getWorkerCustomCoresList(w).includes(idx);
    },
    isWorkerCoreECore(w, idx) {
      const t = this.getWorkerCpuTopology(w);
      return t.is_hybrid && t.e_core_indices.includes(idx);
    },
    // Kolečko „Nastavení workeru“ (všichni workeři): rozlišení, staging, lokální disk, jádra CPU, u Windows i integrace
    // Panel Nastavení workeru se sám zasune, když nad kartou workeru minutu nebyla myš
    // (ne když je v něm rozepsaná a neuložená Windows integrace nebo v něm je kurzor v poli).
    touchWorkerPanel(w) {
      if (!w || !w.worker_name || !this.expandedWorkerSettings[w.worker_name]) return;
      (this._panelSeen = this._panelSeen || {})[w.worker_name] = Date.now();   // nereaktivně – bez překreslování
    },
    workerWinDraftDirty(w) {
      const d = this.workerWinDrafts[w.worker_name];
      if (!d) return false;
      const ws = w.windows_settings || {};
      const s = this.state.settings || {};
      return d.autostart_mode !== (ws.autostart_mode || 'after_login') || d.smb_server !== (ws.smb_server || '') ||
        d.smb_user !== (ws.smb_user || s.smb_username || '') || d.smb_pass !== (ws.smb_pass || s.smb_password || '') ||
        d.desktop_shortcut !== (ws.desktop_shortcut !== false) || d.start_menu_shortcut !== (ws.start_menu_shortcut !== false) ||
        d.tray_minimize !== (ws.tray_minimize !== false) || d.sleep_prevention_mode !== (ws.sleep_prevention_mode || 'encoding') ||
        d.skip_tray_prompt !== Boolean(ws.skip_tray_prompt) || d.show_log !== Boolean(ws.show_log) || d.wake_check !== (ws.wake_check !== false);
    },
    closeIdleWorkerPanels() {
      const now = Date.now();
      const seen = this._panelSeen || {};
      (this.state.workers || []).forEach(w => {
        const n = w.worker_name;
        if (!this.expandedWorkerSettings[n]) return;
        if (!seen[n]) { seen[n] = now; this._panelSeen = seen; return; }
        if (now - seen[n] < 60000) return;
        const card = document.querySelector(`[data-worker-card="${CSS.escape(n)}"]`);
        if (card && (card.matches(':hover') || card.contains(document.activeElement))) { seen[n] = now; return; }
        if (w.is_windows !== false && this.workerWinDraftDirty(w)) return;
        this.toggleWorkerSettingsPanel(w);
        delete seen[n];
      });
    },
    toggleWorkerSettingsPanel(w) {
      if (!w || !w.worker_name) return;
      const open = !this.expandedWorkerSettings[w.worker_name];
      if (open) (this._panelSeen = this._panelSeen || {})[w.worker_name] = Date.now();
      if (w.is_windows !== false && Boolean(this.expandedWinSettings[w.worker_name]) !== open) this.toggleWorkerWinSettings(w);
      this.expandedWorkerSettings[w.worker_name] = open;
    },
    onWorkerPowerModeChange(w) {
      this.saveWorkerPolicy(w);
      if (w.power_mode === 'CUSTOM' && !this.expandedWorkerSettings[w.worker_name]) this.toggleWorkerSettingsPanel(w);
    },
    async applyWorkerCpuPreset(w, preset) {
      if (!w) return;
      const t = this.getWorkerCpuTopology(w);
      let next = [];
      if (preset === 'p_only' && t.p_core_indices.length > 0) next = [...t.p_core_indices];
      else if (preset === 'e_only' && t.e_core_indices.length > 0) next = [...t.e_core_indices];
      else if (preset === 'no_smt' && t.no_smt_indices.length > 0) next = [...t.no_smt_indices];
      else next = Array.from({ length: t.total_threads }, (_, i) => i);
      w.custom_cores = next;
      w.power_mode = 'CUSTOM';
      this.expandedCustomCpu[w.worker_name] = true;
      await this.saveWorkerPolicy(w);
    },
    async toggleWorkerCustomCore(w, idx) {
      if (!w) return;
      const cur = new Set(this.getWorkerCustomCoresList(w));
      if (cur.has(idx)) {
        if (cur.size <= 1) return;
        cur.delete(idx);
      } else {
        cur.add(idx);
      }
      w.custom_cores = Array.from(cur).sort((a, b) => a - b);
      w.power_mode = 'CUSTOM';
      this.expandedCustomCpu[w.worker_name] = true;
      await this.saveWorkerPolicy(w);
    },
    async saveWorkerPolicy(w) {
      const defSlots = this.defaultSlotsForCap(w.capability);
      w.max_slots = Math.max(1, Math.min(16, parseInt(w.max_slots || defSlots, 10) || defSlots));
      this.workerCapPrev[w.worker_name] = w.capability || 'all';
      if (w.power_mode === 'CUSTOM') {
        this.expandedCustomCpu[w.worker_name] = true;
        if (!Array.isArray(w.custom_cores) || w.custom_cores.length === 0) {
          w.custom_cores = this.getWorkerCustomCoresList(w);
        }
      }
      this.pausePollingUntil = Date.now() + 1500;
      const r = await this.apiFetch(`/api/v2/workers/${encodeURIComponent(w.worker_name)}/policy`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          max_slots: w.max_slots,
          capability: w.capability,
          allow_4k: w.capability !== '1080p' && w.capability !== 'sd',
          staging_mode: w.staging_mode,
          power_mode: w.power_mode,
          custom_cores: Array.isArray(w.custom_cores) ? w.custom_cores : undefined,
          prioritize_4k: w.prioritize_4k,
          use_local_temp: w.use_local_temp,
          is_enabled: w.is_enabled
        })
      });
      this.pausePollingUntil = 0;
      if (!r || !r.ok) {
        this.toast(`${w.worker_name}: ${this.t('toast_settings_save_failed')}`, 'error');
        this.fetchState(true);
        return;
      }
      const d = await r.json().catch(() => ({}));
      if (d.smart_premux !== undefined && this.state.settings) this.state.settings.smart_premux = d.smart_premux;
      if (d.policy) Object.assign(w, d.policy);           // Manager mohl sloty / 4K srovnat (4K = 4 sloty)
      if (d.note) {
        this.slotsFlash = { ...this.slotsFlash, [w.worker_name]: true };
        setTimeout(() => { this.slotsFlash = { ...this.slotsFlash, [w.worker_name]: false }; }, 2600);
        this.toast(`${w.worker_name}: ${this.t(d.note === 'slots_up_4' ? 'toast_slots_up_4' : 'toast_slots_4k_off')}`, 'warn');
        return;
      }
      this.toast(`${w.worker_name}: ${this.t('toast_worker_saved')}`);
    },
    // Schopnosti Windows PC hlášené workerem (od dev.38): podle nich se šednou volby, které na tomto PC nemohou fungovat.
    winCaps(w) {
      const c = w?.windows_settings?.caps;
      return (c && typeof c === 'object') ? c : {};
    },
    winCapBlocked(w, what) {
      const c = this.winCaps(w);
      if (what === 'wake') return c.wake_timers === 'disabled' || c.wake_timers === 'important_only';
      if (what === 'before_login') return c.can_elevate === false;
      return false;
    },
    // W3: výsledek kontroly práv Windows workeru při startu (caps.priv) jako jeden řádek
    winPrivSummary(w) {
      const p = this.winCaps(w).priv;
      if (!p || typeof p !== 'object') return '';
      const v = (x) => (x === 'ok' ? 'OK' : (x === 'unsupported' ? this.t('w_priv_unsupported') : this.t('w_priv_fail')));
      const tk = { ok: 'w_priv_task_ok', legacy: 'w_priv_task_legacy', none: 'w_priv_task_none', highest: 'w_priv_task_legacy', limited: 'w_priv_task_legacy' }[p.task] || 'w_priv_task_unknown';
      return this.t('w_priv_selftest', this.t(p.admin ? 'w_priv_yes' : 'w_priv_no'), v(p.priority), v(p.affinity), v(p.wake_timer), v(p.keep_awake), this.t(tk));
    },
    // Stav naplánované úlohy před přihlášením (C+): UAC na PC, chybějící nebo stará úloha → hláška + tlačítko opravy
    winAutostartAlert(w) {
      if (!w || w.is_windows === false) return null;
      const c = this.winCaps(w);
      const ws = w.windows_settings || {};
      const mode = ws.autostart_mode || 'after_login';
      const st = String(c.autostart_status || '').toLowerCase();
      const task = String(c.autostart_task || c.priv?.task || '').toLowerCase();
      const name = w.worker_name || '';
      const canElev = !this.winCapBlocked(w, 'before_login');
      const repairBtn = this.t(mode === 'before_login' ? 'w_autostart_repair_btn' : 'w_autostart_remove_btn');
      if (st === 'uac_pending') {
        return { level: 'info', text: this.t('wrk_autostart_uac_pending', name), canRepair: false, repairLabel: '' };
      }
      if (st === 'uac_cancelled') {
        return { level: 'warn', text: this.t('wrk_autostart_uac_cancelled', name), canRepair: canElev, repairLabel: repairBtn };
      }
      if (st === 'uac_no_desktop') {
        return { level: 'warn', text: this.t('wrk_autostart_uac_no_desktop', name), canRepair: false, repairLabel: '' };
      }
      if (st === 'no_admin') {
        return { level: 'err', text: this.t('wrk_cap_before_login_no_admin'), canRepair: false, repairLabel: '' };
      }
      if (st === 'failed' || (!st && c.autostart_error)) {
        return { level: 'err', text: this.t('wrk_cap_autostart_failed', c.autostart_error || '?'), canRepair: canElev, repairLabel: repairBtn };
      }
      if (mode === 'before_login' && task === 'none') {
        return { level: 'warn', text: this.t('wrk_autostart_task_missing'), canRepair: canElev, repairLabel: this.t('w_autostart_repair_btn') };
      }
      if (mode === 'before_login' && (task === 'legacy' || task === 'highest' || task === 'limited')) {
        return { level: 'warn', text: this.t('wrk_autostart_task_legacy'), canRepair: canElev, repairLabel: this.t('w_autostart_repair_btn') };
      }
      if (mode !== 'before_login' && (task === 'ok' || task === 'legacy' || task === 'highest' || task === 'limited')) {
        return { level: 'warn', text: this.t('wrk_autostart_task_leftover'), canRepair: canElev, repairLabel: this.t('w_autostart_remove_btn') };
      }
      return null;
    },
    winCapNotes(w) {
      const c = this.winCaps(w);
      const out = [];
      if (c.wake_timers === 'disabled' || c.wake_timers === 'important_only') out.push({ level: 'block', text: this.t('wrk_cap_wake_disabled') });
      if (c.modern_standby) out.push({ level: 'info', text: this.t('wrk_cap_modern_standby') });
      if (c.has_battery && c.on_ac === false) out.push({ level: 'info', text: this.t('wrk_cap_on_battery') });
      return out;
    },
    linuxCapNotes(w) {
      // co Linux kontejner neumí (hlásí worker v diag.linux_caps) → vysvětlení, co nastavit
      const c = w?.telemetry?.diag?.linux_caps;
      if (!c || w?.is_windows !== false) return [];
      const out = [];
      if (c.docker_sock === 'missing') out.push({ level: 'block', text: this.t('wrk_cap_lx_no_sock') });
      else if (c.docker_sock === 'stale') out.push({ level: 'block', text: this.t(['always', 'unless-stopped', 'on-failure'].includes(c.restart_policy) ? 'wrk_cap_lx_sock_stale' : 'wrk_cap_lx_sock_stale_manual') });
      if (c.restart_policy === 'no') out.push({ level: 'info', text: this.t('wrk_cap_lx_no_restart') });
      if (c.cpus_host && c.cpus_allowed && c.cpus_allowed < c.cpus_host) out.push({ level: 'info', text: this.t('wrk_cap_lx_cpuset', c.cpus_allowed, c.cpus_host) });
      return out;
    },
    getWorkerWinDraft(w) {
      if (!w || !w.worker_name) {
        return { autostart_mode: 'after_login', smb_server: '', smb_user: '', smb_pass: '', desktop_shortcut: true, start_menu_shortcut: true, tray_minimize: true, sleep_prevention_mode: 'encoding', skip_tray_prompt: false, show_log: false, wake_check: true };
      }
      if (!this.workerWinDrafts[w.worker_name]) {
        const ws = w.windows_settings || {};
        const s = this.state.settings || {};
        this.workerWinDrafts[w.worker_name] = {
          autostart_mode: ws.autostart_mode || 'after_login',
          smb_server: ws.smb_server || '',
          smb_user: ws.smb_user || s.smb_username || '',
          smb_pass: ws.smb_pass || s.smb_password || '',
          desktop_shortcut: ws.desktop_shortcut !== false,
          start_menu_shortcut: ws.start_menu_shortcut !== false,
          tray_minimize: ws.tray_minimize !== false,
          sleep_prevention_mode: ws.sleep_prevention_mode || 'encoding',
          skip_tray_prompt: Boolean(ws.skip_tray_prompt),
          show_log: Boolean(ws.show_log),
          wake_check: ws.wake_check !== false
        };
      }
      return this.workerWinDrafts[w.worker_name];
    },
    toggleWorkerWinSettings(w) {
      if (!w || !w.worker_name) return;
      const nextOpen = !this.expandedWinSettings[w.worker_name];
      if (nextOpen) {
        const ws = w.windows_settings || {};
        const s = this.state.settings || {};
        this.workerWinDrafts[w.worker_name] = {
          autostart_mode: ws.autostart_mode || 'after_login',
          smb_server: ws.smb_server || '',
          smb_user: ws.smb_user || s.smb_username || '',
          smb_pass: ws.smb_pass || s.smb_password || '',
          desktop_shortcut: ws.desktop_shortcut !== false,
          start_menu_shortcut: ws.start_menu_shortcut !== false,
          tray_minimize: ws.tray_minimize !== false,
          sleep_prevention_mode: ws.sleep_prevention_mode || 'encoding',
          skip_tray_prompt: Boolean(ws.skip_tray_prompt),
          show_log: Boolean(ws.show_log),
          wake_check: ws.wake_check !== false
        };
      }
      this.expandedWinSettings[w.worker_name] = nextOpen;
    },
    async saveWorkerWindowsSettings(w) {
      if (!w || !w.worker_name) return;
      const draft = this.getWorkerWinDraft(w);
      this.pausePollingUntil = Date.now() + 1500;
      const r = await this.apiFetch(`/api/v2/workers/${encodeURIComponent(w.worker_name)}/windows_settings`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          autostart_mode: draft.autostart_mode || 'after_login',
          smb_server: (draft.smb_server || '').trim(),
          smb_user: (draft.smb_user || '').trim(),
          smb_pass: draft.smb_pass || '',
          desktop_shortcut: Boolean(draft.desktop_shortcut),
          start_menu_shortcut: Boolean(draft.start_menu_shortcut),
          tray_minimize: Boolean(draft.tray_minimize),
          sleep_prevention_mode: draft.sleep_prevention_mode || 'encoding',
          skip_tray_prompt: Boolean(draft.skip_tray_prompt),
          show_log: Boolean(draft.show_log),
          wake_check: draft.wake_check !== false
        })
      });
      this.pausePollingUntil = 0;
      if (r.ok) {
        const d = await r.json().catch(() => ({}));
        if (d.windows_settings) {
          w.windows_settings = d.windows_settings;
        }
        this.toast(this.t('toast_win_settings_queued', w.worker_name));
        this.fetchState(true);
      }
    },
    async repairWorkerAutostartTask(w) {
      if (!w || !w.worker_name || !w.is_online) return;
      const draft = this.getWorkerWinDraft(w);
      const ws = w.windows_settings || {};
      this.pausePollingUntil = Date.now() + 1500;
      const r = await this.apiFetch(`/api/v2/workers/${encodeURIComponent(w.worker_name)}/windows_settings`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          autostart_mode: ws.autostart_mode || draft.autostart_mode || 'before_login',
          smb_server: (draft.smb_server || ws.smb_server || '').trim(),
          smb_user: (draft.smb_user || ws.smb_user || '').trim(),
          smb_pass: draft.smb_pass || ws.smb_pass || '',
          desktop_shortcut: draft.desktop_shortcut !== false,
          start_menu_shortcut: draft.start_menu_shortcut !== false,
          tray_minimize: draft.tray_minimize !== false,
          sleep_prevention_mode: draft.sleep_prevention_mode || ws.sleep_prevention_mode || 'encoding',
          skip_tray_prompt: Boolean(draft.skip_tray_prompt),
          show_log: Boolean(draft.show_log),
          wake_check: draft.wake_check !== false,
          force_task_repair: true
        })
      });
      this.pausePollingUntil = 0;
      if (r.ok) {
        const d = await r.json().catch(() => ({}));
        if (d.windows_settings) {
          w.windows_settings = d.windows_settings;
        }
        this.toast(this.t('toast_win_task_repair_sent', w.worker_name));
        this.fetchState(true);
      }
    },
    triggerHistoryDelete(jid) {
      if (this.requireTwoClickConfirm && !this.confirmingHistoryDelMap[jid]) {
        this.confirmingHistoryDelMap = { ...this.confirmingHistoryDelMap, [jid]: true };
        if (this._histDelTimers[jid]) clearTimeout(this._histDelTimers[jid]);
        this._histDelTimers[jid] = setTimeout(() => {
          const next = { ...this.confirmingHistoryDelMap };
          delete next[jid];
          this.confirmingHistoryDelMap = next;
        }, 3500);
        return;
      }
      const next = { ...this.confirmingHistoryDelMap };
      delete next[jid];
      this.confirmingHistoryDelMap = next;
      if (this._histDelTimers[jid]) { clearTimeout(this._histDelTimers[jid]); delete this._histDelTimers[jid]; }
      return this.historyJobAction(jid, 'delete');
    },
    async triggerDeleteSelectedHistory() {
      if (!this.selectedHistoryIds.length) return;
      if (this.requireTwoClickConfirm && !this.confirmingHistoryDelSel) {
        this.confirmingHistoryDelSel = true;
        if (this._histDelSelTimer) clearTimeout(this._histDelSelTimer);
        this._histDelSelTimer = setTimeout(() => { this.confirmingHistoryDelSel = false; }, 4000);
        return;
      }
      this.confirmingHistoryDelSel = false;
      if (this._histDelSelTimer) { clearTimeout(this._histDelSelTimer); this._histDelSelTimer = null; }
      return this.deleteSelectedHistory();
    },
    async historyJobAction(jid, act) {
      const r = await this.apiFetch('/api/v2/jobs/action', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ job_ids: [jid], action: act }) });
      if (!r || !r.ok) {
        this.toast(this.t('toast_action_failed'), 'error');
        return;
      }
      this.fetchHistory(); this.fetchState(true);
      if (act === 'requeue') {
        this.toast(this.t('toast_job_requeued', jid));
      }
    },
    toggleAllHistory(ev) { this.selectedHistoryIds = ev.target.checked ? this.paginatedHistory.map(h => h.id) : []; },
    async requeueSelectedHistory() {
      if (!this.selectedHistoryIds.length) return;
      const r = await this.apiFetch('/api/v2/jobs/action', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ job_ids: this.selectedHistoryIds, action: 'requeue' }) });
      if (!r || !r.ok) {
        this.toast(this.t('toast_action_failed'), 'error');
        return;
      }
      this.toast(this.t('toast_requeued_many_fmt', this.selectedHistoryIds.length));
      this.selectedHistoryIds = []; this.fetchHistory(); this.fetchState(true);
    },
    async deleteSelectedHistory() {
      if (!this.selectedHistoryIds.length) return;
      const r = await this.apiFetch('/api/v2/jobs/action', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ job_ids: this.selectedHistoryIds, action: 'delete' }) });
      if (!r || !r.ok) {
        this.toast(this.t('toast_action_failed'), 'error');
        return;
      }
      this.selectedHistoryIds = []; this.fetchHistory(); this.fetchState(true);
    },
    async uploadQueueFile(ev) {
      const files = Array.from(ev.target.files || []);
      if (!files.length) return;
      await this.importQueueFiles(files);
      ev.target.value = '';
    },
    copyPath(txt, lbl) {
      if (!txt) return;
      if (navigator.clipboard && window.isSecureContext) navigator.clipboard.writeText(txt);
      else {
        const el = document.createElement('textarea'); el.value = txt; el.style.position = 'fixed'; el.style.opacity = '0';
        document.body.appendChild(el); el.select(); document.execCommand('copy'); document.body.removeChild(el);
      }
      this.toast(this.t('toast_copied_fmt', lbl, txt));
    },
    copyWithFeedback(txt, key, lbl) {
      if (!txt) return;
      this.copyPath(txt, lbl);
      this.copiedKey = key;
      if (this._copiedTimer) clearTimeout(this._copiedTimer);
      this._copiedTimer = setTimeout(() => {
        if (this.copiedKey === key) this.copiedKey = '';
      }, 2200);
    },
    formatBytes(b) {
      const n = Number(b || 0);
      if (n <= 0) return '0.0 GB';
      const tb = n / Math.pow(1024, 4);
      return tb >= 1.0 ? tb.toFixed(2) + ' TB' : (n / Math.pow(1024, 3)).toFixed(1) + ' GB';
    },
    formatSecondsHM(sec) {
      const s = Math.max(0, Math.round(Number(sec || 0)));
      const h = Math.floor(s / 3600); const m = Math.floor((s % 3600) / 60);
      if (s > 0 && h === 0 && m === 0) return '01m';
      return h > 0 ? `${String(h).padStart(2, '0')}h ${String(m).padStart(2, '0')}m` : `${String(m).padStart(2, '0')}m`;
    },
    formatSecondsHMS(sec) {
      const s = Math.max(0, Math.round(Number(sec || 0)));
      const h = Math.floor(s / 3600); const m = Math.floor((s % 3600) / 60); const r = s % 60;
      return `${String(h).padStart(2, '0')}h${String(m).padStart(2, '0')}m${String(r).padStart(2, '0')}s`;
    },
    dupTip(job) {
      // vysvětlení štítku „Možná duplicita“ (shoda zdroje + parametrů s dokončenou úlohou / jinou úlohou ve frontě)
      const parts = [];
      const h = job.dup_done;
      if (h) {
        const d = h.completed_at ? new Date(h.completed_at) : null;
        const when = d && !isNaN(d.getTime()) ? d.toLocaleString(this.uiLang === 'cs' ? 'cs-CZ' : 'en-US', { day: 'numeric', month: 'numeric', year: 'numeric', hour: '2-digit', minute: '2-digit' }) : '?';
        parts.push(this.t('dup_tip_done', h.id, when, h.worker || '?', h.dest || '?'));
      }
      if (job.dup_queue && job.dup_queue.length) parts.push(this.t('dup_tip_queue', job.dup_queue.map(x => 'ID:' + x).join(', ')));
      parts.push(this.t('dup_tip_hint'));
      return parts.join('\n\n');
    },
    formatDateTime(iso) {
      if (!iso) return '-';
      const d = new Date(iso);
      return isNaN(d.getTime()) ? iso : d.toLocaleString(this.uiLang === 'cs' ? 'cs-CZ' : 'en-US', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' });
    },
    savingsBadgeText(srcV, outV) {
      if (!srcV || srcV <= 0) return '';
      const diff = 100.0 - ((outV / srcV) * 100.0);
      return diff >= 0 ? `-${diff.toFixed(1)} %` : `+${Math.abs(diff).toFixed(1)} %`;
    },
    linuxWorkerSlug(name) {
      return String(name || 'Linux-Worker-1').trim().toLowerCase().replace(/[^a-z0-9_-]/g, '-') || 'linux-worker-1';
    },
    linuxAutoDataDir(name) {
      return `/opt/hb_worker/${this.linuxWorkerSlug(name)}`;
    },
    linuxWorkerTempDir(dir, name) {
      // every worker needs its own Temp: an idle worker removes job folders it does not know (also those of another worker)
      const base = String(dir || '').trim().replace(/\/+$/, '') || '/mnt/nvme/hb_temp';
      const slug = this.linuxWorkerSlug(name);
      return base.split('/').pop() === slug ? base : `${base}/${slug}`;
    },
    isLinuxAutoDataDir(dir, name) {
      const d = String(dir || '').trim();
      return !d || d === '/opt/hb_worker/data' || d === this.linuxAutoDataDir(name);
    },
    setLinuxDeployMode(mode) {
      const m = this.addWorkerModal;
      m.linDeployMode = ['portainer', 'compose', 'cli'].includes(mode) ? mode : 'portainer';
      const cur = (m.linDataDir || '').trim();
      const auto = this.isLinuxAutoDataDir(cur, m.linWorkerName);
      if (m.linDeployMode === 'compose') {
        if (auto) m.linDataDir = './data';
      } else if (auto || cur === './data' || cur === '.') {
        m.linDataDir = this.linuxAutoDataDir(m.linWorkerName);
      }
    },
    openAddWorkerModal() {
      const s = this.state.settings || {};
      this.ensureDlToken();
      this.addWorkerModal.managerUrl = window.location.origin;
      this.addWorkerModal.winInputsDir = s.win_default_inputs_dir || '';
      this.addWorkerModal.winOutputsDir = s.win_default_outputs_dir || '';
      this.addWorkerModal.winUseCustomTemp = false;
      this.addWorkerModal.smbUsername = s.smb_username || '';
      this.addWorkerModal.smbPassword = s.smb_password || '';
      this.addWorkerModal.smbTestResult = null;
      this.addWorkerModal.linDeployMode = ['portainer', 'compose', 'cli'].includes(this.addWorkerModal.linDeployMode) ? this.addWorkerModal.linDeployMode : 'portainer';
      if (this.addWorkerModal.linDeployMode === 'compose') {
        this.addWorkerModal.linDataDir = this.isLinuxAutoDataDir(this.addWorkerModal.linDataDir, this.addWorkerModal.linWorkerName) ? './data' : this.addWorkerModal.linDataDir;
      } else {
        const curD = (this.addWorkerModal.linDataDir || '').trim();
        this.addWorkerModal.linDataDir = (this.isLinuxAutoDataDir(curD, this.addWorkerModal.linWorkerName) || curD === './data')
          ? this.linuxAutoDataDir(this.addWorkerModal.linWorkerName) : curD;
      }
      this.addWorkerModal.linInputsDir = s.linux_default_inputs_dir || '/mnt/nas/inputs';
      this.addWorkerModal.linOutputsDir = s.linux_default_outputs_dir || '/mnt/nas/outputs';
      this.addWorkerModal.linUseCustomTemp = false;
      this.addWorkerModal.linTempDir = s.linux_default_temp_dir || '/mnt/nvme/hb_temp';
      this.addWorkerModal.open = true;
      this.pushModalHistory();
    },
    async testAndSaveSmbPaths(mode = 'windows') {
      const m = this.addWorkerModal;
      m.isTestingSmb = true;
      m.smbTestResult = null;
      try {
        const payload = {
          win_default_inputs_dir: (m.winInputsDir || '').trim(),
          win_default_outputs_dir: (m.winOutputsDir || '').trim(),
          smb_username: (m.smbUsername || '').trim(),
          smb_password: m.smbPassword || '',
          linux_default_inputs_dir: (m.linInputsDir || '').trim(),
          linux_default_outputs_dir: (m.linOutputsDir || '').trim(),
          linux_default_temp_dir: (m.linTempDir || '').trim()
        };
        const r = await this.apiFetch('/api/v2/settings/test_smb_and_save', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload)
        });
        if (r.ok) {
          const d = await r.json().catch(() => ({}));
          const inRes = mode === 'linux' ? d.lin_in : d.win_in;
          const outRes = mode === 'linux' ? d.lin_out : d.win_out;
          const allOk = Boolean((!inRes || inRes.ok) && (!outRes || outRes.ok));
          const icon = (x) => (!x ? '•' : (x.skipped ? 'ℹ' : (x.ok ? '✓' : '✗')));
          m.smbTestResult = {
            ok: allOk,
            msg: `${icon(inRes)} Inputs: ${inRes?.detail || '-'}  |  ${icon(outRes)} Outputs: ${outRes?.detail || '-'}`
          };
          this.toast(allOk ? this.t('toast_smb_test_ok') : this.t('toast_smb_test_fail'), allOk ? 'info' : 'warn');
          this.fetchState(true);
        } else {
          m.smbTestResult = { ok: false, msg: this.t('toast_mgr_unreachable') };
        }
      } catch (e) {
        m.smbTestResult = { ok: false, msg: String(e) };
      } finally {
        m.isTestingSmb = false;
      }
    },
    async openFolderBrowser(targetField, initialPath = '/') {
      this.folderBrowser.targetField = targetField;
      this.folderBrowser.open = true;
      this.pushModalHistory();
      await this.browseFolderPath(initialPath || '/');
    },
    async browseFolderPath(p) {
      try {
        const r = await this.apiFetch(`/api/v2/fs/browse?path=${encodeURIComponent(p || '/')}`);
        if (r.ok) {
          const d = await r.json();
          this.folderBrowser.currentPath = d.current_path || '/';
          this.folderBrowser.parentPath = d.parent_path || '';
          this.folderBrowser.entries = d.entries || [];
        }
      } catch (e) {}
    },
    confirmFolderBrowserSelection() {
      const f = this.folderBrowser.targetField;
      if (f && this.folderBrowser.currentPath) {
        this.addWorkerModal[f] = this.folderBrowser.currentPath;
      }
      this.folderBrowser.open = false;
    },
    isValidWorkerName(v) {
      const s = String(v || '').trim();
      return /^[A-Za-z0-9._-]{1,48}$/.test(s);
    },
    normalizeWorkerName(v) {
      // diakritika -> bez ní (é->e, ř->r), mezery -> '-', ostatní nepovolené znaky pryč (stejně jako wc.normalize_worker_name)
      const extra = { 'ß': 'ss', 'ø': 'o', 'Ø': 'O', 'ł': 'l', 'Ł': 'L', 'đ': 'd', 'Đ': 'D', 'æ': 'ae', 'Æ': 'AE', 'œ': 'oe', 'Œ': 'OE', 'þ': 'th', 'Þ': 'Th' };
      let s = String(v || '').normalize('NFKD').replace(/[\u0300-\u036f]/g, '');
      s = s.replace(/[ßøØłŁđĐæÆœŒþÞ]/g, c => extra[c] || '');
      s = s.replace(/\s+/g, '-').replace(/[^A-Za-z0-9._-]/g, '');
      return s.slice(0, 48);
    },
    async downloadWindowsWorkerZip() {
      const m = this.addWorkerModal;
      if (!this.isValidWorkerName(m.workerName)) {
        this.toast(this.t('add_wrk_name_invalid'), 'error');
        return;
      }
      await this.ensureDlToken();
      const payload = {
        worker_name: (m.workerName || 'Win-Worker-1').trim(),
        manager_url: (m.managerUrl || window.location.origin).trim(),
        inputs_dir: (m.winInputsDir || '').trim(),
        outputs_dir: (m.winOutputsDir || '').trim(),
        temp_dir: m.winUseCustomTemp ? (m.winTempDir || '').trim() : '',
        smb_username: (m.smbUsername || '').trim(),
        smb_password: m.smbPassword || '',
        autostart_mode: m.winAutostartMode || 'after_login',
        lang: this.uiLang || 'en',
        desktop_shortcut: m.winDesktopShortcut !== false,
        start_menu_shortcut: m.winStartMenuShortcut !== false,
        tray_minimize: m.winTray !== false,
        sleep_mode: m.winSleepMode || 'encoding',
        wake_check: m.winWakeCheck !== false,
        include_binaries: !!m.includeBinaries,
        dl: m.dlToken || ''
      };
      try {
        const r = await this.apiFetch('/api/v2/workers/package/windows', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload)
        });
        if (!r.ok) {
          this.toast(this.t('toast_mgr_unreachable'), 'error');
          return;
        }
        const blob = await r.blob();
        const cd = r.headers.get('content-disposition') || '';
        const match = /filename="([^"]+)"/.exec(cd);
        const fn = match ? match[1] : `HBWorker_${(payload.worker_name || 'Win-Worker').replace(/[^a-zA-Z0-9_-]/g, '_')}.zip`;
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = fn;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        setTimeout(() => URL.revokeObjectURL(url), 10000);
      } catch (e) {
        this.toast(this.t('toast_mgr_unreachable'), 'error');
      }
    },
    downloadLinuxComposeFile() {
      if (!this.isValidWorkerName(this.addWorkerModal.linWorkerName)) {
        this.toast(this.t('add_wrk_name_invalid'), 'error');
        return;
      }
      const yaml = this.generatedLinuxComposeYaml;
      const blob = new Blob([yaml], { type: 'text/yaml;charset=utf-8' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `docker-compose-${(this.addWorkerModal.linWorkerName || 'worker').toLowerCase()}.yml`;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
    },
    showMetricPopover(ev, job, kind = 'video') {
      if (!job || !ev || !ev.currentTarget) return;
      const rect = ev.currentTarget.getBoundingClientRect();
      const rows = [];
      let title = '';
      if (kind === 'video') {
        const dur = Number(job.video_duration_sec || 0);
        const inKbps = Number(job.v_bitrate_in_kbps || 0);
        const outKbps = Number(job.v_bitrate_out_kbps || 0);
        if (dur <= 0 || !this.hasValidInputBitrate(job)) return;
        title = this.t('pop_video_hdr');
        const inBytes = Math.round((inKbps * 1000 * dur) / 8);
        rows.push({
          label: this.t('pop_orig_video'),
          val: `${this.formatBytes(inBytes)} (${this.formatBitrate(inKbps)})`,
          cls: 'text-sky-300 font-bold'
        });
        if (outKbps > 0) {
          const outBytes = Math.round((outKbps * 1000 * dur) / 8);
          const diffSign = outBytes <= inBytes ? '-' : '+';
          const diffStr = `${diffSign}${this.formatBytes(Math.abs(inBytes - outBytes))} (${this.savingsBadgeText(inKbps, outKbps)})`;
          rows.push({
            label: this.t('pop_new_video'),
            val: `${this.formatBytes(outBytes)} (${this.formatBitrate(outKbps)})`,
            cls: 'text-emerald-300 font-bold'
          });
          rows.push({
            label: this.t('pop_video_diff'),
            val: diffStr,
            cls: outBytes <= inBytes ? 'text-emerald-400 font-black' : 'text-rose-400 font-black'
          });
        }
      } else {
        const srcB = Number(job.src_size_bytes || 0);
        const outB = Number(job.out_size_bytes || 0);
        if (srcB <= 104857600 || outB <= 0) return;
        title = this.t('pop_file_hdr');
        const diffSign = outB <= srcB ? '-' : '+';
        const diffStr = `${diffSign}${this.formatBytes(Math.abs(srcB - outB))} (${this.savingsBadgeText(srcB, outB)})`;
        rows.push({
          label: this.t('pop_orig_file'),
          val: this.formatBytes(srcB),
          cls: 'text-slate-200 font-bold'
        });
        rows.push({
          label: this.t('pop_new_file'),
          val: this.formatBytes(outB),
          cls: 'text-emerald-300 font-bold'
        });
        rows.push({
          label: this.t('pop_file_diff'),
          val: diffStr,
          cls: outB <= srcB ? 'text-emerald-400 font-black' : 'text-rose-400 font-black'
        });
      }
      const popW = 310;
      const popH = rows.length > 2 ? 128 : 88;
      let left = rect.left + (rect.width / 2) - (popW / 2);
      left = Math.max(12, Math.min((window.innerWidth || 1280) - popW - 12, left));
      let top = rect.top - popH - 10;
      if (top < 12) {
        top = rect.bottom + 10;
      }
      this.metricPopover = {
        visible: true,
        x: Math.round(left),
        y: Math.round(top),
        kind,
        title,
        rows
      };
    },
    hideMetricPopover() {
      if (this.metricPopover && this.metricPopover.visible) {
        this.metricPopover.visible = false;
      }
    },
    formatNetFallbackReason(job) {
      const raw = String(job?.net_fallback_reason || '').trim();
      if (!raw) return this.t('direct_net_generic_tip');
      try {
        const d = JSON.parse(raw);
        if (d && d.kind === 'staging_space') {
          return this.t('direct_net_staging_tip', d.free_gb ?? '?', d.src_gb ?? '?', d.req_gb ?? '?');
        }
        if (d && d.kind === 'direct_outputs_space') {
          return this.t('direct_net_outputs_tip', d.free_gb ?? '?', d.req_gb ?? '?');
        }
      } catch (_) {}
      return raw;
    },
    showNetFallbackTip(ev, job) {
      const el = ev?.currentTarget;
      if (!el) return;
      const rect = el.getBoundingClientRect();
      const popW = 320;
      const popH = 92;
      let left = rect.left + (rect.width / 2) - (popW / 2);
      left = Math.max(12, Math.min((window.innerWidth || 1280) - popW - 12, left));
      let top = rect.top - popH - 10;
      if (top < 12) top = rect.bottom + 10;
      this.netFallbackTip = {
        visible: true,
        x: Math.round(left),
        y: Math.round(top),
        text: this.formatNetFallbackReason(job)
      };
    },
    hideNetFallbackTip() {
      if (this.netFallbackTip && this.netFallbackTip.visible) {
        this.netFallbackTip.visible = false;
      }
    },
    hideAllFloatingTips() {
      this.hideWorkerBubbleTip();
      this.hideMetricPopover();
      this.hideNetFallbackTip();
    },
    translateSubsStatus(j) {
      if (j.subs_status === 'FORCED_OK') {
        return this.t('subs_forced_extracted_fmt', j.subs_removed || 1);
      }
      if (j.subs_status === 'CLEANED') {
        return this.t('subs_cleaned_fmt', j.subs_removed || 0);
      }
      if (j.subs_status === 'OK') return this.t('subs_status_ok');
      if (j.subs_status === 'ERROR' || j.subs_status === 'ANALYZE_ERR') return this.t('subs_status_err');
      return '';
    },
    translateError(j) {
      if (j.error_code === 'ERR_SOURCE_MISSING') return this.t('err_source_missing');
      if (j.error_code === 'ERR_NAS_UNREACHABLE') return this.t('err_nas_unreachable_fmt', j.error_detail || '');
      if (j.error_code === 'ERR_NETWORK_LOST') return this.t('err_network_lost_fmt', j.error_detail || '');
      if (j.error_code === 'ERR_LOST_CONNECTION') return this.t('err_lost_connection');
      if (j.error_code === 'ERR_ZOMBIE_TIMEOUT') return this.t('err_zombie_timeout_fmt', j.error_detail || 'Timeout');
      if (j.error_code === 'ERR_HELD_OUTPUT_LOST') return this.t('err_held_output_lost', j.error_detail || j.worker_name || 'worker');
      if (j.error_code === 'ERR_HB_FAILED') return j.error_detail ? `${this.t('err_failed_prefix')}: ${j.error_detail}` : `HandBrakeCLI failed (${j.attempt_count}/3)`;
      return '';
    },
    statusLabel(st) {
      if (st === 'WAITING') return this.t('waiting_lbl');
      if (st === 'WAITING_DISK') return this.t('waiting_disk_lbl');
      if (st === 'IN_PROGRESS') return this.t('encoding_lbl');
      if (st === 'STAGING') return this.t('staging_lbl');
      if (st === 'PREMUX') return 'Pre-Mux';
      if (st === 'COPYING') return this.t('saving_lbl');
      if (st === 'PAUSED') return this.t('paused_lbl');
      if (st === 'ERROR') return this.t('errors_lbl');
      if (st === 'CANCELLED') return this.t('cancelled_lbl');
      return st;
    },
    statusBadgeClass(st) {
      const m = {
        WAITING: 'bg-transparent text-sky-300 font-bold border border-sky-500',
        WAITING_DISK: 'bg-amber-700 text-amber-100 font-black border border-amber-500 hb-sync-pulse',
        STAGING: 'bg-purple-700 text-white font-bold',
        PREMUX: 'bg-teal-700 text-white font-bold',
        IN_PROGRESS: 'bg-emerald-700 text-white font-bold',
        COPYING: 'bg-cyan-700 text-white font-bold',
        PAUSED: 'bg-amber-900/80 text-amber-100 font-bold border border-amber-500',
        ERROR: 'bg-rose-600 text-white font-black border border-rose-400 shadow-sm shadow-rose-950',
        CANCELLED: 'bg-slate-700 text-slate-300 font-bold'
      };
      return m[st] || 'bg-slate-700 text-white font-bold';
    },
    ramPct(t) { return (!t || !t.ram_total_gb) ? 0 : Math.min(100, Math.round((t.ram_used_gb / t.ram_total_gb) * 100)); },
    ramHbPct(t) { return (!t || !t.ram_total_gb) ? 0 : Math.min(100, ((t.ram_hb_gb || 0) / t.ram_total_gb) * 100).toFixed(1); },
    workerCpuSourcePref() {
      const s = this.state?.settings?.worker_cpu_source || this.state?.settings?.telemetry_source;
      return (s === 'system' || s === 'hb' || s === 'both') ? s : 'both';
    },
    workerRamSourcePref() {
      const s = this.state?.settings?.worker_ram_source || this.state?.settings?.telemetry_source;
      return (s === 'system' || s === 'hb' || s === 'both') ? s : 'both';
    },
    ramFormatPref() {
      const f = this.state?.settings?.ram_format;
      return (f === 'gb_ratio' || f === 'gb_pct' || f === 'pct_gb' || f === 'pct_only') ? f : 'gb_ratio';
    },
    formatSysRam(t, decimals = 1) {
      const used = Number(t?.ram_used_gb || 0).toFixed(decimals);
      const tot = Number(t?.ram_total_gb || 0).toFixed(decimals);
      const pct = this.ramPct(t);
      const fmt = this.ramFormatPref();
      const ratio = `${used}/${tot} GB`;
      if (fmt === 'gb_pct') return `${ratio} (${pct}%)`;
      if (fmt === 'pct_gb') return `${pct}% (${ratio})`;
      if (fmt === 'pct_only') return `${pct}%`;
      return ratio;
    },
    formatWorkerBubbleCpu(w) {
      const t = w?.telemetry || {};
      const src = this.workerCpuSourcePref();
      const cpuSys = Math.round(t.cpu_total || 0);
      const cpuHb = Math.round(t.cpu_hb || 0);
      if (src === 'hb') return `HB CPU: ${cpuHb}%`;
      if (src === 'system') return `CPU: ${cpuSys}%`;
      return `CPU: ${cpuSys}% (HB ${cpuHb}%)`;
    },
    formatWorkerBubbleRam(w) {
      const t = w?.telemetry || {};
      const src = this.workerRamSourcePref();
      const hbRam = Number(t.ram_hb_gb || 0).toFixed(1);
      if (src === 'hb') return `HB RAM: ${hbRam} GB`;
      const sysRam = this.formatSysRam(t, 1);
      if (src === 'system') return `RAM: ${sysRam}`;
      return `RAM: ${sysRam} (HB ${hbRam} GB)`;
    },
    formatHeaderClusterCpu() {
      const cs = this.clusterStats;
      const src = this.workerCpuSourcePref();
      if (src === 'hb') return `HB CPU ${Math.round(cs.weightedHbCpu)}%`;
      if (src === 'system') return `CPU ${Math.round(cs.weightedCpu)}%`;
      return `CPU ${Math.round(cs.weightedCpu)}% (HB ${Math.round(cs.weightedHbCpu)}%)`;
    },
  },
  mounted() {
    this.initHbTooltips();
    document.documentElement.setAttribute('lang', this.uiLang);
    this.updateDynamicFavicon();
    this.checkHeaderWorkersOverflow();
    this.syncPulseAnimations();
    this.fetchHistory();
    this.securityRefreshSession();
    this.fetchState(true).then(() => {
      if (this.activeTab === 'settings' && this.state?.settingsSection === 'security') {
        this.securityOpen();
      }
      this.$nextTick(() => { this.initSortable(); this.checkHeaderWorkersOverflow(); this.syncPulseAnimations(); });
    });
    this.checkGhcrUpdate(false, true);
    if (typeof ResizeObserver !== 'undefined' && this.$refs.headerTopRow) {
      this.resizeObserver = new ResizeObserver(() => { this.checkHeaderWorkersOverflow(); });
      this.resizeObserver.observe(this.$refs.headerTopRow);
      if (this.$refs.headerLogoBox) this.resizeObserver.observe(this.$refs.headerLogoBox);
    }
    window.addEventListener('resize', this.checkHeaderWorkersOverflow);
    window.addEventListener('keydown', this.onGlobalKeyDown);
    window.addEventListener('popstate', this.onPopState);
    const initNav = this.parseNavHash();
    if (initNav) {
      this._applyingPopState = true;
      try {
        if (initNav.tab === 'settings') {
          this.switchTab('settings', initNav.mobileSetList ? null : initNav.section);
          this.mobileSetList = Boolean(initNav.mobileSetList);
        } else if (initNav.tab !== 'queue') {
          this.switchTab(initNav.tab);
        }
      } finally {
        this._applyingPopState = false;
      }
    }
    this.pushNavHistory(true);
    window.addEventListener('scroll', this.hideAllFloatingTips, true);
    window.addEventListener('dragenter', this.onWindowDragEnter, false);
    window.addEventListener('dragover', this.onWindowDragOver, false);
    window.addEventListener('dragleave', this.onWindowDragLeave, false);
    window.addEventListener('drop', this.onWindowDrop, false);
    this.pollTimer = setInterval(() => this.fetchState(false), 2000);
    this._onVisibility = () => { if (!document.hidden) this.fetchState(true); };
    document.addEventListener('visibilitychange', this._onVisibility);
    this.ghcrTimer = setInterval(() => this.checkGhcrUpdate(false, true), 5 * 60 * 1000);
    this.kofiTimer = setInterval(() => { this.kofiHour = new Date().getHours(); }, 60000);
    this.idlePanelTimer = setInterval(() => this.closeIdleWorkerPanels(), 10000);
  },
  beforeUnmount() {
    if (this.pollTimer) clearInterval(this.pollTimer);
    if (this._onVisibility) document.removeEventListener('visibilitychange', this._onVisibility);
    if (this.ghcrTimer) clearInterval(this.ghcrTimer);
    if (this.kofiTimer) clearInterval(this.kofiTimer);
    if (this.idlePanelTimer) clearInterval(this.idlePanelTimer);
    if (this._highlightWorkerTimer) clearTimeout(this._highlightWorkerTimer);
    if (this._delStatsTimer) clearTimeout(this._delStatsTimer);
    if (this._copiedTimer) clearTimeout(this._copiedTimer);
    if (this._bulkConfirmTimer) clearTimeout(this._bulkConfirmTimer);
    if (this._histDelSelTimer) clearTimeout(this._histDelSelTimer);
    if (this._forceReinstallTimer) clearTimeout(this._forceReinstallTimer);
    if (this._clearSmbTimer) clearTimeout(this._clearSmbTimer);
    Object.values(this._histDelTimers || {}).forEach(tid => clearTimeout(tid));
    if (this.resizeObserver) this.resizeObserver.disconnect();
    window.removeEventListener('resize', this.checkHeaderWorkersOverflow);
    window.removeEventListener('keydown', this.onGlobalKeyDown);
    window.removeEventListener('popstate', this.onPopState);
    window.removeEventListener('scroll', this.hideAllFloatingTips, true);
    window.removeEventListener('dragenter', this.onWindowDragEnter, false);
    window.removeEventListener('dragover', this.onWindowDragOver, false);
    window.removeEventListener('dragleave', this.onWindowDragLeave, false);
    window.removeEventListener('drop', this.onWindowDrop, false);
  }
}).mount('#app');

// ==============================================================================
// POPISKY TLAČÍTEK „T / L“ – jedna sdílená funkce pro celou aplikaci (vybráno autorem 2026-11-18 po testu variant
// A / B / C / D; ostatní varianty odstraněny). V kontejneru data-hb-pop="start|end" se po krátkém podržení myši
// nad tlačítkem (nebo pod ním v hlavičce) vysune popisek spojený s tlačítkem jedním obrysem:
//  - tlačítka v řadě se nikdy nehýbou, kreslí se SVG v <body> (position: fixed, pointer-events: none) → myš
//    i kliknutí vždy patří tlačítku, které je fyzicky pod ní,
//  - popisek je vycentrovaný (tvar T), nepřesáhne začátek skupiny (start = levý okraj prvního tlačítka,
//    end = pravý okraj posledního) – u kraje má tvar L; delší přesáhne jen na volnou stranu, nikdy mimo okno,
//  - barva výplně, obrysu, textu i průhlednost (pulz potvrzení) průběžně sledují tlačítko,
//  - animace: vysunutí ~180 ms (nejdřív ven, pak do stran, text naposled), zavření stejnou cestou zpět,
//  - potvrzovací stav (.is-active / .is-confirm) drží popisek (text se obnovuje),
//  - dotyk / „omezit animace“ → vypnuto (platí obyčejné vysunutí textu z CSS s krátkou prodlevou).
// ==============================================================================
(function initHbPop() {
  const LABEL_SEL = '.hb-btn-expand, .bulk-stop-expand, .kofi-hover-label';
  const OPEN_DELAY_MS = 150, NEXT_DELAY_MS = 50, CLOSE_DELAY_MS = 90;
  const SVGNS = 'http://www.w3.org/2000/svg';
  const T_R = 8, T_F = 8, T_CH = 26, T_PAD = 12, T_OPEN = 180, T_CLOSE = 130, T_EDGE = 6, T_GAP = 4;
  const root = document.documentElement;
  const mqMotion = window.matchMedia ? window.matchMedia('(prefers-reduced-motion: reduce)') : null;
  const mqMouse = window.matchMedia ? window.matchMedia('(hover: hover) and (pointer: fine)') : null;
  const layers = new Map();          // tlačítko -> stav popisku
  let lastTs = 0, openTimer = null;
  const enabled = () => !(mqMotion && mqMotion.matches) && (!mqMouse || mqMouse.matches);
  const labelOf = (g) => g.querySelector(LABEL_SEL);
  const isHeld = (g) => { const l = labelOf(g); return !!l && (l.classList.contains('is-active') || l.classList.contains('is-confirm')); };
  const tEase = (x) => 1 - Math.pow(1 - x, 3);
  const transparent = (col) => /rgba\(0, 0, 0, 0\)|transparent/.test(col);
  const parseCol = (cs) => { const m = String(cs || '').match(/rgba?\(([^)]+)\)/); if (!m) return null; const p = m[1].split(',').map(x => parseFloat(x)); return { r: p[0], g: p[1], b: p[2], a: p.length > 3 ? p[3] : 1 }; };
  // poloprůhledné pozadí „složené“ s pozadím předků na neprůhlednou barvu (bez světlých / tmavých švů na spojích)
  const solidBg = (el) => {
    const chain = [];
    for (let n = el; n && n.nodeType === 1; n = n.parentElement) { const c = parseCol(getComputedStyle(n).backgroundColor); if (c && c.a > 0) { chain.push(c); if (c.a >= 1) break; } }
    let acc = { r: 2, g: 6, b: 23 };   // slate-950 – pozadí stránky
    for (let i = chain.length - 1; i >= 0; i--) { const c = chain[i]; acc = { r: c.r * c.a + acc.r * (1 - c.a), g: c.g * c.a + acc.g * (1 - c.a), b: c.b * c.a + acc.b * (1 - c.a) }; }
    return `rgb(${Math.round(acc.r)}, ${Math.round(acc.g)}, ${Math.round(acc.b)})`;
  };

  function tPaths(bw, bh, eL, eR, ch, rb, gap) {
    // tvar „popisek nahoře“ (pro „dole“ se zrcadlí transformací); 0,0 = levý horní roh popisku
    // vrací [výplň popisku + přechodů + „krčku“ (zasahuje 1 px do tlačítka), obrys celého tvaru vč. boků a spodku tlačítka]
    // gap = krček mezi popiskem a tlačítkem → popisek nesedí přímo na sousedních tlačítkách, je mezi nimi malá mezera
    const bx = eL, cw = eL + bw + eR, H = ch + gap + bh, mn = Math.min(T_R, ch / 2);
    const fL = Math.max(0, Math.min(T_F, eL - mn)), fR = Math.max(0, Math.min(T_F, eR - mn));
    let fill = `M ${mn},0 H ${cw - mn} Q ${cw},0 ${cw},${mn}`;
    fill += eR > 0.5 ? ` V ${ch - mn} Q ${cw},${ch} ${cw - mn},${ch} H ${bx + bw + fR} Q ${bx + bw},${ch} ${bx + bw},${ch + fR} L ${bx + bw - 1},${ch + fR} V ${ch + gap + 1}` : ` V ${ch + gap + 1}`;
    fill += eL > 0.5 ? ` H ${bx + 1} V ${ch + fL} L ${bx},${ch + fL} Q ${bx},${ch} ${bx - fL},${ch} H ${mn} Q 0,${ch} 0,${ch - mn}` : ` H ${bx}`;
    fill += ` V ${mn} Q 0,0 ${mn},0 Z`;
    let line = `M ${mn},0 H ${cw - mn} Q ${cw},0 ${cw},${mn}`;
    line += eR > 0.5 ? ` V ${ch - mn} Q ${cw},${ch} ${cw - mn},${ch} H ${bx + bw + fR} Q ${bx + bw},${ch} ${bx + bw},${ch + fR}` : '';
    line += ` V ${H - rb} Q ${bx + bw},${H} ${bx + bw - rb},${H} H ${bx + rb} Q ${bx},${H} ${bx},${H - rb}`;
    line += eL > 0.5 ? ` V ${ch + fL} Q ${bx},${ch} ${bx - fL},${ch} H ${mn} Q 0,${ch} 0,${ch - mn}` : '';
    line += ` V ${mn} Q 0,0 ${mn},0 Z`;
    return [fill, line];
  }

  function build(g) {
    const box = g.closest('[data-hb-pop]');
    const l = labelOf(g);
    if (!box || !l) return null;
    const text = (l.textContent || '').trim();
    if (!text) return null;
    const end = box.getAttribute('data-hb-pop') === 'end';
    const gr = g.getBoundingClientRect();
    const down = !!g.closest('header') || gr.top < T_CH + T_GAP + 12;
    const gs = getComputedStyle(g);
    const bw = gr.width, bh = gr.height, bx0 = gr.left;
    const rb = Math.min(bh / 2, parseFloat(down ? gs.borderTopLeftRadius : gs.borderBottomLeftRadius) || 8);
    // hranice skupiny = od prvního po poslední tlačítko s popiskem (jinak celý kontejner)
    const btns = Array.from(box.querySelectorAll('.group')).filter(x => labelOf(x) && x.offsetParent !== null && !x.classList.contains('hb-pop-skip'));
    let gL, gR;
    if (btns.length > 1) { gL = Math.min(...btns.map(x => x.getBoundingClientRect().left)); gR = Math.max(...btns.map(x => x.getBoundingClientRect().right)); }
    else { const brc = box.getBoundingClientRect(); gL = brc.left; gR = brc.right; }
    const capEl = document.createElement('div');
    capEl.className = 'hb-t-cap';
    capEl.textContent = text;
    capEl.style.color = gs.color;
    document.body.appendChild(capEl);
    const tw = capEl.getBoundingClientRect().width;
    // krátký popisek aspoň tak široký, aby se po obou stranách vešel roh + zaoblený přechod → souměrné T
    const cw = Math.max(tw + 2 * T_PAD, bw + 2 * (T_F + T_R));
    let cx = bx0 + bw / 2 - cw / 2;
    if (end) cx = Math.min(cx, gR - cw); else cx = Math.max(cx, gL);
    cx = Math.max(T_EDGE, Math.min(window.innerWidth - T_EDGE - cw, cx));
    cx = Math.min(cx, bx0); if (cx + cw < bx0 + bw) cx = bx0 + bw - cw;
    let eL = Math.max(0, bx0 - cx), eR = Math.max(0, cx + cw - (bx0 + bw));
    if (eL > 0 && eL < T_F + T_R) { eR += eL; eL = 0; }      // malé přesahy → rovná hrana (L)
    if (eR > 0 && eR < T_F + T_R) { eL += eR; eR = 0; }

    // tlačítko po dobu popisku: bez vlastního rámečku, stínu a prolínání, rovné rohy na spojené straně (rozměry beze změny)
    const rk1 = down ? 'borderBottomLeftRadius' : 'borderTopLeftRadius', rk2 = down ? 'borderBottomRightRadius' : 'borderTopRightRadius';
    const saved = { bc: g.style.borderColor, r1: g.style[rk1], r2: g.style[rk2], tr: g.style.transition, bs: g.style.boxShadow };
    g.style.transition = 'none'; g.style.boxShadow = 'none'; g.style.borderColor = 'transparent';
    g.style[rk1] = '0'; g.style[rk2] = '0';
    const restore = () => { g.style.transition = saved.tr; g.style.boxShadow = saved.bs; g.style.borderColor = saved.bc; g.style[rk1] = saved.r1; g.style[rk2] = saved.r2; };

    const svg = document.createElementNS(SVGNS, 'svg');
    svg.setAttribute('class', 'hb-t-shape');
    svg.setAttribute('aria-hidden', 'true');
    const grp = document.createElementNS(SVGNS, 'g');
    const pf = document.createElementNS(SVGNS, 'path'), ps = document.createElementNS(SVGNS, 'path');
    pf.setAttribute('stroke', 'none'); ps.setAttribute('fill', 'none'); ps.setAttribute('stroke-width', '1');
    grp.appendChild(pf); grp.appendChild(ps); svg.appendChild(grp);
    document.body.appendChild(svg);

    // barvy tlačítka: rámeček čteme jen při prvním vykreslení nebo změně třídy tlačítka (žádný 60fps reflow)
    let lastCls = '', cachedStroke = '#334155', cachedHasAnim = false;
    const sync = (forceBorder) => {
      const cs = getComputedStyle(g);
      const f = transparent(cs.backgroundColor) ? solidBg(g.parentElement) : solidBg(g);
      const curCls = g.className || '';
      if (forceBorder || curCls !== lastCls) {
        lastCls = curCls;
        g.style.borderColor = saved.bc;
        const bcs = getComputedStyle(g);
        let s = parseFloat(bcs.borderTopWidth) > 0 && !transparent(bcs.borderTopColor) ? bcs.borderTopColor : (transparent(cs.backgroundColor) ? '#334155' : f);
        g.style.borderColor = 'transparent';
        const fc = parseCol(f), sc = parseCol(s);
        if (fc && sc && Math.abs(fc.r - sc.r) + Math.abs(fc.g - sc.g) + Math.abs(fc.b - sc.b) < 60) {
          s = `rgb(${Math.round(fc.r + (255 - fc.r) * 0.4)}, ${Math.round(fc.g + (255 - fc.g) * 0.4)}, ${Math.round(fc.b + (255 - fc.b) * 0.4)})`;
        }
        cachedStroke = s;
        cachedHasAnim = Boolean(cs.animationName && cs.animationName !== 'none');
      }
      if (pf.getAttribute('fill') !== f) pf.setAttribute('fill', f);
      if (ps.getAttribute('stroke') !== cachedStroke) ps.setAttribute('stroke', cachedStroke);
      if (capEl.style.color !== cs.color) capEl.style.color = cs.color;
      const op = parseFloat(cs.opacity);
      return isNaN(op) ? 1 : op;
    };

    const st = { svg, cap: capEl, text, raf: 0, closing: false, p: 0, sync: null };
    const draw = (p) => {
      const pv = tEase(Math.min(1, p / 0.45)), ph = tEase(Math.max(0, (p - 0.2) / 0.8));
      const gp = T_GAP * pv;
      const ch = 4 + (T_CH - 4) * pv, lx = eL * ph, rx = eR * ph, w = lx + bw + rx, H = ch + gp + bh;
      const [df, ds] = tPaths(bw, bh, lx, rx, ch, rb, gp);
      svg.setAttribute('width', w + 2); svg.setAttribute('height', H + 2); svg.setAttribute('viewBox', `-1 -1 ${w + 2} ${H + 2}`);
      grp.setAttribute('transform', down ? `translate(0 ${H}) scale(1 -1)` : '');
      svg.style.left = (bx0 - lx - 1) + 'px';
      svg.style.top = ((down ? gr.top : gr.top - ch - gp) - 1) + 'px';
      pf.setAttribute('d', df); ps.setAttribute('d', ds);
      capEl.style.left = (bx0 - eL + (eL + bw + eR - tw) / 2) + 'px';
      capEl.style.top = ((down ? gr.bottom + gp : gr.top - ch - gp) + (ch - capEl.offsetHeight) / 2) + 'px';
    };
    const finish = () => { if (st.raf) { cancelAnimationFrame(st.raf); st.raf = 0; } svg.remove(); capEl.remove(); restore(); };
    let t0 = performance.now(), pStart = 0, lastP = -1;
    const applyOpacity = (p, op) => {
      svg.style.opacity = op.toFixed(3);
      capEl.style.opacity = (Math.max(0, (p - 0.55) / 0.45) * op).toFixed(3);
    };
    st.sync = () => {
      if (!g.isConnected) { finish(); return; }
      applyOpacity(st.p, sync(false));
    };
    const step = (now) => {
      st.raf = 0;
      if (!g.isConnected) { finish(); return; }
      let p;
      if (st.closing) {
        p = Math.max(0, pStart * (1 - (now - t0) / T_CLOSE));
        if (p <= 0) { finish(); return; }
      } else {
        p = Math.min(1, (now - t0) / T_OPEN);
      }
      st.p = p;
      if (p !== lastP) { draw(p); lastP = p; }
      applyOpacity(p, sync(lastP === 0));
      // Po dokončení otevření zastavit 60fps smyčku u statických tlačítek (běží jen při animaci nebo pulzu)
      if (st.closing || p < 1 || cachedHasAnim) {
        st.raf = requestAnimationFrame(step);
      }
    };
    st.close = (immediate) => {
      if (immediate) { finish(); return; }
      if (st.closing) return;
      st.closing = true; pStart = st.p; t0 = performance.now();
      if (!st.raf) st.raf = requestAnimationFrame(step);
    };
    sync(true);
    st.raf = requestAnimationFrame(step);
    return st;
  }

  function show(g) {
    hide(g, true);
    let st = null;
    try { st = build(g); } catch (err) { console.warn('[hb-pop] popisek se nepodařilo vytvořit', err, g); }
    if (st) { layers.set(g, st); lastTs = Date.now(); }
  }
  function hide(g, immediate) {
    const st = layers.get(g);
    if (!st) return;
    layers.delete(g);
    st.close(immediate);          // zavření stejnou cestou zpět (barvy dál sledují tlačítko), pak úklid
  }
  const hideOthers = (keep) => layers.forEach((_, og) => { if (og !== keep && !isHeld(og)) hide(og); });
  const sync = () => { root.classList.toggle('hb-pop-on', enabled()); if (!root.classList.contains('hb-pop-on')) layers.forEach((_, g) => hide(g, true)); };
  sync();
  [mqMotion, mqMouse].forEach(mq => { try { mq && mq.addEventListener('change', sync); } catch (e) {} });

  document.addEventListener('pointerover', (e) => {
    if (e.pointerType !== 'mouse' || !root.classList.contains('hb-pop-on')) return;
    const g = e.target.closest ? e.target.closest('[data-hb-pop] .group') : null;
    if (!g || !labelOf(g) || g.classList.contains('hb-pop-skip')) return;
    clearTimeout(openTimer);
    const quick = layers.size > 0 || Date.now() - lastTs < 350;
    if (quick) hideOthers(g);                      // přejetí na souseda: starý popisek hned zajíždí
    openTimer = setTimeout(() => { hideOthers(g); if (g.matches(':hover') && !layers.has(g)) show(g); }, quick ? NEXT_DELAY_MS : OPEN_DELAY_MS);
  }, { passive: true });
  document.addEventListener('pointerout', (e) => {
    if (e.pointerType !== 'mouse') return;
    const g = e.target.closest ? e.target.closest('[data-hb-pop] .group') : null;
    if (!g || (e.relatedTarget && g.contains(e.relatedTarget))) return;
    clearTimeout(openTimer);
    setTimeout(() => { if (!g.matches(':hover') && !isHeld(g)) hide(g); }, CLOSE_DELAY_MS);
  }, { passive: true });
  // kliknutí (např. první klik na Stop → potvrzení): popisek hned obnovit s potvrzovacím textem
  document.addEventListener('click', (e) => {
    if (!root.classList.contains('hb-pop-on')) return;
    const g = e.target.closest ? e.target.closest('[data-hb-pop] .group') : null;
    if (!g || !labelOf(g) || g.classList.contains('hb-pop-skip')) return;
    clearTimeout(openTimer);
    setTimeout(() => { if (g.isConnected && (g.matches(':hover') || isHeld(g))) show(g); }, 40);
  }, true);
  // posun stránky / změna okna: popisky s position: fixed by se rozjely → zavřít (při dalším pohybu se ukážou znovu)
  window.addEventListener('scroll', (e) => {
    const t = e.target;
    layers.forEach((_, g) => { if (t === document || t === document.documentElement || (t && t.contains && t.contains(g))) hide(g, true); });
  }, { passive: true, capture: true });
  window.addEventListener('resize', () => layers.forEach((_, g) => hide(g, true)));
  // průběžně: potvrzení vzniklo bez myši, změnil se text, tlačítko zmizelo, potvrzení skončilo (na skryté záložce nic nedělá)
  setInterval(() => {
    if (document.hidden || !root.classList.contains('hb-pop-on')) return;
    document.querySelectorAll('[data-hb-pop] .hb-btn-expand.is-active, [data-hb-pop] .bulk-stop-expand.is-confirm').forEach(l => {
      const g = l.closest('.group');
      if (g && !layers.has(g)) show(g);
    });
    layers.forEach((st, g) => {
      if (!g.isConnected) { hide(g, true); return; }
      if (!g.matches(':hover') && !isHeld(g)) { hide(g); return; }
      const txt = (labelOf(g)?.textContent || '').trim();
      if (txt !== st.text) { show(g); return; }
      if (!st.raf && st.sync) st.sync();
    });
  }, 250);
})();
