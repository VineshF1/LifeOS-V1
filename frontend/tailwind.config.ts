import type { Config } from "tailwindcss";

const config: Config = {
  content: ["./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      keyframes: {
        "fade-in": {
          from: { opacity: "0", transform: "translateY(4px)" },
          to: { opacity: "1", transform: "translateY(0)" },
        },
        shimmer: {
          "0%": { transform: "translateX(-100%)" },
          "100%": { transform: "translateX(100%)" },
        },
        "border-beam": {
          "0%": { offsetDistance: "0%" },
          "100%": { offsetDistance: "100%" },
        },
        "grid-move": {
          "0%": { transform: "translate(0, 0)" },
          "100%": { transform: "translate(40px, 40px)" },
        },
      },
      animation: {
        "fade-in": "fade-in 180ms ease-out",
        shimmer: "shimmer 2s infinite",
        "border-beam": "border-beam 4s linear infinite",
        "grid-move": "grid-move 20s linear infinite",
      },
      backgroundImage: {
        "skiper-radial": "radial-gradient(600px circle at 50% -10%, rgba(99,102,241,0.15), transparent 80%)",
      },
    },
  },
  plugins: [],
};

export default config;
