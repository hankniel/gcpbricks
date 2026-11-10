import { lazy, Suspense, useState } from "react";
import { Global } from "@emotion/react";
import { Navigate, NavLink, Route, Routes } from "react-router-dom";
import {
  Avatar, BeakerIcon, BookIcon, FolderHomeIcon, NotebookIcon, Button, CatalogIcon, CloudModelIcon, DashboardIcon, DollarIcon, DropdownMenu, FolderIcon, Input, ListIcon, ModelsIcon, MoonIcon, SearchIcon,
  ParagraphSkeleton, SidebarIcon, SunIcon, TableIcon, TableModelIcon, Typography, WorkflowsIcon, useDesignSystemTheme,
} from "@databricks/design-system";
import { api } from "./api";
import { Appearance, useAppearance } from "./theme";
import { useLoad } from "./util";
import { BundlePage, BundlesPage } from "./pages/Bundles";
import { DashboardPage, DashboardsPage } from "./pages/Dashboards";
import CostsPage from "./pages/Costs";
import JobsPage from "./pages/Jobs";
import JobPage from "./pages/Job";
import RunPage from "./pages/Run";
import TaskRunPage from "./pages/TaskRun";
import { CatalogLayout, CatalogPage, DatasetPage, TablePage } from "./pages/catalog/Catalog";
import { BatchServeTaskPage, BatchServingPage } from "./pages/ml/BatchServing";
import EndpointPage from "./pages/ml/Endpoint";
import ExperimentPage from "./pages/ml/Experiment";
import ExperimentRunPage from "./pages/ml/ExperimentRun";
import ExperimentsPage from "./pages/ml/Experiments";
import { FeatureGroupPage, FeaturesPage } from "./pages/ml/Features";
import ModelPage from "./pages/ml/Model";
import ModelVersionPage from "./pages/ml/ModelVersion";
import ModelsPage from "./pages/ml/Models";
import ServingPage from "./pages/ml/Serving";
import NotebooksPage from "./pages/Notebooks";
import NotebookEditorPage from "./pages/NotebookEditor";
import WorkspacePage from "./pages/Workspace";

// The blog brings mermaid, KaTeX and highlight.js: loaded when it's opened, not with the console.
const blog = () => import("./pages/Blog");
const BlogPage = lazy(() => blog().then((m) => ({ default: m.BlogPage })));
const BlogPostPage = lazy(() => blog().then((m) => ({ default: m.BlogPostPage })));
const BlogEditorPage = lazy(() => blog().then((m) => ({ default: m.BlogEditorPage })));

const NAV_KEY = "mlops-console.sidebar";

type Item = { to: string; label: string; icon: JSX.Element; live?: boolean };
const SECTIONS: { title?: string; items: Item[] }[] = [
  { items: [
    { to: "/workspace", label: "Workspace", icon: <FolderHomeIcon />, live: true },
    { to: "/catalog", label: "Catalog", icon: <CatalogIcon />, live: true },
    { to: "/", label: "Jobs & Pipelines", icon: <WorkflowsIcon />, live: true },
    { to: "/dashboards", label: "Dashboards", icon: <DashboardIcon />, live: true },
    { to: "/bundles", label: "Bundles", icon: <FolderIcon />, live: true },
    { to: "/notebooks", label: "Notebooks", icon: <NotebookIcon />, live: true },
    { to: "/blog", label: "Blog", icon: <BookIcon />, live: true },
    { to: "/costs", label: "Costs", icon: <DollarIcon />, live: true },
  ] },
  { title: "Data Engineering", items: [{ to: "/runs", label: "Runs", icon: <ListIcon />, live: true }] },
  { title: "AI/ML", items: [
    { to: "/experiments", label: "Experiments", icon: <BeakerIcon />, live: true },
    { to: "/features", label: "Features", icon: <TableIcon />, live: true },
    { to: "/models", label: "Models", icon: <ModelsIcon />, live: true },
    { to: "/serving", label: "Serving", icon: <CloudModelIcon />, live: true },
    { to: "/batch-serving", label: "Batch serving", icon: <TableModelIcon />, live: true },
  ] },
];

