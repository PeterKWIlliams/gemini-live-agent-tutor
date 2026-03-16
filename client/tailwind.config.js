/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{js,jsx}'],
  theme: {
    extend: {
      colors: {
        sand: '#f7f2e8',
        ink: '#18222b',
        orange: '#f28c55',
        sage: '#67a46b',
        moss: '#40664c',
        blush: '#ffe1cf',
        mist: '#edf4ef',
      },
      fontFamily: {
        sans: ['"DM Sans"', 'sans-serif'],
        display: ['"Fraunces"', 'serif'],
      },
      boxShadow: {
        glow: '0 18px 45px rgba(24, 34, 43, 0.12)',
      },
      backgroundImage: {
        paper:
          'radial-gradient(circle at top left, rgba(255, 247, 238, 0.9), transparent 34%), radial-gradient(circle at bottom right, rgba(178, 225, 186, 0.28), transparent 28%)',
      },
    },
  },
  plugins: [],
};
