import { useCallback, useEffect, useState } from "react";
import { Layout } from "./components/Layout";
import { clearToken, getToken, setToken, UNAUTHORIZED_EVENT } from "./lib/auth";
import { matchRoute } from "./lib/routes";
import { useRouter } from "./lib/router";
import { Bugs } from "./pages/Bugs";
import { Dashboard } from "./pages/Dashboard";
import { JobDetailPage } from "./pages/JobDetail";
import { Jobs } from "./pages/Jobs";
import { Login } from "./pages/Login";
import { Media } from "./pages/Media";
import { NotFound } from "./pages/NotFound";
import { Resources } from "./pages/Resources";
import { Workers } from "./pages/Workers";

function Routes() {
  const { path } = useRouter();
  if (matchRoute("/", path)) return <Dashboard />;
  if (matchRoute("/jobs", path)) return <Jobs />;
  const job = matchRoute("/jobs/:id/:tab?/:sub?", path);
  if (job?.id) return <JobDetailPage key={job.id} jobId={job.id} tab={job.tab ?? "overview"} sub={job.sub ?? null} />;
  if (matchRoute("/workers", path)) return <Workers />;
  if (matchRoute("/resources", path)) return <Resources />;
  if (matchRoute("/media", path)) return <Media />;
  if (matchRoute("/bugs", path)) return <Bugs />;
  return <NotFound />;
}

export function App() {
  const [token, setTok] = useState<string | null>(() => getToken());
  const [notice, setNotice] = useState<string | null>(null);

  useEffect(() => {
    const onUnauthorized = () => {
      setTok(null);
      setNotice("Sitzung abgelaufen oder Token ungültig – bitte erneut anmelden.");
    };
    window.addEventListener(UNAUTHORIZED_EVENT, onUnauthorized);
    return () => {
      window.removeEventListener(UNAUTHORIZED_EVENT, onUnauthorized);
    };
  }, []);

  const login = useCallback((t: string) => {
    setToken(t);
    setNotice(null);
    setTok(t);
  }, []);
  const logout = useCallback(() => {
    clearToken();
    setNotice("Abgemeldet.");
    setTok(null);
  }, []);

  if (!token) return <Login onLogin={login} notice={notice} />;
  return (
    <Layout onLogout={logout}>
      <Routes />
    </Layout>
  );
}
