// Připraví static/vendor/ pro Manager: Vue, SortableJS a sestavený Tailwind CSS.
// Spouští se při sestavení Docker obrazu (Dockerfile.manager, fáze web-builder) – lze i ručně: npm ci && npm run build
const fs = require('fs');
const path = require('path');
const { execFileSync } = require('child_process');

const root = path.resolve(__dirname, '..');
const out = path.join(root, 'static', 'vendor');
fs.mkdirSync(out, { recursive: true });

const copy = (from, name) => {
  fs.copyFileSync(path.join(root, 'node_modules', from), path.join(out, name));
  console.log('vendor:', name);
};
copy('vue/dist/vue.global.prod.js', 'vue.global.prod.js');
copy('sortablejs/Sortable.min.js', 'Sortable.min.js');

execFileSync(process.execPath, [
  path.join(root, 'node_modules', 'tailwindcss', 'lib', 'cli.js'),
  '-c', path.join(root, 'tailwind.config.js'),
  '-i', path.join(root, 'web', 'tailwind.input.css'),
  '-o', path.join(out, 'hb-tailwind.css'),
  '--minify'
], { stdio: 'inherit', cwd: root });

for (const f of ['vue.global.prod.js', 'Sortable.min.js', 'hb-tailwind.css']) {
  const st = fs.statSync(path.join(out, f));
  if (st.size < 1000) throw new Error('vendor file too small: ' + f);
}
console.log('vendor: hotovo');
