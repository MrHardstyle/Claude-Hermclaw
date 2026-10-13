import { Link } from "../lib/router";

export function NotFound() {
  return (
    <div className="page">
      <h1>Seite nicht gefunden</h1>
      <p>
        Diese Adresse gibt es nicht. <Link to="/">Zum Dashboard</Link>
      </p>
    </div>
  );
}
