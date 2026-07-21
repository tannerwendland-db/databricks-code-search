import { toggleTheme, useTheme } from "../theme";

export function ThemeToggle(): JSX.Element {
  const theme = useTheme();
  return (
    <button
      type="button"
      className="theme-toggle"
      onClick={toggleTheme}
      aria-label={`Switch to ${theme === "dark" ? "light" : "dark"} theme`}
      title={`Switch to ${theme === "dark" ? "light" : "dark"} theme`}
    >
      {theme === "dark" ? "🌙" : "☀️"}
    </button>
  );
}
