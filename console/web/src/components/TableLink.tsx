import { Link } from "react-router-dom";

/** "ml_predictions.x" or "project.ml_predictions.x" -> its Catalog page, in this project. */
export default function TableLink({ name, project }: { name: string | null; project?: string }) {
  if (!name) return <>-</>;
  const parts = name.split(".");
  const [ds, t] = parts.slice(-2);
  if (parts.length === 3 && project && parts[0] !== project) return <>{name}</>;
  return <Link to={`/catalog/${ds}/${t}`}>{name}</Link>;
}
