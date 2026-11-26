// Tailwind CSS pro webové rozhraní HB Manageru (dřív se generovalo v prohlížeči z cdn.tailwindcss.com).
// Třídy se hledají v index.html a app.js – nové třídy proto pište vždy celé (ne skládané z kousků).
module.exports = {
  content: ['./index.html', './app.js'],
  darkMode: 'class',
  theme: {
    extend: {
      fontFamily: {
        sans: ['Inter', 'system-ui', 'sans-serif'],
        mono: ['JetBrains Mono', 'Consolas', 'monospace']
      }
    }
  }
};