export default function App() {
  const { theme } = useDesignSystemTheme();
  const me = useLoad(api.me, []);
  // Databricks' nav row: 28px, 12px in, icons in the secondary color until the row is hovered or current.
  const row = {
    display: "flex", alignItems: "center", gap: theme.spacing.sm, height: 28, marginBottom: 2,
    padding: `6px ${theme.spacing.md - 4}px`, borderRadius: 4,
    color: theme.colors.textPrimary, textDecoration: "none", fontSize: 13, lineHeight: "16px",
    "& svg": { color: theme.colors.textSecondary },
  } as const;
  const lit = { background: theme.colors.actionDefaultBackgroundHover, color: theme.colors.actionDefaultTextHover,
                "& svg": { color: theme.colors.actionDefaultTextHover } };
  const { appearance, setAppearance } = useAppearance();
  const [navOpen, setNavOpen] = useState(() => { try { return localStorage.getItem(NAV_KEY) !== "closed"; } catch { return true; } });
  const setNav = (open: boolean) => {
    setNavOpen(open);
    try { localStorage.setItem(NAV_KEY, open ? "open" : "closed"); } catch { /* the choice lasts this visit only */ }
  };
  return (
    <>
    {/* Router links are plain anchors: they read as Typography.Link does, in either theme. */}
    {/* color-scheme makes native controls and scrollbars follow the theme; thin scrollbars as Databricks'. */}
    <Global styles={{ html: { colorScheme: theme.isDarkMode ? "dark" : "light" },
                      "*": { scrollbarWidth: "thin", scrollbarColor: `${theme.colors.borderDecorative ?? theme.colors.border} transparent` },
                      "*::-webkit-scrollbar": { width: 8, height: 8 },
                      "*::-webkit-scrollbar-track": { background: "transparent" },
                      "*::-webkit-scrollbar-thumb": { background: theme.colors.borderDecorative ?? theme.colors.border, borderRadius: 4,
                                                      "&:hover": { background: theme.colors.textPlaceholder } },
                      "*::-webkit-scrollbar-corner": { background: "transparent" } }} />
    <Global styles={{ "main a": { color: theme.colors.actionLinkDefault, textDecoration: "none",
                                  "&:hover": { color: theme.colors.actionLinkHover, textDecoration: "underline" },
                                  "&:active": { color: theme.colors.actionLinkPress } } }} />
    <div css={{ display: "grid", gridTemplateRows: "48px 1fr", height: "100%", background: theme.colors.backgroundSecondary }}>
      <header css={{ display: "grid", gridTemplateColumns: "1fr minmax(0, 560px) 1fr", columnGap: theme.spacing.md,
                     alignItems: "center", padding: `0 14px 0 ${theme.spacing.md}px` }}>
        <div css={{ display: "flex", alignItems: "center", gap: 4 }}>
          <Button componentId="topbar.nav" icon={<SidebarIcon />} aria-label={navOpen ? "Collapse sidebar" : "Expand sidebar"}
                  aria-expanded={navOpen} onClick={() => setNav(!navOpen)} css={{ "&&": { width: 32, height: 32 } }} />
          <span css={{ display: "flex", alignItems: "center", gap: theme.spacing.sm, padding: "0 5px" }}>
            <span css={{ width: 17, height: 17, borderRadius: 4, display: "grid", placeItems: "center",
                         background: theme.colors.actionPrimaryBackgroundDefault, color: theme.colors.actionPrimaryTextDefault,
                         fontWeight: 700, fontSize: 11, lineHeight: 1 }}>b</span>
            <span css={{ fontWeight: 600, fontSize: 18, lineHeight: "20px", letterSpacing: -0.3, color: theme.colors.textPrimary }}>gcpbricks</span>
          </span>
        </div>
        {/* Read-only rather than disabled: it reads as Databricks' search box, and still searches nothing. */}
        <Input componentId="topbar.search" prefix={<SearchIcon />} placeholder="Search jobs, runs, and more..." readOnly
               title="Search isn't available yet" />
        <div css={{ justifySelf: "end", display: "flex", alignItems: "center", gap: 4 }}>
          <Typography.Text size="sm" color="secondary">{me.data?.project}</Typography.Text>
          <DropdownMenu.Root>
            <DropdownMenu.Trigger asChild>
              <Button componentId="topbar.user" aria-label="User menu" css={{ padding: 0 }}
                      icon={<Avatar type="user" size="sm" label={me.data?.email ?? "User"} />} />
            </DropdownMenu.Trigger>
            <DropdownMenu.Content align="end" minWidth={220}>
              <DropdownMenu.Label>{me.data?.email ?? ""}</DropdownMenu.Label>
              <DropdownMenu.Separator />
              <DropdownMenu.Sub>
                <DropdownMenu.SubTrigger>
                  <DropdownMenu.IconWrapper>{appearance === "dark" ? <MoonIcon /> : <SunIcon />}</DropdownMenu.IconWrapper>
                  Appearance
                </DropdownMenu.SubTrigger>
                <DropdownMenu.SubContent>
                  <DropdownMenu.RadioGroup componentId="topbar.appearance" value={appearance}
                                           onValueChange={(v) => setAppearance(v as Appearance)}>
                    {(["light", "dark", "system"] as const).map((a) => (
                      <DropdownMenu.RadioItem key={a} value={a}>
                        <DropdownMenu.ItemIndicator />
                        {a === "system" ? "Match system" : a === "light" ? "Light" : "Dark"}
                      </DropdownMenu.RadioItem>
                    ))}
                  </DropdownMenu.RadioGroup>
                </DropdownMenu.SubContent>
              </DropdownMenu.Sub>
            </DropdownMenu.Content>
          </DropdownMenu.Root>
        </div>
      </header>
      <div css={{ display: "grid", gridTemplateColumns: navOpen ? "200px minmax(0, 1fr)" : "minmax(0, 1fr)", minHeight: 0 }}>
        <nav aria-label="Sections" hidden={!navOpen} css={{ padding: `0 ${theme.spacing.md - 4}px ${theme.spacing.md}px`, overflow: "auto" }}>
          {SECTIONS.map((sec, i) => (
            <div key={i}>
              {sec.title && <div css={{ padding: `21px ${theme.spacing.md - 4}px 7px`, fontSize: 12, lineHeight: "16px",
                                        color: theme.colors.textSecondary }}>{sec.title}</div>}
              {sec.items.map((n) => n.live ? (
                <NavLink key={n.to} to={n.to} end={n.to === "/"} css={{ ...row, "&.active, &:hover": lit }}>{n.icon}{n.label}</NavLink>
              ) : (
                <span key={n.to} title="Not available yet" css={{ ...row, color: theme.colors.textSecondary }}>{n.icon}{n.label}</span>
              ))}
            </div>
          ))}
        </nav>
        <main css={{ margin: navOpen ? "0 4px 4px 0" : "0 4px 4px", background: theme.colors.backgroundPrimary,
                     border: `1px solid ${theme.colors.border}`, borderRadius: 8,
                     overflow: "auto", padding: `${theme.spacing.md}px ${theme.spacing.md}px` }}>
          <Suspense fallback={<ParagraphSkeleton label="Loading" />}>
          <Routes>
            <Route path="/" element={<JobsPage tab="jobs" />} />
            <Route path="/runs" element={<JobsPage tab="runs" />} />
            <Route path="/jobs/:wf" element={<JobPage />} />
            <Route path="/jobs/:wf/runs/:id" element={<RunPage />} />
            <Route path="/jobs/:wf/runs/:id/tasks/:task" element={<TaskRunPage />} />
            <Route path="/bundles" element={<BundlesPage />} />
            <Route path="/bundles/:name" element={<BundlePage />} />
            <Route path="/dashboards" element={<DashboardsPage />} />
            <Route path="/dashboards/:bundle/:target/:key" element={<DashboardPage />} />
            <Route path="/experiments" element={<ExperimentsPage />} />
            <Route path="/experiments/:id" element={<ExperimentPage />} />
            <Route path="/experiments/:id/runs/:run" element={<ExperimentRunPage />} />
            <Route path="/models" element={<ModelsPage />} />
            <Route path="/costs" element={<CostsPage />} />
            <Route path="/models/:id" element={<ModelPage />} />
            <Route path="/models/:id/versions/:version" element={<ModelVersionPage />} />
            <Route path="/serving" element={<ServingPage />} />
            <Route path="/serving/:id" element={<EndpointPage />} />
            <Route path="/features" element={<FeaturesPage />} />
            <Route path="/workspace" element={<WorkspacePage />} />
            <Route path="/workspace/notebook" element={<NotebookEditorPage />} />
            <Route path="/notebooks" element={<NotebooksPage />} />
            <Route path="/compute" element={<Navigate to="/notebooks?tab=runtimes" replace />} />
            <Route path="/features/:name" element={<FeatureGroupPage />} />
            <Route path="/blog" element={<BlogPage />} />
            <Route path="/blog/:slug" element={<BlogPostPage />} />
            <Route path="/blog/:slug/edit" element={<BlogEditorPage />} />
            <Route path="/batch-serving" element={<BatchServingPage />} />
            <Route path="/batch-serving/:wf/:task" element={<BatchServeTaskPage />} />
            <Route path="/catalog" element={<CatalogLayout />}>
              <Route index element={<CatalogPage />} />
              <Route path=":dataset" element={<DatasetPage />} />
              <Route path=":dataset/:table" element={<TablePage />} />
            </Route>
          </Routes>
          </Suspense>
        </main>
      </div>
    </div>
    </>
  );
}
