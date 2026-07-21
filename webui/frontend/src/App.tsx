import { ThemeToggle } from "./components/ThemeToggle";
import { FilePage } from "./pages/FilePage";
import { ReposPage } from "./pages/ReposPage";
import { SearchPage } from "./pages/SearchPage";
import { useLinkInterception, useRoute } from "./router";

export function App(): JSX.Element {
  useLinkInterception();
  const route = useRoute();

  return (
    <div className="app-shell">
      <header className="app-header">
        <span className="brand">Code Search</span>
        <nav>
          <a href="/">Search</a>
          <a href="/repos">Repos</a>
        </nav>
        <ThemeToggle />
      </header>
      <main className="app-main">
        {route.page === "search" && <SearchPage initialQuery={route.query} />}
        {route.page === "file" && <FilePage repo={route.repo} path={route.path} line={route.line} />}
        {route.page === "repos" && <ReposPage />}
      </main>
    </div>
  );
}
