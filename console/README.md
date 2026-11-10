# Console

Every bundle's jobs and runs, across repos, and a Catalog of the project's BigQuery tables with
their lineage, and a Blog of Markdown posts and guides (mermaid, KaTeX, code, GitHub alerts, pasted
images; kept in `gs://my-ml-bucket/blog`, `MLOPS_BLOG`), in one place: a FastAPI backend
(`gcpbricks.console`) and a React app on Databricks' own design system
(`@databricks/design-system`, ISC), served together behind Identity-Aware Proxy.

Local, against a project's real Workflows with your Application Default Credentials:

```bash
cd console/web && npm ci && npm run build && cd ../..
MLOPS_PROJECT=my-gcp-project MLOPS_CONSOLE_DEV=1 MLOPS_CONSOLE_STATIC=console/web/dist \
  uvicorn gcpbricks.console.app:app --port 8080
```

`npm run dev` in `console/web` serves the app with hot reload and proxies `/api` to port 8080.
